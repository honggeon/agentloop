"""Phase 4/5 authentication and authorization boundary.

Modes (``AUDITRAIL_AUTH_MODE``):

- ``disabled``: every request is allowed. Local development only — refused
  at startup in production deployment (see ``config.validate_startup_settings``).
- ``local``: one server-owned identity (``AUDITRAIL_AUTH_ROLE`` /
  ``AUDITRAIL_AUTH_ACTOR``) authorizes requests. Development/single-operator
  mode, not a production authentication capability.
- ``oidc`` / ``jwt``: Bearer tokens verified server-side (issuer, audience,
  expiry, signature, JWKS rotation — see ``backend.app.oidc``). The
  production modes.

Rules enforced here:

- identity comes from server configuration or a verified token — **never**
  from request headers like ``X-Role`` (only the ``Authorization`` Bearer
  credential is read, and only its verified claims are used);
- roles are ordered ``viewer < operator < admin < owner`` and map to a
  fixed permission set; unknown token roles are ignored (never grant);
- 401 means unauthenticated (missing/invalid/expired token), 403 means
  authenticated but insufficient role;
- the tenant is server-owned (single-tenant ``default``); client-supplied
  ``tenant_id`` is never trusted;
- tokens never enter logs, audit events or responses.
"""

from __future__ import annotations

import contextvars
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from backend.app.config import Settings

ROLES = ("viewer", "operator", "admin", "owner")
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3, "owner": 4}

AUTH_MODES = ("local", "disabled", "oidc", "jwt")
PRODUCTION_AUTH_MODES = ("oidc", "jwt")

#: Role -> permission set. Centralized here; routes use require_role
#: (rank) or require_permission (capability) — frontend hiding is UX only.
ROLE_PERMISSIONS: Dict[str, frozenset] = {
    "viewer": frozenset(
        {
            "runs:read",
            "agents:read",
            "suites:read",
            "imports:read",
            "prices:read",
            "index:read",
            "audit:read",
        }
    ),
    "operator": frozenset(
        {
            "runs:read",
            "agents:read",
            "suites:read",
            "imports:read",
            "prices:read",
            "index:read",
            "audit:read",
            "jobs:run",
            "jobs:cancel",
            "imports:write",
            "imports:cancel",
            "imports:retry",
        }
    ),
    "admin": frozenset(
        {
            "runs:read",
            "agents:read",
            "agents:write",
            "suites:read",
            "suites:write",
            "jobs:run",
            "jobs:cancel",
            "imports:read",
            "imports:write",
            "imports:cancel",
            "imports:retry",
            "drafts:approve",
            "prices:read",
            "prices:write",
            "index:read",
            "index:admin",
            "audit:read",
        }
    ),
    "owner": frozenset(
        {
            "runs:read",
            "agents:read",
            "agents:write",
            "suites:read",
            "suites:write",
            "jobs:run",
            "jobs:cancel",
            "imports:read",
            "imports:write",
            "imports:cancel",
            "imports:retry",
            "drafts:approve",
            "prices:read",
            "prices:write",
            "index:read",
            "index:admin",
            "audit:read",
            "users:admin",
        }
    ),
}

_LOCAL_NOTE = "本地身份模式：单一本地身份，无生产级认证"
_DISABLED_NOTE = "认证已禁用：仅适用于本地开发，所有请求均被允许"
_OIDC_NOTE = "OIDC/JWT 认证：服务端校验 Bearer token（issuer/audience/expiry/签名）"

#: The request-scoped identity is stowed by the auth middleware; service
#: code and routes read it through ``current_identity``. Tests that never
#: pass through the middleware get the env-derived identity directly.
_REQUEST_IDENTITY: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "auditrail_request_identity", default=None
)

_ANONYMOUS = {"mode": "oidc", "role": None, "actor": None, "anonymous": True}


def set_request_identity(identity: Optional[Dict[str, Any]]) -> None:
    _REQUEST_IDENTITY.set(identity)


def get_request_identity() -> Optional[Dict[str, Any]]:
    return _REQUEST_IDENTITY.get()


# ---------------------------------------------------------------------------
# identity resolution
# ---------------------------------------------------------------------------


