"""Phase 4 minimal, honest local permission boundary.

There is no production identity system in this deployment and this module
does not pretend otherwise. Access control is a *local development* guard:

- ``AUDITRAIL_AUTH_MODE=local`` (default): a single configured identity from
  ``AUDITRAIL_AUTH_ROLE`` / ``AUDITRAIL_AUTH_ACTOR`` (server-owned env, never
  request headers) authorizes requests;
- ``AUDITRAIL_AUTH_MODE=disabled``: every request is allowed. This is a
  local-development convenience only — every response that depends on it
  states ``auth_mode: "disabled"`` and the UI surfaces a development banner.
  It is never a production security capability.

Roles (ascending): ``viewer`` < ``operator`` < ``admin``.

- viewer: read-only (agents, runs, suite jobs, external evidence, audit log);
- operator: start/cancel/resume imports, review (reject/archive) drafts;
- admin: agent config writes, price version mutations, index rebuild, watcher
  control, promptfoo draft approval.

Anything beyond this (SSO, multi-user RBAC, token verification) is
explicitly out of scope for this phase.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import HTTPException

from backend.app.config import Settings

ROLES = ("viewer", "operator", "admin")
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}

AUTH_MODES = ("local", "disabled")

_LOCAL_NOTE = "本地身份模式：单一本地身份，无生产级认证"
_DISABLED_NOTE = "认证已禁用：仅适用于本地开发，所有请求均被允许"


def current_identity(settings: Settings) -> Dict[str, Any]:
    """The effective caller identity from server-owned configuration."""
    mode = str(getattr(settings, "auth_mode", "local") or "local")
    if mode not in AUTH_MODES:
        # A misconfigured mode fails closed rather than silently allowing.
        raise HTTPException(
            status_code=500,
            detail={
                "code": "auth_misconfigured",
                "message": "AUDITRAIL_AUTH_MODE must be one of local|disabled",
                "retryable": False,
            },
        )
    role = str(getattr(settings, "auth_role", "admin") or "admin")
    if role not in ROLES:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "auth_misconfigured",
                "message": "AUDITRAIL_AUTH_ROLE must be one of viewer|operator|admin",
                "retryable": False,
            },
        )
    actor = str(getattr(settings, "auth_actor", "") or "").strip() or ("local-%s" % role)
    return {"mode": mode, "role": role, "actor": actor}


def role_allowed(role: str, minimum: str) -> bool:
    return ROLE_RANK.get(role, 0) >= ROLE_RANK.get(minimum, 99)


def require_role(settings: Settings, minimum: str) -> Dict[str, Any]:
    """Authorize the current identity for ``minimum`` role; raises 403.

    Returns the identity mapping for audit logging. In ``disabled`` mode every
    request passes and the returned identity says so — callers must surface
    the mode in their response.
    """
    identity = current_identity(settings)
    if identity["mode"] == "disabled":
        return identity
    if not role_allowed(identity["role"], minimum):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "permission_denied",
                "message": "当前身份角色 %s 无权执行该操作（需要 %s）" % (identity["role"], minimum),
                "retryable": False,
            },
        )
    return identity


def auth_context_payload(settings: Settings) -> Dict[str, Any]:
    """Public auth context for the UI: mode, role and an honest boundary note."""
    identity = current_identity(settings)
    return {
        "auth_mode": identity["mode"],
        "role": identity["role"],
        "actor": identity["actor"],
        "roles": list(ROLES),
        "permissions": {
            "import": role_allowed(identity["role"], "operator") or identity["mode"] == "disabled",
            "review_draft": role_allowed(identity["role"], "operator") or identity["mode"] == "disabled",
            "approve_draft": role_allowed(identity["role"], "admin") or identity["mode"] == "disabled",
            "manage_prices": role_allowed(identity["role"], "admin") or identity["mode"] == "disabled",
            "manage_agents": role_allowed(identity["role"], "admin") or identity["mode"] == "disabled",
            "manage_index": role_allowed(identity["role"], "admin") or identity["mode"] == "disabled",
        },
        "note": _DISABLED_NOTE if identity["mode"] == "disabled" else _LOCAL_NOTE,
        "production_ready": False,
    }
