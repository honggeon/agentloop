"""Phase 3/4 routes: external evaluation/observability integrations.

These endpoints expose the Langfuse/promptfoo adapters, the resumable
ImportJob pipeline and the unified external evidence store. Invariants
enforced here and in the core adapters:

- imports are data-only: nothing in an import file is executed, no provider
  or Agent is called, and formal run artifacts are never modified;
- external results never overwrite deterministic checks, judge
  ``overall_status`` or score caps — they are display evidence only;
- import paths are confined to the allowed roots (``resolve_import_path``);
- ``Idempotency-Key``: same key + same fingerprint replays the original
  import job, same key + different fingerprint is a 409;
- imports run through the persisted ImportJob state machine: small dry-runs
  may execute inline (``mode=sync``, HTTP 200), large/HTTP imports run on
  the background worker (``mode=async``, HTTP 202 + ``import_id``);
- errors are redacted and structured (``{code, message, request_id,
  retryable}`` — never internal paths, tokens or raw exception text);
- permission boundary: mutations require the local ``operator``/``admin``
  role (see ``backend.app.auth``); ``auth_mode`` is stated on responses.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from auditrail.integrations import import_jobs

from backend.app import audit_service, auth, import_worker, integration_service
from backend.app.config import get_settings, resolve_run_path

router = APIRouter(prefix="/api")


def _request_id() -> str:
    return "req-%s" % uuid.uuid4().hex[:12]


def _error(status_code: int, code: str, message: str, *, retryable: bool = False) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": str(message)[:300], "request_id": _request_id(), "retryable": retryable},
    )


def _audit(settings, identity, action: str, resource_type: str, resource_id: Optional[str], result: str = "success", reason: Optional[str] = None) -> None:
    """Write the audit event; a failed write is an explicit 500, never silent."""
    try:
        audit_service.append_event(
            settings,
            actor=identity["actor"],
            role=identity["role"],
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            result=result,
            reason=reason,
        )
    except audit_service.AuditLogError as exc:
        raise _error(500, "audit_unavailable", "审计日志写入失败：%s（操作结果已生效或已放弃）" % str(exc)) from exc


def _auth_block(settings) -> dict[str, Any]:
    identity = auth.current_identity(settings)
    return {"auth_mode": identity["mode"], "role": identity["role"]}


def _validate_source(source: str) -> str:
    name = str(source or "").strip().lower()
    if name not in integration_service.IMPORT_SOURCES:
        raise _error(404, "unknown_integration", "unknown integration source %r" % source)
    return name


# ---------------------------------------------------------------------------
# import create (sync compatibility + async ImportJob)
# ---------------------------------------------------------------------------


@router.post("/integrations/{source}/import")
def import_integration(
    source: str,
    payload: dict[str, Any],
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> Any:
    source = _validate_source(source)
    if not isinstance(payload, dict):
        raise _error(400, "invalid_import", "import payload must be an object")
    settings = get_settings()
    identity = auth.require_role(settings, "operator")
    fingerprint = integration_service.job_fingerprint(source, payload)
    request_id = _request_id()

    if idempotency_key:
        existing = integration_service.lookup_import_idempotency(idempotency_key, settings)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise _error(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key was already used with a different import payload",
                )
            job = integration_service.get_import_job(str(existing.get("import_id") or ""), settings)
            if job is None:
                raise _error(
                    409,
                    "idempotency_conflict",
                    "Idempotency-Key references an import that no longer exists",
                )
            body = integration_service.import_report_response(job)
            body["idempotent_replay"] = True
            body.update(_auth_block(settings))
            status_code = 202 if job.get("status") in import_jobs.ACTIVE_STATUSES else 200
            return JSONResponse(status_code=status_code, content=body)

    try:
        job = integration_service.create_import_job(source, payload, settings, actor=identity["actor"])
    except ValueError as exc:
        raise _error(400, "invalid_import", str(exc)) from exc

    import_id = str(job["import_id"])
    _audit(settings, identity, "import_create", "import_job", import_id)
    if idempotency_key:
        integration_service.store_import_idempotency(idempotency_key, fingerprint, import_id, settings)

    if job.get("mode") == "sync":
        finished = import_worker.execute_import_inline(import_id, settings)
        body = integration_service.import_report_response(finished)
        body.update(_auth_block(settings))
        body["request_id"] = request_id
        return body

    import_worker.submit_import(import_id, settings)
    degraded = import_worker.deliver_async(import_id, settings)
    body = {
        "import_id": import_id,
        "status": job.get("status"),
        "mode": "async",
        "queue_degraded": degraded,
        "status_url": "/api/integrations/imports/%s/status" % import_id,
        "request_id": request_id,
    }
    body.update(_auth_block(settings))
    return JSONResponse(status_code=202, content=body)


@router.post("/integrations/{source}/validate")
def validate_integration(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    source = _validate_source(source)
    if not isinstance(payload, dict):
        raise _error(400, "invalid_import", "validate payload must be an object")
    try:
        return integration_service.validate_source(source, payload, get_settings())
    except ValueError as exc:
        raise _error(400, "invalid_import", str(exc)) from exc


# ---------------------------------------------------------------------------
# ImportJob reads and lifecycle control
# ---------------------------------------------------------------------------


@router.get("/integrations/imports")
def list_integration_imports(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    from auditrail.db import StorageUnavailableError

    try:
        return integration_service.list_import_jobs(get_settings(), limit=limit, offset=offset)
    except StorageUnavailableError:
        raise _error(503, "storage_unavailable", "平台元数据存储不可用", retryable=True)
    except (TypeError, ValueError) as exc:
        raise _error(400, "invalid_pagination", str(exc)) from exc


@router.get("/integrations/imports/{import_id}")
def get_integration_import(import_id: str) -> dict[str, Any]:
    from auditrail.db import StorageUnavailableError

    try:
        job = integration_service.get_import_job(import_id, get_settings())
    except StorageUnavailableError:
        raise _error(503, "storage_unavailable", "平台元数据存储不可用", retryable=True)
    if job is None:
        raise _error(404, "import_not_found", "unknown integration import %s" % import_id)
    return job


@router.get("/integrations/imports/{import_id}/status")
def get_integration_import_status(import_id: str) -> dict[str, Any]:
    settings = get_settings()
    job = integration_service.get_import_job(import_id, settings)
    if job is None:
        raise _error(404, "import_not_found", "unknown integration import %s" % import_id)
    return {
        "import_id": job.get("import_id"),
        "status": job.get("status"),
        "phase": job.get("phase"),
        "mode": job.get("mode"),
        "cancel_requested": bool(job.get("cancel_requested")),
        "total_records": job.get("total_records"),
        "processed_records": job.get("processed_records"),
        "imported_records": job.get("imported_records"),
        "duplicate_records": job.get("duplicate_records"),
        "unlinked_records": job.get("unlinked_records"),
        "invalid_records": job.get("invalid_records"),
        "failed_records": job.get("failed_records"),
        "retry_count": job.get("retry_count"),
        "lease_version": job.get("lease_version"),
        "attempt_count": job.get("attempt_count"),
        "max_attempts": job.get("max_attempts"),
        "next_retry_at": job.get("next_retry_at"),
        "pause_requested": bool(job.get("pause_requested")),
        "queue_degraded": bool(job.get("queue_degraded")),
        "checkpoint": job.get("checkpoint"),
        "checkpoint_at": job.get("checkpoint_at"),
        "restart_from_beginning": bool(job.get("restart_from_beginning")),
        "error": job.get("error"),
        "warnings": list(job.get("warnings") or []),
        "worker_id": job.get("worker_id"),
        "heartbeat_at": job.get("heartbeat_at"),
        "lease_expires_at": job.get("lease_expires_at"),
        "stale": import_jobs.job_is_stale(job),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


@router.post("/integrations/imports/{import_id}/cancel")
def cancel_integration_import(import_id: str) -> dict[str, Any]:
    settings = get_settings()
    identity = auth.require_role(settings, "operator")
    try:
        job = import_worker.request_cancel(import_id, settings)
    except import_jobs.ImportJobError as exc:
        raise _error(409, exc.code, str(exc)) from exc
    if job is None:
        raise _error(404, "import_not_found", "unknown integration import %s" % import_id)
    _audit(settings, identity, "import_cancel", "import_job", import_id)
    view = integration_service.get_import_job(import_id, settings) or import_jobs.job_view(job)
    view = dict(view)
    view.update(_auth_block(settings))
    return view


@router.post("/integrations/imports/{import_id}/pause")
def pause_integration_import(import_id: str) -> dict[str, Any]:
    """Cooperative pause: the worker stops at the next batch/page boundary
    (the record in flight may complete) and the lease is released."""
    settings = get_settings()
    identity = auth.require_role(settings, "operator")
    try:
        job = import_worker.request_pause(import_id, settings)
    except import_jobs.ImportJobError as exc:
        raise _error(409, exc.code, str(exc)) from exc
    if job is None:
        raise _error(404, "import_not_found", "unknown integration import %s" % import_id)
    _audit(settings, identity, "import_pause", "import_job", import_id)
    view = integration_service.get_import_job(import_id, settings) or import_jobs.job_view(job)
    view = dict(view)
    view.update(_auth_block(settings))
    return view


@router.post("/integrations/imports/{import_id}/retry")
@router.post("/integrations/imports/{import_id}/resume")
def resume_integration_import(import_id: str) -> Any:
    """Retry/resume a failed/partial/stale job from its checkpoint, or
    re-queue a paused job to continue from it."""
    settings = get_settings()
    identity = auth.require_role(settings, "operator")
    try:
        existing = integration_service.get_import_job(import_id, settings)
        if existing is None:
            raise KeyError(import_id)
        if str(existing.get("status") or "") == "paused":
            job = import_worker.prepare_resume(import_id, settings)
        else:
            job = import_worker.prepare_retry(import_id, settings)
    except KeyError:
        raise _error(404, "import_not_found", "unknown integration import %s" % import_id)
    except import_jobs.ImportJobError as exc:
        status_code = 409 if exc.code == "import_invalid_state" else 400
        raise _error(status_code, exc.code, str(exc), retryable=exc.retryable) from exc
    _audit(settings, identity, "import_resume", "import_job", import_id)
    import_worker.submit_import(import_id, settings)
    degraded = import_worker.deliver_async(import_id, settings)
    body = {
        "import_id": import_id,
        "status": job.get("status"),
        "retry_count": job.get("retry_count"),
        "resumed_from_checkpoint": bool(import_jobs.resume_position(job)),
        "restart_from_beginning": bool(job.get("restart_from_beginning")),
        "queue_degraded": degraded,
        "status_url": "/api/integrations/imports/%s/status" % import_id,
    }
    body.update(_auth_block(settings))
    return JSONResponse(status_code=202, content=body)


# ---------------------------------------------------------------------------
# promptfoo draft review / promotion
# ---------------------------------------------------------------------------


@router.get("/integrations/promptfoo/drafts")
def list_promptfoo_drafts(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    try:
        return integration_service.list_promptfoo_drafts(get_settings(), limit=limit, offset=offset)
    except (TypeError, ValueError) as exc:
        raise _error(400, "invalid_pagination", str(exc)) from exc


@router.get("/integrations/promptfoo/drafts/{draft_id}")
def get_promptfoo_draft(draft_id: str) -> dict[str, Any]:
    if not draft_id or "/" in draft_id or "\\" in draft_id:
        raise _error(400, "invalid_draft_id", "invalid draft id")
    draft = integration_service.get_promptfoo_draft(draft_id, get_settings())
    if draft is None:
        raise _error(404, "draft_not_found", "unknown promptfoo draft %s" % draft_id)
    return {"draft": draft}


@router.post("/integrations/promptfoo/drafts/{draft_id}/approve")
def approve_promptfoo_draft(draft_id: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Promote a draft to a formal TestCase (admin only, idempotent)."""
    settings = get_settings()
    identity = auth.require_role(settings, "admin")
    override = None
    if isinstance(payload, dict) and isinstance(payload.get("input_override"), dict):
        override = payload.get("input_override")
    try:
        result = integration_service.approve_promptfoo_draft(
            draft_id, settings, actor=identity["actor"], input_override=override
        )
    except KeyError:
        raise _error(404, "draft_not_found", "unknown promptfoo draft %s" % draft_id)
    except integration_service.DraftReviewError as exc:
        raise _error(409, exc.code, str(exc)) from exc
    _audit(settings, identity, "promptfoo_draft_approve", "promptfoo_draft", draft_id)
    result.update(_auth_block(settings))
    return result


