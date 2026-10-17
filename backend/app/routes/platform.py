"""Phase 0/1 platform routes: Agents, TestCases, TestSuites, Runs and SuiteJobs.

This module only assembles HTTP: validation lives in ``auditrail.netguard`` /
``auditrail.testcases`` / ``auditrail.testsuites``, execution in
``auditrail.runner`` (driven by ``backend.app.run_executor`` and
``backend.app.suite_executor``), and run state in ``auditrail.run_contract``.
Nothing here re-implements collection, audit or judge logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from auditrail import evaluation as evaluation_mod
from auditrail import run_contract
from auditrail import testsuites as suite_mod
from auditrail.netguard import EndpointRejected, validate_endpoint
from auditrail.runner import create_platform_run
from backend.app import agent_overview, platform_store, run_context, run_executor, suite_executor
from backend.app.config import build_endpoint_policy, get_settings, resolve_run_path

router = APIRouter(prefix="/api")


# -------------------------------------------------------------------- agents


@router.get("/agents")
def list_agents() -> dict[str, Any]:
    """Agent configs with additive run stats, plus run-derived agents.

    The original config-list semantics are preserved: ``agents`` keeps every
    existing field and only gains ``derived``/``stats``. Agents visible only
    through run data appear under ``derived_agents`` and are never persisted.
    """
    return agent_overview.list_agents_with_stats(get_settings())


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str) -> dict[str, Any]:
    try:
        detail = agent_overview.get_agent_detail(agent_id, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_agent_id", "message": str(exc)}) from exc
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "agent_not_found", "message": "unknown agent %s" % agent_id},
        )
    return detail


@router.get("/agents/{agent_id}/overview")
def get_agent_overview(agent_id: str, range: str = "7d") -> dict[str, Any]:
    settings = get_settings()
    try:
        range_key = agent_overview.validate_range(range)
        overview = agent_overview.get_agent_overview(agent_id, range_key, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_request", "message": str(exc)}) from exc
    if overview is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "agent_not_found", "message": "unknown agent %s" % agent_id},
        )
    return overview


@router.get("/agents/{agent_id}/runs")
def get_agent_runs(
    agent_id: str,
    range: str = "7d",
    status: str = "all",
    limit: int = agent_overview.DEFAULT_RUN_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    settings = get_settings()
    try:
        range_key = agent_overview.validate_range(range)
        status_filter = agent_overview.validate_status_filter(status)
        result = agent_overview.list_agent_runs(
            agent_id, range_key, status_filter, settings, limit=limit, offset=offset
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_request", "message": str(exc)}) from exc
    if result is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "agent_not_found", "message": "unknown agent %s" % agent_id},
        )
    return result


@router.post("/agents", status_code=201)
def create_agent(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    try:
        endpoint = validate_endpoint(payload.get("endpoint"), build_endpoint_policy(settings))
    except EndpointRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "endpoint_rejected", "message": exc.reason},
        ) from exc
    try:
        agent = platform_store.create_agent({**payload, "endpoint": endpoint}, settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_agent", "message": str(exc)},
        ) from exc
    return {"agent": agent}


@router.put("/agents/{agent_id}")
def update_agent(agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Update editable Agent metadata (Phase 2). ``agent_id``/``created_at``
    are immutable; historical runs keep their own ``agent_ref`` snapshot."""
    settings = get_settings()
    if payload.get("endpoint") is not None:
        try:
            payload = {**payload, "endpoint": validate_endpoint(payload.get("endpoint"), build_endpoint_policy(settings))}
        except EndpointRejected as exc:
            raise HTTPException(
                status_code=400,
                detail={"code": "endpoint_rejected", "message": exc.reason},
            ) from exc
    try:
        agent = platform_store.update_agent(agent_id, payload, settings)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_agent", "message": str(exc)},
        ) from exc
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "agent_not_found", "message": "unknown agent %s" % agent_id},
        )
    return {"agent": agent}


@router.delete("/agents/{agent_id}")
def delete_agent(agent_id: str) -> dict[str, Any]:
    """Delete an Agent config. Historical runs are preserved and keep their
    ``run.json`` snapshot; the agent reappears as derived/missing config."""
    if not platform_store.delete_agent(agent_id, get_settings()):
        raise HTTPException(
            status_code=404,
            detail={"code": "agent_not_found", "message": "unknown agent %s" % agent_id},
        )
    return {"deleted": agent_id}


