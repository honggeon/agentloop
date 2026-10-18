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


# ---------------------------------------------------------------------------
# Phase 5: platform health / workers / migrations / backups
# ---------------------------------------------------------------------------


@router.get("/admin/health")
def get_admin_health() -> dict[str, Any]:
    """Detailed platform health: database, queue, storage, workers, stale
    jobs, index, recent failures, audit writability. Never exposes secrets,
    connection strings or absolute paths."""
    from backend.app import health as health_mod

    settings = get_settings()
    auth.require_role(settings, "viewer")
    return health_mod.readiness(settings)


@router.get("/admin/workers")
def get_admin_workers() -> dict[str, Any]:
    """Worker registry + embedded worker status."""
    settings = get_settings()
    identity = auth.require_role(settings, "viewer")
    from backend.app import job_store as job_store_mod, queue_adapter, worker_main

    workers: list = []
    registry_available = False
    if job_store_mod.storage_mode(settings) != "file":
        try:
            store = job_store_mod.job_store(settings)
            raw = getattr(store, "raw", None)
            if raw is not None:
                workers = raw.list_workers()
                registry_available = True
        except Exception:
            registry_available = False
    return {
        "workers": workers,
        "registry_available": registry_available,
        "embedded_worker": worker_main.embedded_worker_status(),
        "queue_mode": queue_adapter.queue_mode(settings),
        "queue_reachable": queue_adapter.queue_ping(settings),
        "auth_mode": identity["mode"],
    }


@router.get("/admin/migrations")
def get_admin_migrations() -> dict[str, Any]:
    """Schema migration status (applied/pending). Never auto-applies."""
    settings = get_settings()
    auth.require_role(settings, "viewer")
    from backend.app import job_store as job_store_mod

    return job_store_mod.migration_state(settings)


@router.post("/admin/migrations/apply")
def apply_admin_migrations() -> dict[str, Any]:
    """Explicitly apply pending schema migrations (admin, audited)."""
    settings = get_settings()
    identity = auth.require_role(settings, "admin")
    from auditrail.db import MigrationError, apply_migrations

    from backend.app import job_store as job_store_mod

    if job_store_mod.storage_mode(settings) == "file":
        raise _error(400, "storage_file_mode", "storage_mode=file 不使用数据库，无需 migration")
    try:
        db = job_store_mod.database_for_settings(settings)
        applied = apply_migrations(db)
    except MigrationError as exc:
        raise _error(500, "migration_failed", str(exc)) from exc
    _audit(settings, identity, "migration_run", "database", "schema", reason="applied=%s" % (applied or []))
    state = job_store_mod.migration_state(settings)
    state["applied_now"] = applied
    state["auth_mode"] = identity["mode"]
    return state


@router.get("/admin/backups")
def list_admin_backups() -> dict[str, Any]:
    """List backup manifests under the configured backup root."""
    import json as json_mod

    settings = get_settings()
    identity = auth.require_role(settings, "viewer")
    root = Path(settings.backup_dir) if settings.backup_dir else (Path(settings.platform_dir) / "backups")
    backups: list = []
    if root.is_dir():
        for child in sorted(root.iterdir(), reverse=True):
            manifest_path = child / "manifest.json"
            if not manifest_path.is_file():
                continue
            try:
                manifest = json_mod.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json_mod.JSONDecodeError):
                continue
            components = manifest.get("components") or {}
            backups.append(
                {
                    "backup_id": manifest.get("backup_id") or child.name,
                    "created_at": manifest.get("created_at"),
                    "file_count": (components.get("platform_files") or {}).get("file_count", 0),
                    "table_count": (components.get("database") or {}).get("table_count", 0),
                    "contains_secrets": bool(manifest.get("contains_secrets")),
                }
            )
    return {
        "backups": backups[:50],
        "backup_root_configured": bool(settings.backup_dir),
        "auth_mode": identity["mode"],
        "note": "备份由 `auditrail backup-platform` / POST /api/admin/backups 创建；runs/ 证据不包含在内",
    }


@router.post("/admin/backups")
def create_admin_backup() -> dict[str, Any]:
    """Create a platform backup now (admin, audited). runs/ evidence is not
    included — see docs/backup-restore.md."""
    settings = get_settings()
    identity = auth.require_role(settings, "admin")
    from auditrail import platform_backup

    root = Path(settings.backup_dir) if settings.backup_dir else (Path(settings.platform_dir) / "backups")
    db = None
    from backend.app import job_store as job_store_mod

    if job_store_mod.storage_mode(settings) != "file":
        try:
            db = job_store_mod.database_for_settings(settings)
        except Exception as exc:
            raise _error(503, "storage_unavailable", "数据库不可用：%s" % type(exc).__name__, retryable=True) from exc
    try:
        manifest = platform_backup.backup_platform(Path(settings.platform_dir), root, db=db)
    except ValueError as exc:
        raise _error(400, "backup_failed", str(exc)) from exc
    _audit(settings, identity, "backup_create", "backup", str(manifest.get("backup_id")))
    return {
        "backup_id": manifest.get("backup_id"),
        "created_at": manifest.get("created_at"),
        "components": {
            name: {key: value for key, value in component.items() if key in ("file_count", "table_count", "entries")}
            for name, component in (manifest.get("components") or {}).items()
        },
        "contains_secrets": False,
        "auth_mode": identity["mode"],
    }
