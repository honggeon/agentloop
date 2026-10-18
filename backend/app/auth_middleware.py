"""Phase 5 auth middleware: resolve identity per request, enforce the gate.

Resolution order per request:

1. public paths (health/metrics/auth-context/login) never require a token;
2. ``local``/``disabled`` modes stow the server-owned env identity;
3. ``oidc``/``jwt`` modes read the ``Authorization: Bearer`` credential,
   verify it (issuer/audience/expiry/signature — see ``backend.app.oidc``)
   and stow the resulting identity; a missing/invalid/expired token on a
   protected path is a 401 (never a silent pass).

The middleware never logs tokens and never trusts role/tenant headers.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional

from backend.app import metrics
from backend.app.auth import PRODUCTION_AUTH_MODES, resolve_bearer_identity, set_request_identity
from backend.app.config import Settings, get_settings
from backend.app.oidc import OidcError

#: Paths that stay reachable without credentials (health probes, the auth
#: context the login page needs, and the login flow itself).
_PUBLIC_PREFIXES = (
    "/api/health",
    "/api/metrics",
    "/api/auth/",
)

_PUBLIC_EXACT = frozenset({"/api/auth/context"})


def _is_public(path: str) -> bool:
    if path in _PUBLIC_EXACT:
        return True
    if not path.startswith("/api/"):
        return True  # docs, openapi, static assets
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


class AuthMiddleware:
    """Pure-ASGI middleware (no BaseHTTPMiddleware buffering)."""

    def __init__(self, app: Any, settings: Optional[Settings] = None) -> None:
        self.app = app
        self._settings = settings

    def _settings_now(self) -> Settings:
        return self._settings or get_settings()

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        settings = self._settings_now()
        mode = str(getattr(settings, "auth_mode", "local") or "local")
        path = str(scope.get("path") or "")

        if mode not in PRODUCTION_AUTH_MODES:
            # local/disabled: identity derives from server-owned settings at
            # the call site (routes pass their own settings), so nothing is
            # stowed here — ``current_identity`` falls back to the env
            # identity of the caller's settings.
            set_request_identity(None)
            await self.app(scope, receive, send)
            return

        # production modes
        token = self._bearer_token(scope)
        identity: Optional[Dict[str, Any]] = None
        error: Optional[OidcError] = None
        if token:
            try:
                identity = resolve_bearer_identity(settings, token)
            except OidcError as exc:
                error = exc
        if identity is not None:
            set_request_identity(identity)
            await self.app(scope, receive, send)
            return

        if _is_public(path):
            # Public path: continue anonymously; require_role still 401s.
            set_request_identity({"mode": mode, "role": None, "actor": None, "anonymous": True})
            await self.app(scope, receive, send)
            return

        code = "token_missing" if error is None else error.code
        status = 401 if error is None else error.status
        metrics.counter_inc("auditrail_auth_failures_total", _help="Authentication failures", mode=mode, reason=code)
        await self._reject(send, status, code, "认证失败或缺少有效凭据")
        return

    @staticmethod
    def _bearer_token(scope: Dict[str, Any]) -> Optional[str]:
        headers = scope.get("headers") or []
        for name, value in headers:
            if name.lower() == b"authorization":
                text = value.decode("latin-1").strip()
                if text.lower().startswith("bearer "):
                    return text[7:].strip() or None
                return None
        return None

    @staticmethod
    async def _reject(send: Callable, status: int, code: str, message: str) -> None:
        body = json.dumps(
            {"detail": {"code": code, "message": message, "retryable": False}},
            ensure_ascii=False,
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})