# --------------------------------------------------------------- test cases


@router.get("/test-cases")
def list_test_cases() -> dict[str, Any]:
    cases, errors = platform_store.list_test_cases_with_errors(get_settings())
    return {"test_cases": cases, "errors": errors}


@router.post("/test-cases", status_code=201)
def create_test_case(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        case = platform_store.create_test_case(payload, get_settings())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_test_case", "message": str(exc)},
        ) from exc
    return {"test_case": case}


@router.get("/test-cases/{case_id}")
def get_test_case(case_id: str) -> dict[str, Any]:
    case = platform_store.get_test_case(case_id, get_settings())
    if case is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "test_case_not_found", "message": "unknown test case %s" % case_id},
        )
    return {"test_case": case}


# --------------------------------------------------------------------- runs


@router.post("/runs")
def create_run(payload: dict[str, Any], idempotency_key: Optional[str] = Header(default=None)):
    settings = get_settings()
    agent_id = str(payload.get("agent_id") or "").strip()
    test_case_id = str(payload.get("test_case_id") or "").strip()
    judge = bool(payload.get("judge"))
    if not agent_id or not test_case_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_request", "message": "agent_id and test_case_id are required"},
        )
    fingerprint = platform_store.run_fingerprint(agent_id, test_case_id, judge)

    if idempotency_key:
        existing = platform_store.lookup_idempotency(idempotency_key, settings)
        if existing:
            if existing.get("fingerprint") != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_conflict", "message": "Idempotency-Key was used with a different request"},
                )
            run_id = str(existing.get("run_id") or "")
            state = _state_for(run_id, settings)
            status_code = 202 if run_contract.is_active_status(state.get("status")) else 200
            return JSONResponse(status_code=status_code, content=_run_accepted(run_id, state))

    agent = platform_store.get_agent(agent_id, settings)
    if agent is None:
        raise HTTPException(status_code=404, detail={"code": "agent_not_found", "message": "unknown agent %s" % agent_id})
    case = platform_store.get_test_case(test_case_id, settings)
    if case is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "test_case_not_found", "message": "unknown test case %s" % test_case_id},
        )
    if case.get("adapter") != "http_session":
        raise HTTPException(
            status_code=400,
            detail={
                "code": "unsupported_adapter",
                "message": "Phase 0 runs support adapter=http_session only, got %s" % case.get("adapter"),
            },
        )
    try:
        validate_endpoint(agent.get("endpoint"), build_endpoint_policy(settings))
    except EndpointRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "endpoint_rejected", "message": exc.reason},
        ) from exc

    run_id = run_contract.new_run_id()
    run_dir = create_platform_run(settings.runs_dir, run_id, agent=agent, test_case=case)
    if idempotency_key:
        platform_store.store_idempotency(idempotency_key, fingerprint, run_id, settings)
    run_executor.submit(
        run_dir,
        agent=agent,
        test_case=case,
        judge=judge,
        policy=build_endpoint_policy(settings),
        judge_runner=_default_judge_runner if judge else None,
    )
    return JSONResponse(status_code=202, content=_run_accepted(run_id, run_contract.load_run_state(run_dir)))


def _run_accepted(run_id: str, state: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "status": state.get("status") or "queued",
        "status_url": "/api/runs/%s/status" % run_id,
    }


def _state_for(run_id: str, settings) -> dict[str, Any]:
    try:
        path = resolve_run_path(run_id, settings)
    except (ValueError, FileNotFoundError):
        return {"status": "unknown"}
    return run_contract.load_run_state(path)


@router.get("/runs/{run_id}/status")
def get_run_status(run_id: str) -> dict[str, Any]:
    path = _run_path_or_400_404(run_id)
    state = run_contract.load_run_state(path)
    subset = run_contract.safe_status_subset(state, stale=run_executor.is_stale(path))
    error = run_contract.read_error(path)
    if error:
        subset["error"] = {
            "phase": error.get("phase"),
            "error_type": error.get("error_type"),
            "retryable": error.get("retryable"),
            "safe_message": error.get("safe_message"),
            "external_status": error.get("external_status"),
            "timestamp": error.get("timestamp"),
        }
    evaluation = evaluation_mod.load_evaluation(path)
    if evaluation is not None:
        subset["evaluation_status"] = evaluation.get("status")
        if evaluation.get("status") == "invalid":
            subset["evaluation_error"] = evaluation.get("error")
    return subset


