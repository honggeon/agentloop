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

from backend.app import cost_service
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


def _require_local_admin(settings) -> None:
    if not getattr(settings, "local_admin_enabled", False):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "admin_disabled",
                "message": "local admin endpoints are disabled (AUDITRAIL_LOCAL_ADMIN)",
            },
        )


@router.get("/admin/run-index/status")
def get_run_index_status() -> dict[str, Any]:
    settings = get_settings()
    _require_local_admin(settings)
    status = run_index.index_status(Path(settings.platform_dir))
    status["authority"] = "local"
    status["note"] = "本地管理能力：无生产级权限控制"
    return status


@router.post("/admin/run-index/rebuild")
def rebuild_run_index() -> dict[str, Any]:
    settings = get_settings()
    _require_local_admin(settings)
    status = run_index.rebuild_index(
        Path(settings.runs_dir),
        Path(settings.platform_dir),
        cost_lookup=cost_service.cost_lookup(settings),
    )
    status["authority"] = "local"
    status["note"] = "本地管理能力：无生产级权限控制"
    return status
