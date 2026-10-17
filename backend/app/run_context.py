"""Phase 2 run linkage reads: Run <-> SuiteJob / CaseExecution / Attempt.

``run.json`` is the authoritative source for a run's platform references; the
SuiteJob record is the authority for attempt state. This module cross-checks
the two and reports inconsistencies as ``warnings`` — linkage problems are
never execution errors, never change the run lifecycle, and a run missing
linkage metadata is simply less connected, not ``failed``.

Legacy compatibility: runs without ``run.json`` (or without ``suite_ref``)
return ``null`` linkage sections and no fabricated suite association; jobs
without ``agent_ref`` display the agent as unknown/derived.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from auditrail import evaluation as evaluation_mod
from auditrail import run_contract
from auditrail import testsuites as suite_mod

from backend.app import platform_store
from backend.app.config import Settings, resolve_run_path


def _safe_run_summary(run_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    try:
        run_dir = resolve_run_path(run_id, settings)
    except (ValueError, FileNotFoundError):
        return None
    state = run_contract.load_run_state(run_dir)
    summary = {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "status_reason": state.get("status_reason"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "legacy_inferred": bool(state.get("legacy_inferred")),
    }
    envelope = evaluation_mod.load_evaluation(run_dir)
    if envelope is not None and envelope.get("status") != "invalid":
        summary["evaluation_status"] = envelope.get("status")
        summary["final_quality_status"] = (envelope.get("final_quality") or {}).get("status")
    return summary


def _agent_section(agent_ref: Optional[Dict[str, Any]], settings: Settings) -> Dict[str, Any]:
    """Agent block for a run: config when still registered, else the snapshot
    carried by ``run.json`` marked as derived/missing-configuration."""
    ref = agent_ref or {}
    agent_id = str(ref.get("id") or "")
    if not agent_id:
        return {"id": None, "name": None, "environment": None, "config_status": "missing", "config": None}
    config = platform_store.get_agent(agent_id, settings)
    if config is None:
        return {
            "id": agent_id,
            "name": str(ref.get("name") or "") or agent_id,
            "environment": ref.get("environment"),
            "config_status": "missing_configuration",
            "config": None,
        }
    return {
        "id": agent_id,
        "name": str(config.get("name") or agent_id),
        "environment": config.get("environment"),
        "config_status": "registered",
        "config": {
            "agent_id": config.get("agent_id"),
            "name": config.get("name"),
            "adapter": config.get("adapter"),
            "environment": config.get("environment"),
            "tags": list(config.get("tags") or []),
            "enabled": bool(config.get("enabled", True)),
        },
    }


def _case_executions(record: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Group a job's attempts into CaseExecutions (one per ``order``)."""
    job_id = str(record.get("job_id") or "")
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    for attempt in record.get("attempts") or []:
        key = attempt.get("order")
        if key is None:
            key = attempt.get("attempt_id")
        groups.setdefault(key, []).append(attempt)
    executions = []
    for key, attempts in groups.items():
        ordered = sorted(attempts, key=lambda item: int(item.get("retry_index") or 0))
        first = ordered[0]
        order = int(first.get("order") or 0)
        executions.append(
            {
                "case_execution_id": str(
                    first.get("case_execution_id") or suite_mod.case_execution_id_for(job_id, order)
                ),
                "order": order,
                "case_id": first.get("case_id"),
                "case_title": first.get("case_title"),
                "attempt_count": len(ordered),
                "attempts": [str(item.get("attempt_id") or "") for item in ordered],
                "final_attempt_id": str(ordered[-1].get("attempt_id") or ""),
                "final_status": ordered[-1].get("status"),
            }
        )
    return sorted(executions, key=lambda item: item["order"])