def _env_identity(settings: Settings) -> Dict[str, Any]:
    mode = str(getattr(settings, "auth_mode", "local") or "local")
    if mode not in AUTH_MODES:
        # A misconfigured mode fails closed rather than silently allowing.
        raise HTTPException(
            status_code=500,
            detail={
                "code": "auth_misconfigured",
                "message": "AUDITRAIL_AUTH_MODE must be one of local|disabled|oidc|jwt",
                "retryable": False,
            },
        )
    role = str(getattr(settings, "auth_role", "admin") or "admin")
    if role not in ROLES:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "auth_misconfigured",
                "message": "AUDITRAIL_AUTH_ROLE must be one of viewer|operator|admin|owner",
                "retryable": False,
            },
        )
    actor = str(getattr(settings, "auth_actor", "") or "").strip() or ("local-%s" % role)
    return {"mode": mode, "role": role, "actor": actor, "tenant_id": tenant_context(settings)["tenant_id"]}


def resolve_bearer_identity(settings: Settings, token: str) -> Dict[str, Any]:
    """Verify a Bearer token and map it to an identity.

    Role resolution: a server-managed user record (database) wins when it
    exists; otherwise the token's recognized roles are used; otherwise the
    least-privileged ``viewer``. Raises ``OidcError`` on any failure.
    """
    from backend.app import oidc

    provider = oidc.provider_for(settings)
    claims = provider.verify_token(token)
    token_roles = provider.extract_roles(claims)
    subject = str(claims.get("sub") or "")[:128]
    issuer = str(claims.get("iss") or "")[:256]
    email = claims.get("email")
    display_name = claims.get("name") or claims.get("preferred_username")

    roles = token_roles
    user_id = None
    if str(getattr(settings, "storage_mode", "file") or "file") != "file":
        try:
            from backend.app import users_service

            user = users_service.provision_from_claims(
                settings, issuer=issuer, subject=subject, email=email, display_name=display_name, token_roles=token_roles
            )
            if user is not None:
                user_id = user.get("user_id")
                db_roles = [r for r in (user.get("roles") or []) if r in ROLES]
                if db_roles:
                    roles = db_roles
        except Exception:
            # User provisioning is best-effort; token roles still apply.
            pass

    role = max(roles, key=lambda r: ROLE_RANK.get(r, 0)) if roles else "viewer"
    return {
        "mode": str(settings.auth_mode),
        "role": role,
        "roles": roles or ["viewer"],
        "actor": subject,
        "subject": subject,
        "issuer": issuer,
        "email": (str(email)[:128] if isinstance(email, str) else None),
        "display_name": (str(display_name)[:128] if isinstance(display_name, str) else None),
        "user_id": user_id,
        "tenant_id": tenant_context(settings)["tenant_id"],
    }


def current_identity(settings: Settings) -> Dict[str, Any]:
    """The effective caller identity.

    Reads the request-scoped identity stowed by the auth middleware first
    (token-verified in oidc/jwt mode); outside a request (tests, CLI,
    startup hooks) falls back to the env-derived identity for local modes.
    """
    request_identity = get_request_identity()
    if request_identity is not None:
        return request_identity
    mode = str(getattr(settings, "auth_mode", "local") or "local")
    if mode in PRODUCTION_AUTH_MODES:
        # No verified token in scope: anonymous. Callers requiring a role
        # turn this into 401.
        return {"mode": mode, "role": None, "actor": None, "anonymous": True}
    return _env_identity(settings)


# ---------------------------------------------------------------------------
# authorization
# ---------------------------------------------------------------------------


def role_allowed(role: Optional[str], minimum: str) -> bool:
    return ROLE_RANK.get(str(role or ""), 0) >= ROLE_RANK.get(minimum, 99)


def role_permissions(role: Optional[str]) -> frozenset:
    return ROLE_PERMISSIONS.get(str(role or ""), frozenset())


def _unauthenticated(settings: Settings) -> HTTPException:
    from backend.app import metrics

    metrics.counter_inc("auditrail_auth_failures_total", _help="Authentication failures", mode=str(settings.auth_mode))
    return HTTPException(
        status_code=401,
        detail={
            "code": "authentication_required",
            "message": "需要有效的认证凭据",
            "retryable": False,
        },
    )


