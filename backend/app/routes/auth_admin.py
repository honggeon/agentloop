"""Phase 5 routes: OIDC browser login flow and user administration.

- ``GET  /api/auth/oidc/login`` — build the authorization URL with a
  server-generated ``state``/``nonce`` pair and redirect (302).
- ``GET  /api/auth/oidc/callback`` — verify state, exchange the code
  server-side, verify the id_token (signature/issuer/audience/exp/nonce),
  then redirect to the SPA with the token in the URL *fragment* (fragments
  never hit access logs or the browser history API on the server side).
- ``POST /api/auth/logout`` — stateless acknowledgement; the client
  discards its token. Audited.
- ``GET  /api/admin/users`` / ``PUT  /api/admin/users/{user_id}/roles`` —
  user registry administration (``users:admin`` = owner role).

State/nonce pairs are short-lived (10 minutes), single-use, and stored in
the database when available (multi-instance safe) or in process memory
(single instance) otherwise. Tokens are never logged or audited.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse

from auditrail.run_contract import utc_now

from backend.app import audit_service, auth, users_service
from backend.app.config import Settings, get_settings
from backend.app.oidc import OidcError, provider_for

router = APIRouter(prefix="/api")

_STATE_TTL_SECONDS = 600
_MEMORY_STATES: Dict[str, Dict[str, Any]] = {}
_MEMORY_LOCK = threading.RLock()


def _error(status_code: int, code: str, message: str, *, retryable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": str(message)[:300], "request_id": "req-%s" % uuid.uuid4().hex[:12], "retryable": retryable},
    )


# ---------------------------------------------------------------------------
# state / nonce store
# ---------------------------------------------------------------------------


def _db_store_available(settings: Settings) -> bool:
    if str(getattr(settings, "storage_mode", "file") or "file") == "file":
        return False
    try:
        from backend.app import job_store as job_store_mod

        job_store_mod.database_for_settings(settings)
        return True
    except Exception:
        return False


def _save_state(settings: Settings, state: str, nonce: str, redirect_uri: str) -> None:
    from datetime import datetime, timedelta, timezone

    expires = (datetime.now(timezone.utc) + timedelta(seconds=_STATE_TTL_SECONDS)).isoformat().replace("+00:00", "Z")
    if _db_store_available(settings):
        from backend.app import job_store as job_store_mod

        job_store_mod.database_for_settings(settings).execute(
            "INSERT INTO oidc_states (state, nonce, redirect_uri, created_at, expires_at) VALUES (%s, %s, %s, %s, %s)",
            (state, nonce, redirect_uri, utc_now(), expires),
        )
        return
    with _MEMORY_LOCK:
        _MEMORY_STATES[state] = {"nonce": nonce, "redirect_uri": redirect_uri, "expires_at": time.time() + _STATE_TTL_SECONDS}


def _consume_state(settings: Settings, state: str) -> Optional[Dict[str, Any]]:
    """Single-use state lookup; expired or unknown states return None."""
    if _db_store_available(settings):
        from backend.app import job_store as job_store_mod

        db = job_store_mod.database_for_settings(settings)
        row = db.query_one("SELECT state, nonce, redirect_uri, expires_at FROM oidc_states WHERE state = %s", (state,))
        db.execute("DELETE FROM oidc_states WHERE state = %s", (state,))
        if row is None:
            return None
        expires = str(row.get("expires_at") or "")
        if expires < utc_now():
            return None
        return {"nonce": row.get("nonce"), "redirect_uri": row.get("redirect_uri")}
    with _MEMORY_LOCK:
        entry = _MEMORY_STATES.pop(state, None)
        # Opportunistic sweep of expired entries.
        now = time.time()
        for key in [key for key, value in _MEMORY_STATES.items() if value.get("expires_at", 0) < now]:
            _MEMORY_STATES.pop(key, None)
    if entry is None or entry.get("expires_at", 0) < time.time():
        return None
    return entry


# ---------------------------------------------------------------------------
# login flow
# ---------------------------------------------------------------------------


def _require_oidc(settings: Settings) -> None:
    if str(settings.auth_mode) != "oidc":
        raise _error(400, "auth_mode_not_oidc", "OIDC login requires AUDITRAIL_AUTH_MODE=oidc")


def _redirect_uri(settings: Settings) -> str:
    configured = str(getattr(settings, "oidc_redirect_uri", "") or "").strip()
    if configured:
        return configured
    return "http://localhost:%d/api/auth/oidc/callback" % int(settings.backend_port)


@router.get("/auth/oidc/login")
def oidc_login() -> Any:
    settings = get_settings()
    _require_oidc(settings)
    state = "st-%s" % secrets.token_urlsafe(16)
    nonce = "nc-%s" % secrets.token_urlsafe(16)
    redirect_uri = _redirect_uri(settings)
    try:
        url = provider_for(settings).build_login_url(state=state, nonce=nonce, redirect_uri=redirect_uri)
    except OidcError as exc:
        raise _error(exc.status, exc.code, str(exc), retryable=exc.retryable) from exc
    _save_state(settings, state, nonce, redirect_uri)
    return RedirectResponse(url, status_code=302)


@router.get("/auth/oidc/callback")
def oidc_callback(code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None) -> Any:
    settings = get_settings()
    _require_oidc(settings)
    if error:
        raise _error(401, "login_failed", "identity provider returned an error", retryable=False)
    if not code or not state:
        raise _error(400, "login_failed", "missing code or state")
    entry = _consume_state(settings, str(state))
    if entry is None:
        raise _error(401, "login_failed", "unknown or expired login state", retryable=False)
    provider = provider_for(settings)
    try:
        tokens = provider.exchange_code(code=str(code), redirect_uri=str(entry.get("redirect_uri") or ""))
        claims = provider.verify_token(str(tokens["id_token"]))
    except OidcError as exc:
        raise _error(exc.status, exc.code, "login failed", retryable=exc.retryable) from exc
    nonce = claims.get("nonce")
    if nonce is not None and str(nonce) != str(entry.get("nonce")):
        raise _error(401, "login_failed", "nonce mismatch", retryable=False)
    # The token travels in the URL fragment: never sent to the server in
    # logs/history, stored by the SPA in sessionStorage.
    target = "/login/callback#token=%s" % str(tokens["id_token"])
    return RedirectResponse(target, status_code=302)


@router.post("/auth/logout")
def logout() -> Dict[str, Any]:
    settings = get_settings()
    identity = auth.current_identity(settings)
    if identity.get("actor") and not identity.get("anonymous"):
        try:
            audit_service.append_event(
                settings,
                actor=str(identity["actor"]),
                role=str(identity.get("role") or "unknown"),
                action="auth_logout",
                resource_type="session",
                resource_id=None,
                result="success",
            )
        except (audit_service.AuditLogError, ValueError):
            pass  # logout never fails because auditing is unavailable
    return {"status": "logged_out", "note": "token 由客户端销毁；服务端不保存会话状态"}


# ---------------------------------------------------------------------------
# user administration
# ---------------------------------------------------------------------------


@router.get("/admin/users")
def list_users() -> Dict[str, Any]:
    settings = get_settings()
    identity = auth.require_permission(settings, "users:admin")
    if not users_service.available(settings):
        return {
            "users": [],
            "available": False,
            "note": "用户注册表需要 database 存储模式；当前为 file 模式（单身份本地模式）",
            "auth_mode": identity["mode"],
        }
    return {
        "users": users_service.list_users(settings),
        "available": True,
        "auth_mode": identity["mode"],
    }


@router.put("/admin/users/{user_id}/roles")
def update_user_roles(user_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    settings = get_settings()
    identity = auth.require_permission(settings, "users:admin")
    roles = payload.get("roles") if isinstance(payload, dict) else None
    if not isinstance(roles, list):
        raise _error(400, "invalid_roles", "roles must be a list of role names")
    # Assigning or revoking the owner role is owner-only.
    requesting_owner = identity.get("role") == "owner"
    if "owner" in [str(r).strip().lower() for r in roles] and not requesting_owner:
        raise _error(403, "permission_denied", "只有 owner 可以授予 owner 角色")
    try:
        existing = users_service.get_user(user_id, settings)
        if existing is not None and "owner" in (existing.get("roles") or []) and not requesting_owner:
            raise _error(403, "permission_denied", "只有 owner 可以修改 owner 的角色")
        user = users_service.update_user_roles(user_id, roles, settings)
    except KeyError:
        raise _error(404, "user_not_found", "unknown user %s" % user_id)
    except users_service.UserStoreError as exc:
        raise _error(400, "invalid_roles", str(exc))
    try:
        audit_service.append_event(
            settings,
            actor=str(identity.get("actor") or "unknown"),
            role=str(identity.get("role") or "unknown"),
            action="user_roles_update",
            resource_type="user",
            resource_id=user_id,
            result="success",
            reason="roles=%s" % ",".join(sorted(str(r) for r in roles))[:200],
        )
    except audit_service.AuditLogError as exc:
        raise _error(500, "audit_unavailable", "审计日志写入失败：%s" % str(exc)[:200]) from exc
    return {"user": user, "auth_mode": identity["mode"]}
