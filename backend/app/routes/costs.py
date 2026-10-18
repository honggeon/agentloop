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
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException

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
def import_costs(payload: dict[str, Any], idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")) -> Any:
    """Create a cost ImportJob. Small/dry-run imports execute inline and
    return the Phase 2-compatible report; large imports run asynchronously
    on the worker (202 + ``status_url``). Same state machine as the
    Langfuse/promptfoo imports."""
    import uuid

    from fastapi.responses import JSONResponse

    from auditrail.integrations import import_jobs

    from backend.app import audit_service, auth, import_worker, integration_service, queue_adapter

    settings = get_settings()
    identity = auth.require_role(settings, "operator")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail={"code": "invalid_import", "message": "import payload must be an object"})
    request_id = "req-%s" % uuid.uuid4().hex[:12]
    fingerprint = integration_service.job_fingerprint("costs", payload)

    if idempotency_key:
        existing = integration_service.lookup_import_idempotency(idempotency_key, settings)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_conflict", "message": "Idempotency-Key was already used with a different import payload"},
                )
            replay = cost_service.get_cost_import(str(existing.get("import_id") or ""), settings)
            if replay is None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_conflict", "message": "Idempotency-Key references an import that no longer exists"},
                )
            body = _cost_job_response(replay)
            body["idempotent_replay"] = True
            body["auth_mode"] = identity["mode"]
            status_code = 202 if replay.get("status") in import_jobs.ACTIVE_STATUSES else 200
            return JSONResponse(status_code=status_code, content=body)

    try:
        job = cost_service.create_cost_import_job(payload, settings, actor=identity["actor"], idempotency_key=idempotency_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail={"code": "invalid_import", "message": str(exc)}
        ) from exc

    import_id = str(job["import_id"])
    try:
        audit_service.append_event(
            settings,
            actor=identity["actor"],
            role=identity["role"],
            action="import_create",
            resource_type="import_job",
            resource_id=import_id,
            result="success",
        )
    except audit_service.AuditLogError as exc:
        raise HTTPException(
            status_code=500, detail={"code": "audit_unavailable", "message": "审计日志写入失败：%s" % str(exc)[:200]}
        ) from exc
    if idempotency_key:
        integration_service.store_import_idempotency(idempotency_key, fingerprint, import_id, settings)

    if job.get("mode") == "sync":
        finished = import_worker.execute_import_inline(import_id, settings)
        body = _cost_job_response(import_jobs.job_view(finished))
        body["auth_mode"] = identity["mode"]
        body["request_id"] = request_id
        return body

    import_worker.submit_import(import_id, settings)
    degraded = _deliver_async(import_id, settings)
    body = {
        "import_id": import_id,
        "status": job.get("status"),
        "mode": "async",
        "queue_degraded": degraded,
        "status_url": "/api/costs/imports/%s" % import_id,
        "request_id": request_id,
        "auth_mode": identity["mode"],
    }
    return JSONResponse(status_code=202, content=body)


def _deliver_async(import_id: str, settings) -> bool:
    """Broker hint for async jobs (see ``import_worker.deliver_async``)."""
    from backend.app import import_worker

    return import_worker.deliver_async(import_id, settings)


def _cost_job_response(view: dict[str, Any]) -> dict[str, Any]:
    """The sync/async response shape: the embedded legacy report when the
    job finished, additive job fields always."""
    report = view.get("report") if isinstance(view.get("report"), dict) else None
    if report is None:
        return {
            "import_id": view.get("import_id"),
            "source_system": view.get("source_system"),
            "kind": view.get("kind"),
            "status": view.get("status"),
            "mode": view.get("mode"),
            "job_status": view.get("status"),
            "dry_run": bool(view.get("dry_run")),
        }
    body = dict(report)
    body["mode"] = view.get("mode")
    body["job_status"] = view.get("status")
    return body


@router.get("/costs/imports/{import_id}")
def get_cost_import(import_id: str) -> dict[str, Any]:
    view = cost_service.get_cost_import(import_id, get_settings())
    if view is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "import_not_found", "message": "unknown cost import %s" % import_id},
        )
    return view


@router.post("/costs/imports/{import_id}/cancel")
def cancel_cost_import(import_id: str) -> dict[str, Any]:
    return _control_cost_import(import_id, "cancel")


@router.post("/costs/imports/{import_id}/pause")
def pause_cost_import(import_id: str) -> dict[str, Any]:
    return _control_cost_import(import_id, "pause")


@router.post("/costs/imports/{import_id}/resume")
@router.post("/costs/imports/{import_id}/retry")
def resume_cost_import(import_id: str) -> Any:
    return _control_cost_import(import_id, "resume")


def _control_cost_import(import_id: str, action: str) -> Any:
    """cancel/pause/resume for cost imports — identical semantics to the
    integration ImportJob controls (same state machine, same audit)."""
    import uuid

    from fastapi.responses import JSONResponse

    from auditrail.integrations import import_jobs

    from backend.app import audit_service, auth, import_worker

    settings = get_settings()
    identity = auth.require_role(settings, "operator")
    try:
        if action == "cancel":
            job = import_worker.request_cancel(import_id, settings)
            audit_action = "import_cancel"
        elif action == "pause":
            job = import_worker.request_pause(import_id, settings)
            audit_action = "import_pause"
        else:
            existing = cost_service.get_cost_import(import_id, settings)
            if existing is None:
                raise KeyError(import_id)
            if str(existing.get("status") or "") == "paused":
                job = import_worker.prepare_resume(import_id, settings)
            else:
                job = import_worker.prepare_retry(import_id, settings)
            audit_action = "import_resume"
    except KeyError:
        raise HTTPException(
            status_code=404, detail={"code": "import_not_found", "message": "unknown cost import %s" % import_id}
        )
    except import_jobs.ImportJobError as exc:
        status_code = 409 if exc.code == "import_invalid_state" else 400
        raise HTTPException(
            status_code=status_code,
            detail={"code": exc.code, "message": str(exc)[:300], "retryable": exc.retryable},
        ) from exc
    if job is None:
        raise HTTPException(
            status_code=404, detail={"code": "import_not_found", "message": "unknown cost import %s" % import_id}
        )
    try:
        audit_service.append_event(
            settings,
            actor=identity["actor"],
            role=identity["role"],
            action=audit_action,
            resource_type="import_job",
            resource_id=import_id,
            result="success",
        )
    except audit_service.AuditLogError as exc:
        raise HTTPException(
            status_code=500, detail={"code": "audit_unavailable", "message": "审计日志写入失败：%s" % str(exc)[:200]}
        ) from exc
    if action == "resume":
        import_worker.submit_import(import_id, settings)
        _deliver_async(import_id, settings)
        body = {
            "import_id": import_id,
            "status": job.get("status"),
            "retry_count": job.get("retry_count"),
            "resumed_from_checkpoint": bool(import_jobs.resume_position(job)),
            "status_url": "/api/costs/imports/%s" % import_id,
            "request_id": "req-%s" % uuid.uuid4().hex[:12],
            "auth_mode": identity["mode"],
        }
        return JSONResponse(status_code=202, content=body)
    view = cost_service.get_cost_import(import_id, settings) or job
    if isinstance(view, dict):
        view = dict(view)
        view["auth_mode"] = identity["mode"]
    return view


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