def require_role(settings: Settings, minimum: str) -> Dict[str, Any]:
    """Authorize the current identity for ``minimum`` role.

    Raises 401 when unauthenticated (oidc/jwt without a valid token) and
    403 when the role is insufficient. Returns the identity mapping for
    audit logging. In ``disabled`` mode every request passes and the
    returned identity says so — callers must surface the mode.
    """
    identity = current_identity(settings)
    if identity["mode"] == "disabled":
        return identity
    if identity.get("anonymous") or identity.get("role") is None:
        raise _unauthenticated(settings)
    if not role_allowed(identity["role"], minimum):
        from backend.app import metrics

        metrics.counter_inc(
            "auditrail_permission_denials_total", _help="Permission denials", required=minimum
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "permission_denied",
                "message": "当前身份角色 %s 无权执行该操作（需要 %s）" % (identity["role"], minimum),
                "retryable": False,
            },
        )
    return identity


def require_permission(settings: Settings, permission: str) -> Dict[str, Any]:
    """Capability-based authorization (fine-grained RBAC)."""
    identity = current_identity(settings)
    if identity["mode"] == "disabled":
        return identity
    if identity.get("anonymous") or identity.get("role") is None:
        raise _unauthenticated(settings)
    if permission not in role_permissions(identity.get("role")):
        from backend.app import metrics

        metrics.counter_inc(
            "auditrail_permission_denials_total", _help="Permission denials", required=permission
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "permission_denied",
                "message": "当前身份缺少权限 %s" % permission,
                "retryable": False,
            },
        )
    return identity


# ---------------------------------------------------------------------------
# tenant context (explicitly single-tenant)
# ---------------------------------------------------------------------------


def tenant_context(settings: Settings) -> Dict[str, Any]:
    """The server-owned tenant context. Multi-tenant deployment is not
    implemented: the tenant id comes from configuration only, client input
    is never consulted, and every database constraint carries it."""
    return {
        "tenant_id": str(getattr(settings, "tenant_id", "default") or "default"),
        "single_tenant": True,
    }


def strip_untrusted_tenant(payload: Any) -> Any:
    """Drop a client-supplied ``tenant_id`` — it is never a trusted field."""
    if isinstance(payload, dict) and "tenant_id" in payload:
        return {key: value for key, value in payload.items() if key != "tenant_id"}
    return payload


# ---------------------------------------------------------------------------
# context payload
# ---------------------------------------------------------------------------


def auth_context_payload(settings: Settings) -> Dict[str, Any]:
    """Public auth context for the UI: mode, role, permissions and an
    honest boundary note. Never includes tokens or secrets."""
    identity = current_identity(settings)
    mode = identity["mode"]
    role = identity.get("role")
    capabilities = sorted(role_permissions(role)) if role else []
    if mode == "disabled":
        capabilities = sorted(set().union(*ROLE_PERMISSIONS.values()))
    payload: Dict[str, Any] = {
        "auth_mode": mode,
        "role": role,
        "actor": identity.get("actor"),
        "roles": list(ROLES),
        # Phase 4 UI compatibility map (booleans per capability area).
        "permissions": {
            "import": role_allowed(role, "operator") or mode == "disabled",
            "review_draft": role_allowed(role, "operator") or mode == "disabled",
            "approve_draft": role_allowed(role, "admin") or mode == "disabled",
            "manage_prices": role_allowed(role, "admin") or mode == "disabled",
            "manage_agents": role_allowed(role, "admin") or mode == "disabled",
            "manage_index": role_allowed(role, "admin") or mode == "disabled",
        },
        # Phase 5 fine-grained permission list (RBAC capability model).
        "capabilities": capabilities,
        "authenticated": bool(role) or mode == "disabled",
        "tenant": tenant_context(settings),
        "note": _DISABLED_NOTE if mode == "disabled" else (_LOCAL_NOTE if mode == "local" else _OIDC_NOTE),
        "production_ready": mode in PRODUCTION_AUTH_MODES,
    }
    if identity.get("email") or identity.get("display_name"):
        payload["user"] = {
            "subject": identity.get("subject"),
            "email": identity.get("email"),
            "display_name": identity.get("display_name"),
        }
    if mode == "oidc":
        payload["login_url"] = "/api/auth/oidc/login"
        payload["logout_url"] = "/api/auth/logout"
    return payload