def _run_path_or_400_404(run_id: str) -> Path:
    try:
        return resolve_run_path(run_id, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_run_id", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": str(exc)}) from exc


@router.get("/runs/{run_id}/context")
def get_run_context(run_id: str) -> dict[str, Any]:
    """Run <-> Agent / SuiteJob / CaseExecution / Attempt linkage (Phase 2).

    ``run.json`` is authoritative for references; disagreements with the
    SuiteJob record are returned as ``warnings`` and never fail the request.
    """
    settings = get_settings()
    try:
        context = run_context.get_run_context(run_id, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_run_id", "message": str(exc)}) from exc
    if context is None:
        raise HTTPException(
            status_code=404, detail={"code": "run_not_found", "message": "unknown run %s" % run_id}
        )
    return context


def _default_judge_runner(run_dir: Path) -> None:
    """Run the default judge engine through the existing judge pipeline."""
    from auditrail.agents.schemas import resolve_judge_report
    from auditrail.agents.tools import write_judge_report
    from auditrail.judges.base import JudgeRequest, resolve_judge_engine

    engine = resolve_judge_engine(None, get_settings())
    result = engine.run(JudgeRequest(run_dir=run_dir, run_id=run_dir.name))
    report = resolve_judge_report(result.proposal, run_dir)
    write_judge_report(
        str(run_dir),
        report,
        judge_metadata={**dict(result.metadata), "engine": result.engine},
        judge_engine=result.engine,
    )


# -------------------------------------------------------------- test suites


@router.get("/test-suites")
def list_test_suites() -> dict[str, Any]:
    return {"test_suites": platform_store.list_test_suites(get_settings())}


@router.post("/test-suites", status_code=201)
def create_test_suite(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        suite = platform_store.create_test_suite(payload, get_settings())
    except ValueError as exc:
        message = str(exc)
        code = "suite_exists" if "already exists" in message else "invalid_test_suite"
        status_code = 409 if code == "suite_exists" else 400
        raise HTTPException(status_code=status_code, detail={"code": code, "message": message}) from exc
    return {"test_suite": suite}


@router.get("/test-suites/{suite_id}")
def get_test_suite(suite_id: str) -> dict[str, Any]:
    settings = get_settings()
    suite = platform_store.get_test_suite(suite_id, settings)
    if suite is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "test_suite_not_found", "message": "unknown test suite %s" % suite_id},
        )
    cases = []
    missing = []
    for case_id in suite.get("case_ids") or []:
        case = platform_store.get_test_case(case_id, settings)
        if case is None:
            missing.append(case_id)
        else:
            cases.append(case)
    return {"test_suite": suite, "test_cases": cases, "missing_case_ids": missing}


@router.put("/test-suites/{suite_id}")
def update_test_suite(suite_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        suite = platform_store.update_test_suite(suite_id, payload, get_settings())
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_test_suite", "message": str(exc)},
        ) from exc
    if suite is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "test_suite_not_found", "message": "unknown test suite %s" % suite_id},
        )
    return {"test_suite": suite}


@router.delete("/test-suites/{suite_id}")
def delete_test_suite(suite_id: str) -> dict[str, Any]:
    if not platform_store.delete_test_suite(suite_id, get_settings()):
        raise HTTPException(
            status_code=404,
            detail={"code": "test_suite_not_found", "message": "unknown test suite %s" % suite_id},
        )
    return {"deleted": suite_id}


# --------------------------------------------------------------- suite jobs