def _suite_section(
    suite_ref: Optional[Dict[str, Any]],
    settings: Settings,
    warnings: List[Dict[str, str]],
    *,
    run_id: str,
) -> Dict[str, Any]:
    """Suite block for a run: the referenced job's summary plus linkage checks."""
    ref = suite_ref or {}
    job_id = str(ref.get("job_id") or "")
    if not job_id:
        return {"suite_id": None, "job_id": None, "job_status": None, "suite_title": None}
    record = platform_store.get_suite_job(job_id, settings)
    if record is None:
        warnings.append(
            {"code": "suite_job_not_found", "message": "run.json references suite job %s which does not exist" % job_id}
        )
        return {"suite_id": ref.get("id") or ref.get("suite_id"), "job_id": job_id, "job_status": None, "suite_title": None}
    job_suite = record.get("suite_ref") or {}
    run_suite_id = ref.get("id") or ref.get("suite_id")
    if run_suite_id and job_suite.get("id") and run_suite_id != job_suite.get("id"):
        warnings.append(
            {
                "code": "suite_ref_mismatch",
                "message": "run.json suite_ref.suite_id disagrees with the suite job record; run.json is authoritative",
            }
        )
    return {
        "suite_id": run_suite_id or job_suite.get("id"),
        "suite_version": ref.get("version") or ref.get("suite_version") or job_suite.get("version"),
        "suite_title": job_suite.get("title"),
        "job_id": job_id,
        "job_status": record.get("status"),
        "case_execution_id": ref.get("case_execution_id"),
        "attempt_id": ref.get("attempt_id"),
        "retry_index": ref.get("retry_index"),
    }


