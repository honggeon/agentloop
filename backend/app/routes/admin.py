"""Phase 4 routes: auth context, audit events, run-index health/pause/resume.

These are *local* management capabilities. There is no production auth in
this deployment: the honest local permission boundary lives in
``backend.app.auth`` (``AUDITRAIL_AUTH_MODE``) and every response states its
``authority``/``auth_mode``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from auditrail import run_index

from backend.app import audit_service, auth, run_index_watch
from backend.app.config import get_settings

router = APIRouter(prefix="/api")


def _error(status_code: int, code: str, message: str, *, retryable: bool = False) -> HTTPException:
    import uuid

    return HTTPException(
        status_code=status_code,
        detail={
            "code": code,
            "message": str(message)[:300],
            "request_id": "req-%s" % uuid.uuid4().hex[:12],
            "retryable": retryable,
        },
    )


def _audit(settings, identity, action, resource_type, resource_id, reason: Optional[str] = None) -> None:
    try:
        audit_service.append_event(
            settings,
            actor=identity["actor"],
            role=identity["role"],
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result="success",
            reason=reason,
        )
    except audit_service.AuditLogError as exc:
        raise _error(500, "audit_unavailable", "审计日志写入失败：%s" % str(exc)) from exc


# ---------------------------------------------------------------------------
# auth context + audit log
# ---------------------------------------------------------------------------


@router.get("/auth/context")
def get_auth_context() -> dict[str, Any]:
    """The honest auth boundary: mode, role and what this identity may do."""
    return auth.auth_context_payload(get_settings())


@router.get("/audit-events")
def list_audit_events(
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = audit_service.DEFAULT_QUERY_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Paginated read of the append-only audit log (viewer and above)."""
    settings = get_settings()
    auth.require_role(settings, "viewer")
    try:
        payload = audit_service.query_events(
            settings,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            limit=limit,
            offset=offset,
        )
    except (TypeError, ValueError) as exc:
        raise _error(400, "invalid_pagination", str(exc)) from exc
    identity = auth.current_identity(settings)
    payload["auth_mode"] = identity["mode"]
    return payload


# ---------------------------------------------------------------------------
# run index health / pause / resume
# ---------------------------------------------------------------------------


def _require_index_admin(settings) -> dict[str, Any]:
    if not getattr(settings, "local_admin_enabled", False):
        raise _error(403, "admin_disabled", "local admin endpoints are disabled (AUDITRAIL_LOCAL_ADMIN)")
    return auth.require_role(settings, "admin")


@router.get("/admin/run-index/health")
def get_run_index_health() -> dict[str, Any]:
    """Index + watcher health: status, refresh reasons, degraded counters."""
    settings = get_settings()
    auth.require_role(settings, "viewer")
    status = run_index.index_status(Path(settings.platform_dir))
    watcher = run_index_watch.watcher_status(settings)
    degraded = watcher.get("status") == "degraded" or status.get("status") == run_index.STATUS_UNAVAILABLE
    return {
        "schema_version": run_index.SCHEMA_VERSION,
        "index": status,
        "watcher": watcher,
        "degraded": bool(degraded),
        "paused": bool(status.get("paused")) or bool(watcher.get("paused")),
        "authority": "local",
        "auth_mode": auth.current_identity(settings)["mode"],
        "note": "索引是缓存，不是事实来源；Run 详情始终读取正式产物",
    }


@router.post("/admin/run-index/pause")
def pause_run_index() -> dict[str, Any]:
    """Pause automatic index refreshes (persisted). Manual rebuild allowed."""
    settings = get_settings()
    identity = _require_index_admin(settings)
    watcher = run_index_watch.pause_watcher(settings)
    _audit(settings, identity, "index_pause", "run_index", "run_index")
    return {
        "watcher": watcher,
        "paused": True,
        "authority": "local",
        "auth_mode": identity["mode"],
        "note": "已暂停自动刷新；手动 rebuild 仍可显式执行",
    }


@router.post("/admin/run-index/resume")
def resume_run_index() -> dict[str, Any]:
    """Resume automatic index refreshes (persisted)."""
    settings = get_settings()
    identity = _require_index_admin(settings)
    watcher = run_index_watch.resume_watcher(settings)
    _audit(settings, identity, "index_resume", "run_index", "run_index")
    return {
        "watcher": watcher,
        "paused": False,
        "authority": "local",
        "auth_mode": identity["mode"],
    }