@router.post("/test-suites/{suite_id}/runs")
def create_suite_job(suite_id: str, payload: dict[str, Any], idempotency_key: Optional[str] = Header(default=None)):
    """Launch one batch job (SuiteJob -> CaseExecution -> Attempt N -> Run).

    Phase 1.1: the payload may carry ``max_retries``, ``timeout_seconds``
    (suite-wide deadline) and ``budget`` (``max_attempts`` /
    ``max_total_tokens``); an ``Idempotency-Key`` header replays to the
    original job and conflicts (409) on a different request.
    """
    settings = get_settings()
    suite = platform_store.get_test_suite(suite_id, settings)
    if suite is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "test_suite_not_found", "message": "unknown test suite %s" % suite_id},
        )
    if not suite.get("enabled", True):
        raise HTTPException(
            status_code=400,
            detail={"code": "suite_disabled", "message": "test suite %s is disabled" % suite_id},
        )
    case_ids = list(suite.get("case_ids") or [])
    if not case_ids:
        raise HTTPException(
            status_code=400,
            detail={"code": "empty_suite", "message": "test suite %s has no cases" % suite_id},
        )
    agent_id = str(payload.get("agent_id") or "").strip()
    if not agent_id:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_request", "message": "agent_id is required"},
        )
    judge = bool(payload.get("judge"))
    try:
        options = suite_mod.normalize_job_options(payload)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "invalid_job_options", "message": str(exc)},
        ) from exc
    fingerprint = platform_store.suite_fingerprint(suite_id, suite.get("version"), agent_id, judge, options)

    if idempotency_key:
        existing = platform_store.lookup_suite_idempotency(idempotency_key, settings)
        if existing:
            if existing.get("fingerprint") != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_conflict", "message": "Idempotency-Key was used with a different request"},
                )
            job_id = str(existing.get("job_id") or "")
            record = platform_store.get_suite_job(job_id, settings)
            if record is None:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "idempotency_conflict", "message": "the job for this Idempotency-Key no longer exists"},
                )
            status_code = 202 if suite_mod.is_active_job_status(record.get("status")) else 200
            return JSONResponse(status_code=status_code, content=_job_accepted(job_id, record, replay=True))

    agent = platform_store.get_agent(agent_id, settings)
    if agent is None:
        raise HTTPException(status_code=404, detail={"code": "agent_not_found", "message": "unknown agent %s" % agent_id})
    try:
        validate_endpoint(agent.get("endpoint"), build_endpoint_policy(settings))
    except EndpointRejected as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "endpoint_rejected", "message": exc.reason},
        ) from exc

    snapshots = []
    for case_id in case_ids:
        case = platform_store.get_test_case(case_id, settings)
        snapshots.append(case or {"case_id": case_id, "title": "", "version": None})
    global_limit, agent_limit = suite_executor.limits_for(settings, agent)
    job_id = suite_mod.new_suite_job_id()
    record = suite_mod.new_suite_job(
        job_id,
        suite=suite,
        agent=agent,
        judge=judge,
        case_snapshots=snapshots,
        options=options,
        limits={"global": global_limit, "agent": agent_limit},
    )
    platform_store.save_suite_job(record, settings)
    if idempotency_key:
        platform_store.store_suite_idempotency(idempotency_key, fingerprint, job_id, settings)
    suite_executor.submit(
        job_id,
        settings=settings,
        agent=agent,
        policy=build_endpoint_policy(settings),
        judge_runner=_default_judge_runner if judge else None,
    )
    return JSONResponse(status_code=202, content=_job_accepted(job_id, record, replay=False))


def _job_accepted(job_id: str, record: dict[str, Any], *, replay: bool) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "status": record.get("status") or "queued",
        "status_url": "/api/suite-jobs/%s" % job_id,
        "idempotent_replay": bool(replay),
    }


@router.get("/suite-jobs")
def list_suite_jobs() -> dict[str, Any]:
    settings = get_settings()
    jobs = []
    for record in platform_store.list_suite_jobs(settings):
        jobs.append(_job_summary(record))
    return {"suite_jobs": jobs}


@router.get("/suite-jobs/{job_id}")
def get_suite_job(job_id: str) -> dict[str, Any]:
    settings = get_settings()
    record = platform_store.get_suite_job(job_id, settings)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "suite_job_not_found", "message": "unknown suite job %s" % job_id},
        )
    return _job_payload(record, settings)


@router.get("/suite-jobs/{job_id}/runs")
def get_suite_job_runs(job_id: str) -> dict[str, Any]:
    """All runs of one SuiteJob, grouped by CaseExecution (Phase 2)."""
    payload = run_context.list_job_runs(job_id, get_settings())
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "suite_job_not_found", "message": "unknown suite job %s" % job_id},
        )
    return payload


@router.get("/suite-jobs/{job_id}/attempts/{attempt_id}")
def get_suite_job_attempt(job_id: str, attempt_id: str) -> dict[str, Any]:
    """One attempt plus its CaseExecution retry history (Phase 2)."""
    payload = run_context.get_job_attempt(job_id, attempt_id, get_settings())
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "attempt_not_found", "message": "unknown attempt %s in suite job %s" % (attempt_id, job_id)},
        )
    return payload