@router.post("/integrations/promptfoo/drafts/{draft_id}/reject")
def reject_promptfoo_draft(draft_id: str, payload: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    settings = get_settings()
    identity = auth.require_role(settings, "operator")
    reason = payload.get("reason") if isinstance(payload, dict) else None
    try:
        result = integration_service.review_promptfoo_draft(
            draft_id, "rejected", settings, actor=identity["actor"], reason=reason
        )
    except KeyError:
        raise _error(404, "draft_not_found", "unknown promptfoo draft %s" % draft_id)
    except integration_service.DraftReviewError as exc:
        raise _error(409, exc.code, str(exc)) from exc
    _audit(settings, identity, "promptfoo_draft_reject", "promptfoo_draft", draft_id, reason=reason)
    result.update(_auth_block(settings))
    return result


@router.post("/integrations/promptfoo/drafts/{draft_id}/archive")
def archive_promptfoo_draft(draft_id: str) -> dict[str, Any]:
    settings = get_settings()
    identity = auth.require_role(settings, "operator")
    try:
        result = integration_service.review_promptfoo_draft(draft_id, "archived", settings, actor=identity["actor"])
    except KeyError:
        raise _error(404, "draft_not_found", "unknown promptfoo draft %s" % draft_id)
    except integration_service.DraftReviewError as exc:
        raise _error(409, exc.code, str(exc)) from exc
    _audit(settings, identity, "promptfoo_draft_archive", "promptfoo_draft", draft_id)
    result.update(_auth_block(settings))
    return result


# ---------------------------------------------------------------------------
# promptfoo safe export
# ---------------------------------------------------------------------------


@router.get("/integrations/promptfoo/export/{case_id}")
def export_promptfoo_case(case_id: str) -> dict[str, Any]:
    """Safe static export of one TestCase as a promptfoo JSON config."""
    if not case_id or "/" in case_id or "\\" in case_id:
        raise _error(400, "invalid_case_id", "invalid case_id")
    settings = get_settings()
    try:
        payload = integration_service.export_promptfoo_case(case_id, settings)
    except ValueError as exc:
        raise _error(400, "invalid_case", str(exc)) from exc
    if payload is None:
        raise _error(404, "case_not_found", "unknown test case %s" % case_id)
    return payload


# ---------------------------------------------------------- external evidence


def _paged(records: list, limit: int, offset: int, *, max_limit: int = 200) -> dict[str, Any]:
    limit = min(max(int(limit), 1), max_limit)
    offset = max(int(offset), 0)
    return {
        "items": records[offset : offset + limit],
        "total": len(records),
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < len(records),
    }


@router.get("/runs/{run_id}/external-evaluations")
def get_run_external_evaluations(run_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    settings = get_settings()
    try:
        resolve_run_path(run_id, settings)
    except ValueError as exc:
        raise _error(400, "invalid_run_id", str(exc)) from exc
    except FileNotFoundError as exc:
        raise _error(404, "run_not_found", str(exc)) from exc
    payload = integration_service.run_external_evaluations(run_id, settings)
    page = _paged(payload["external_evaluations"], limit, offset)
    payload["external_evaluations"] = page["items"]
    payload["pagination"] = {key: page[key] for key in ("total", "limit", "offset", "has_more")}
    return payload


@router.get("/runs/{run_id}/external-traces")
def get_run_external_traces(run_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    settings = get_settings()
    try:
        resolve_run_path(run_id, settings)
    except ValueError as exc:
        raise _error(400, "invalid_run_id", str(exc)) from exc
    except FileNotFoundError as exc:
        raise _error(404, "run_not_found", str(exc)) from exc
    payload = integration_service.run_external_traces(run_id, settings)
    page = _paged(payload["external_traces"], limit, offset)
    payload["external_traces"] = page["items"]
    payload["pagination"] = {key: page[key] for key in ("total", "limit", "offset", "has_more")}
    return payload


@router.get("/agents/{agent_id}/external-evaluations")
def get_agent_external_evaluations(agent_id: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    if not agent_id or "/" in agent_id or "\\" in agent_id:
        raise _error(400, "invalid_agent_id", "invalid agent_id")
    settings = get_settings()
    payload = integration_service.agent_external_evaluations(agent_id, settings)
    # Phase 4: the evaluation list is paginated server-side; the summary
    # stays a bounded aggregate over the whole record set.
    evaluations = payload.pop("external_evaluations")
    page = _paged(evaluations, limit, offset)
    payload["external_evaluations"] = page["items"]
    payload["pagination"] = {key: page[key] for key in ("total", "limit", "offset", "has_more")}
    return payload