def _attempt_section(
    suite_ref: Optional[Dict[str, Any]],
    settings: Settings,
    warnings: List[Dict[str, str]],
    *,
    run_id: str,
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """(attempt, case_execution, related_runs) for a suite-bound run."""
    ref = suite_ref or {}
    job_id = str(ref.get("job_id") or "")
    attempt_id = str(ref.get("attempt_id") or "")
    if not job_id or not attempt_id:
        return None, None, []
    record = platform_store.get_suite_job(job_id, settings)
    if record is None:
        return None, None, []
    attempt = suite_mod.find_attempt(record, attempt_id)
    if attempt is None:
        warnings.append(
            {
                "code": "attempt_not_found",
                "message": "run.json references attempt %s which is not in the suite job record" % attempt_id,
            }
        )
        return None, None, []
    if attempt.get("run_id") and str(attempt.get("run_id")) != run_id:
        warnings.append(
            {
                "code": "attempt_run_mismatch",
                "message": "the suite job binds this attempt to a different run; run.json is authoritative",
            }
        )
    order = attempt.get("order")
    siblings = [
        item
        for item in record.get("attempts") or []
        if (item.get("order") if item.get("order") is not None else item.get("attempt_id"))
        == (order if order is not None else attempt_id)
    ]
    siblings.sort(key=lambda item: int(item.get("retry_index") or 0))
    history = [
        {
            "attempt_id": item.get("attempt_id"),
            "run_id": item.get("run_id"),
            "retry_index": int(item.get("retry_index") or 0),
            "status": item.get("status"),
            "is_final_attempt": item is siblings[-1] or item.get("attempt_id") == siblings[-1].get("attempt_id"),
        }
        for item in siblings
    ]
    related = []
    for item in siblings:
        sibling_run = item.get("run_id")
        if not sibling_run or sibling_run == run_id:
            continue
        summary = _safe_run_summary(str(sibling_run), settings) or {"run_id": sibling_run, "status": None}
        summary["attempt_id"] = item.get("attempt_id")
        summary["retry_index"] = int(item.get("retry_index") or 0)
        related.append(summary)
    execution = {
        "case_execution_id": str(
            attempt.get("case_execution_id")
            or suite_mod.case_execution_id_for(job_id, int(attempt.get("order") or 0))
        ),
        "case_id": attempt.get("case_id"),
        "case_title": attempt.get("case_title"),
        "attempt_count": len(siblings),
        "attempt_history": history,
    }
    attempt_view = {
        "attempt_id": attempt.get("attempt_id"),
        "retry_index": int(attempt.get("retry_index") or 0),
        "status": attempt.get("status"),
        "status_reason": attempt.get("status_reason"),
        "run_id": attempt.get("run_id"),
        "started_at": attempt.get("started_at"),
        "finished_at": attempt.get("finished_at"),
        "is_final_attempt": history[-1]["attempt_id"] == attempt.get("attempt_id") if history else True,
    }
    return attempt_view, execution, related


def get_run_context(run_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    """Full linkage context for one run; ``None`` when the run is unknown."""
    try:
        run_dir = resolve_run_path(run_id, settings)
    except ValueError:
        raise
    except FileNotFoundError:
        return None
    state = run_contract.load_run_state(run_dir)
    warnings: List[Dict[str, str]] = []
    suite_ref = state.get("suite_ref") or None
    agent_ref = state.get("agent_ref") or None
    attempt, execution, related = _attempt_section(suite_ref, settings, warnings, run_id=run_id)
    if bool(state.get("legacy_inferred")):
        warnings.append(
            {"code": "legacy_run", "message": "run predates run.json; references are unavailable, not failed"}
        )
    return {
        "run": run_contract.safe_status_subset(state),
        "agent": _agent_section(agent_ref, settings),
        "suite": _suite_section(suite_ref, settings, warnings, run_id=run_id),
        "case_execution": execution,
        "attempt": attempt,
        "related_runs": related,
        "warnings": warnings,
    }


def list_job_runs(job_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    """All runs of one SuiteJob, grouped by CaseExecution with attempt history."""
    record = platform_store.get_suite_job(job_id, settings)
    if record is None:
        return None
    warnings: List[Dict[str, str]] = []
    runs = []
    for attempt in record.get("attempts") or []:
        run_id = attempt.get("run_id")
        entry = {
            "attempt_id": attempt.get("attempt_id"),
            "case_execution_id": str(
                attempt.get("case_execution_id")
                or suite_mod.case_execution_id_for(job_id, int(attempt.get("order") or 0))
            ),
            "case_id": attempt.get("case_id"),
            "case_title": attempt.get("case_title"),
            "retry_index": int(attempt.get("retry_index") or 0),
            "status": attempt.get("status"),
            "run_id": run_id,
            "run": _safe_run_summary(str(run_id), settings) if run_id else None,
        }
        if run_id and entry["run"] is None:
            warnings.append(
                {"code": "run_not_found", "message": "attempt %s references a missing run" % attempt.get("attempt_id")}
            )
        runs.append(entry)
    agent_ref = record.get("agent_ref") or {}
    return {
        "job_id": job_id,
        "status": record.get("status"),
        "suite_ref": record.get("suite_ref"),
        "agent": _agent_section(agent_ref, settings) if agent_ref.get("id") else {
            "id": None,
            "name": None,
            "environment": None,
            "config_status": "unknown",
            "config": None,
        },
        "case_executions": _case_executions(record),
        "runs": runs,
        "warnings": warnings,
    }


def get_job_attempt(job_id: str, attempt_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    """One attempt plus its CaseExecution retry history; ``None`` when unknown."""
    record = platform_store.get_suite_job(job_id, settings)
    if record is None:
        return None
    attempt = suite_mod.find_attempt(record, attempt_id)
    if attempt is None:
        return None
    order = attempt.get("order")
    siblings = [
        item
        for item in record.get("attempts") or []
        if (item.get("order") if item.get("order") is not None else item.get("attempt_id"))
        == (order if order is not None else attempt_id)
    ]
    siblings.sort(key=lambda item: int(item.get("retry_index") or 0))
    history = []
    for item in siblings:
        run_id = item.get("run_id")
        history.append(
            {
                "attempt_id": item.get("attempt_id"),
                "retry_index": int(item.get("retry_index") or 0),
                "status": item.get("status"),
                "status_reason": item.get("status_reason"),
                "run_id": run_id,
                "run": _safe_run_summary(str(run_id), settings) if run_id else None,
                "is_final_attempt": item.get("attempt_id") == siblings[-1].get("attempt_id"),
                "started_at": item.get("started_at"),
                "finished_at": item.get("finished_at"),
            }
        )
    return {
        "job_id": job_id,
        "attempt": {
            "attempt_id": attempt.get("attempt_id"),
            "case_execution_id": str(
                attempt.get("case_execution_id")
                or suite_mod.case_execution_id_for(job_id, int(attempt.get("order") or 0))
            ),
            "case_id": attempt.get("case_id"),
            "case_title": attempt.get("case_title"),
            "retry_index": int(attempt.get("retry_index") or 0),
            "status": attempt.get("status"),
            "status_reason": attempt.get("status_reason"),
            "run_id": attempt.get("run_id"),
            "is_final_attempt": attempt.get("attempt_id") == siblings[-1].get("attempt_id"),
            "started_at": attempt.get("started_at"),
            "finished_at": attempt.get("finished_at"),
        },
        "attempt_history": history,
        "suite_ref": record.get("suite_ref"),
        "agent_ref": record.get("agent_ref"),
    }
