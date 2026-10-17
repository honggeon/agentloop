"""Phase 2 routes: LLM cost reads/imports and the local run-index admin API.

Costs: ``auditrail.costs`` owns the model, storage, import pipeline and price
table; this module only assembles HTTP. Import errors are reported per line
(redacted, bounded) and never abort the whole import; dry-runs validate
without touching the store.

Run index admin: this is a *local* management capability — there is no
production-grade auth in this deployment, so the endpoints are gated by the
server-owned ``local_admin_enabled`` setting and every response states its
``authority: "local"``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from auditrail import run_index

from backend.app import cost_service, run_index_watch
from backend.app.config import get_settings, resolve_run_path

router = APIRouter(prefix="/api")


# --------------------------------------------------------------------- costs


@router.get("/runs/{run_id}/costs")
def get_run_costs(run_id: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        resolve_run_path(run_id, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_run_id", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": str(exc)}) from exc
    return cost_service.run_costs(run_id, settings)


@router.get("/agents/{agent_id}/costs")
def get_agent_costs(agent_id: str) -> dict[str, Any]:
    settings = get_settings()
    if not agent_id or "/" in agent_id or "\\" in agent_id:
        raise HTTPException(status_code=400, detail={"code": "invalid_agent_id", "message": "invalid agent_id"})
    return cost_service.agent_costs(agent_id, settings)


@router.post("/costs/import")
def import_costs(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    try:
        report = cost_service.import_costs(payload, settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_import", "message": str(exc)}
        ) from exc
    return report


@router.get("/costs/imports/{import_id}")
def get_cost_import(import_id: str) -> dict[str, Any]:
    report = cost_service.get_import_report(import_id, get_settings())
    if report is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "import_not_found", "message": "unknown cost import %s" % import_id},
        )
    return report


# ------------------------------------------------------------- run index admin


def _require_local_admin(settings):
    if not getattr(settings, "local_admin_enabled", False):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "admin_disabled",
                "message": "local admin endpoints are disabled (AUDITRAIL_LOCAL_ADMIN)",
            },
        )
    # Phase 4: the local permission boundary applies on top of the legacy
    # kill-switch; default local mode grants the admin role.
    from backend.app import auth

    return auth.require_role(settings, "admin")


def _audit(settings, identity, action: str, resource_id: str) -> None:
    """Append the audit event; a failed write is an explicit 500."""
    from backend.app import audit_service

    try:
        audit_service.append_event(
            settings,
            actor=identity["actor"],
            role=identity["role"],
            action=action,
            resource_type="run_index",
            resource_id=resource_id,
            result="success",
        )
    except audit_service.AuditLogError as exc:
        raise HTTPException(
            status_code=500,
            detail={"code": "audit_unavailable", "message": "审计日志写入失败：%s" % str(exc)[:200]},
        ) from exc


@router.get("/admin/run-index/status")
def get_run_index_status() -> dict[str, Any]:
    settings = get_settings()
    _require_local_admin(settings)
    status = run_index.index_status(Path(settings.platform_dir))
    # Phase 3: watcher/polling state is reported separately and never
    # disguised as an index-consistency or data-quality signal.
    status["watcher"] = run_index_watch.watcher_status(settings)
    status["authority"] = "local"
    status["note"] = "本地管理能力：无生产级权限控制"
    return status


@router.post("/admin/run-index/rebuild")
def rebuild_run_index() -> dict[str, Any]:
    settings = get_settings()
    identity = _require_local_admin(settings)
    try:
        status = run_index.rebuild_index(
            Path(settings.runs_dir),
            Path(settings.platform_dir),
            cost_lookup=cost_service.cost_lookup(settings),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "index_rebuilding", "message": str(exc)[:200], "retryable": True},
        ) from exc
    _audit(settings, identity, "index_rebuild", "run_index")
    status["authority"] = "local"
    status["note"] = "本地管理能力：无生产级权限控制"
    return status
