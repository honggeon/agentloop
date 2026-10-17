"""Phase 0 platform routes: Agents, TestCases and single asynchronous Runs.

This module only assembles HTTP: validation lives in ``auditrail.netguard`` /
``auditrail.testcases``, execution in ``auditrail.runner`` (driven by
``backend.app.run_executor``), and run state in ``auditrail.run_contract``.
Nothing here re-implements collection, audit or judge logic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from auditrail import evaluation as evaluation_mod
from auditrail import run_contract
from auditrail.netguard import EndpointRejected, validate_endpoint
from auditrail.runner import create_platform_run
from backend.app import platform_store, run_executor
from backend.app.config import build_endpoint_policy, get_settings, resolve_run_path

router = APIRouter(prefix="/api")


# -------------------------------------------------------------------- agents


@router.get("/agents")
def list_agents() -> dict[str, Any]:
    return {"agents": platform_store.list_agents(get_settings())}


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
