"""Judge routes.

Selects one judge engine (``deepagent`` or ``dsh``) and runs it against a
validated run directory. Every engine returns an untrusted proposal; this
module only maps HTTP input to the core engine registry and maps core failures
to HTTP errors. Normalization, evidence validation, deterministic caps, and
report writing always happen inside ``write_judge_report``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from auditrail.agents.schemas import resolve_judge_report
from auditrail.agents.tools import write_judge_report
from auditrail.judges.base import (
    JudgeExecutionError,
    JudgeRequest,
    JudgeUnavailableError,
    describe_engines,
    resolve_judge_engine,
)
from auditrail.judges.proposals import JudgeProposalError
from backend.app.config import build_dsh_judge_settings, get_settings, resolve_run_path

router = APIRouter(prefix="/api")


@router.get("/judge/engines")
def get_judge_engines() -> dict[str, Any]:
    """List selectable judge engines and their availability for the UI."""
    return describe_engines(None, get_settings())


@router.post("/runs/{run_id}/judge")
def run_judge(run_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    settings = get_settings()
    engine_id = _requested_engine(payload)
    engine = _resolve_engine(engine_id, settings)
    # Validate the server-owned DSH judge policy before any judge work starts so
    # a misconfigured judge maps to 503 instead of a mid-run failure.
    _engine_metadata(engine.id, settings)
    request = JudgeRequest(run_dir=path, run_id=run_id)

    try:
        result = engine.run(request)
    except JudgeUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except JudgeProposalError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except JudgeExecutionError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    try:
        report = resolve_judge_report(result.proposal, path)
        report_metadata = {**dict(result.metadata), "engine": result.engine}
        paths = write_judge_report(str(path), report, judge_metadata=report_metadata)
    except Exception as exc:
        raise HTTPException(status_code=500, detail="%s: %s" % (type(exc).__name__, exc)) from exc
    return {
        "status": "ok",
        "engine": result.engine,
        "paths": paths,
        "report": report,
    }


def _requested_engine(payload: dict[str, Any] | None) -> str | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="judge request body must be an object")
    engine = payload.get("engine")
    if engine is None:
        return None
    if not isinstance(engine, str) or not engine.strip():
        raise HTTPException(status_code=400, detail="judge engine must be a non-empty string")
    return engine


def _resolve_engine(engine_id: str | None, settings: Any):
    try:
        return resolve_judge_engine(engine_id, settings)
    except JudgeUnavailableError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _engine_metadata(engine_id: str, settings: Any) -> dict[str, Any]:
    """Collect non-secret engine provenance, failing safely for the DSH judge."""
    metadata: dict[str, Any] = {"engine": engine_id}
    if engine_id != "dsh":
        return metadata
    try:
        judge_settings = build_dsh_judge_settings(settings)
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    metadata["profile"] = judge_settings.dsh_judge_profile
    metadata["workspace"] = str(judge_settings.dsh_judge_workspace)
    return metadata


def _run_path_or_404(run_id: str):
    try:
        return resolve_run_path(run_id, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