@router.post("/suite-jobs/{job_id}/cancel")
def cancel_suite_job(job_id: str) -> dict[str, Any]:
    settings = get_settings()
    record = platform_store.get_suite_job(job_id, settings)
    if record is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "suite_job_not_found", "message": "unknown suite job %s" % job_id},
        )
    if not suite_mod.is_active_job_status(record.get("status")):
        raise HTTPException(
            status_code=409,
            detail={"code": "suite_job_not_cancellable", "message": "suite job %s is already %s" % (job_id, record.get("status"))},
        )
    record = platform_store.request_suite_job_cancel(job_id, settings)
    suite_executor.request_cancel(job_id)
    return _job_payload(record, settings)


@router.post("/suite-jobs/recover")
def recover_suite_jobs() -> dict[str, Any]:
    """Resume queued/running SuiteJobs whose executor is gone (stale jobs).

    In-process and cross-process staleness are handled identically; resumed
    jobs are marked ``recovered`` and continue from their remaining queued
    cases. Returns the ids of jobs that were re-submitted.
    """
    return {"resumed": recover_stale_suite_jobs()}


def recover_stale_suite_jobs() -> list:
    """Startup/manual recovery entry point (also wired to app startup)."""
    settings = get_settings()
    return suite_executor.recover_stale_jobs(
        settings,
        policy=build_endpoint_policy(settings),
        judge_runner=_default_judge_runner,
    )


def _job_summary(record: dict[str, Any]) -> dict[str, Any]:
    agent_ref = record.get("agent_ref") or {}
    return {
        "job_id": record.get("job_id"),
        "status": record.get("status"),
        "suite_ref": record.get("suite_ref"),
        "agent_ref": record.get("agent_ref"),
        # Phase 2 display conveniences; legacy jobs without them stay null.
        "agent_id": agent_ref.get("id"),
        "agent_name": agent_ref.get("name"),
        "agent_environment": agent_ref.get("environment"),
        "judge": bool(record.get("judge")),
        "max_retries": int(record.get("max_retries") or 0),
        "timeout_seconds": record.get("timeout_seconds"),
        "budget": record.get("budget") or {"max_attempts": None, "max_total_tokens": None},
        "budget_used": record.get("budget_used") or {"attempts": 0, "total_tokens": 0},
        "limits": record.get("limits"),
        "recovered": bool(record.get("recovered")),
        "created_at": record.get("created_at"),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "stats": record.get("stats") or suite_mod.compute_job_stats(record),
        "stale": suite_executor.is_stale(record),
    }


def _job_payload(record: dict[str, Any], settings) -> dict[str, Any]:
    attempts = []
    all_attempts = list(record.get("attempts") or [])
    for attempt in all_attempts:
        entry = dict(attempt)
        job_id = str(record.get("job_id") or "")
        order = attempt.get("order")
        entry.setdefault(
            "case_execution_id", suite_mod.case_execution_id_for(job_id, int(order or 0))
        )
        siblings = [
            item
            for item in all_attempts
            if (item.get("order") if item.get("order") is not None else item.get("attempt_id"))
            == (order if order is not None else attempt.get("attempt_id"))
        ]
        final = max(siblings, key=lambda item: int(item.get("retry_index") or 0)) if siblings else attempt
        entry["attempt_count"] = len(siblings)
        entry["is_final_attempt"] = attempt.get("attempt_id") == final.get("attempt_id")
        entry["attempt_history_url"] = "/api/suite-jobs/%s/attempts/%s" % (job_id, attempt.get("attempt_id"))
        run_id = attempt.get("run_id")
        if run_id:
            entry["run_status_url"] = "/api/runs/%s/status" % run_id
            entry["run_context_url"] = "/api/runs/%s/context" % run_id
            _attach_run_evaluation(entry, str(run_id), settings)
        attempts.append(entry)
    payload = _job_summary(record)
    payload.update(
        {
            "status_reason": record.get("status_reason"),
            "cancel_requested": bool(record.get("cancel_requested")),
            "attempts": attempts,
        }
    )
    return {"suite_job": payload}


def _attach_run_evaluation(entry: dict[str, Any], run_id: str, settings) -> None:
    """Expose the deterministic quality of a finished case run, never gating."""
    try:
        run_dir = resolve_run_path(run_id, settings)
    except (ValueError, FileNotFoundError):
        return
    envelope = evaluation_mod.load_evaluation(run_dir)
    if envelope is None:
        return
    entry["evaluation_status"] = envelope.get("status")
    final_quality = envelope.get("final_quality") or {}
    entry["final_quality_status"] = final_quality.get("status")
