"""Judge routes.

Selects one judge engine (``deepagent`` or ``dsh``) and runs it against a
validated run directory. Every engine returns an untrusted proposal; this
module only maps HTTP input to the core engine registry and maps core failures
to HTTP errors. Normalization, evidence validation, deterministic caps, and
report writing always happen inside ``write_judge_report``.

One run keeps one archived report per engine under ``judges/<engine>/`` with a
``judges/manifest.json`` index. The root ``agent_judge.json``/``agent_judge.md``/
``chart_data.json`` stay the active, backward-compatible report.
"""

from __future__ import annotations

from contextlib import contextmanager
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
from auditrail.judges.reports import (
    JudgeReportLockTimeout,
    JudgeReportSnapshotUnstable,
    compare_judge_reports,
    list_judge_reports,
    normalize_engine_id,
    read_judge_report,
    set_active_judge_report,
)
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
        paths = write_judge_report(
            str(path),
            report,
            judge_metadata=report_metadata,
            judge_engine=result.engine,
        )
    except JudgeProposalError as exc:
        # Structural completeness gate: an incomplete proposal must fail
        # loudly (and never overwrite the active report), not publish shells.
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="%s: %s" % (type(exc).__name__, exc)) from exc
    return {
        "status": "ok",
        "engine": result.engine,
        "paths": paths,
        "report": report,
    }


@router.get("/runs/{run_id}/judges")
def get_run_judges(run_id: str) -> dict[str, Any]:
    """List every stored judge report for the run and which one is active."""
    path = _run_path_or_404(run_id)
    with _report_errors():
        return {"run_id": run_id, **list_judge_reports(path)}


@router.get("/runs/{run_id}/judges/compare")
def get_run_judges_comparison(run_id: str) -> dict[str, Any]:
    """Compare engine reports without picking a winner."""
    path = _run_path_or_404(run_id)
    with _report_errors():
        return {"run_id": run_id, **compare_judge_reports(path)}


@router.get("/runs/{run_id}/judges/{engine}")
def get_run_judge_report(run_id: str, engine: str) -> dict[str, Any]:
    """Read one engine's stored report."""
    path = _run_path_or_404(run_id)
    with _report_errors():
        return read_judge_report(path, engine)


@router.post("/runs/{run_id}/judges/{engine}/active")
def activate_run_judge_report(run_id: str, engine: str) -> dict[str, Any]:
    """Make one stored engine report the active root report again."""
    path = _run_path_or_404(run_id)
    with _report_errors():
        manifest = set_active_judge_report(path, engine)
        return {"run_id": run_id, **manifest}


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


@contextmanager
def _report_errors():
    """Map core judge-report failures to HTTP without leaking internals."""
    try:
        yield
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except JudgeReportLockTimeout as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except JudgeReportSnapshotUnstable as exc:
        raise HTTPException(status_code=503, detail="judge report is being published; retry shortly") from exc


def _run_path_or_404(run_id: str):
    try:
        return resolve_run_path(run_id, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
