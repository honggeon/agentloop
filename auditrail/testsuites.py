"""Unified TestSuite model and the SuiteJob -> CaseAttempt -> Run batch contract.

Phase 1 adds sequential batch execution on top of the Phase 0 single-run
platform. This module is a normalization and metadata layer, mirroring
``auditrail.testcases`` (suite configs) and ``auditrail.run_contract``
(``job.json`` lifecycle records); it has no backend or Web app dependencies.

TestSuite config (stored by the platform store):

```json
{
  "suite_id": "suite-001",
  "version": 1,
  "title": "工具恢复回归集",
  "description": "...",
  "case_ids": ["case-001", "case-002"],
  "enabled": true,
  "created_at": "...",
  "updated_at": "..."
}
```

SuiteJob record (``job.json``, one file per job): the job lifecycle is
``queued -> running -> completed|failed|cancelled``; each CaseAttempt reuses
the Run lifecycle statuses (``queued/running/completed/failed/timed_out/
cancelled/skipped``). A single failed attempt never blocks the suite; a cancel
request marks the in-flight attempt ``cancelled`` (cooperatively, via the run
itself) and every attempt that has not started ``skipped``. Completed runs and
their artifacts are always preserved.

Phase 1.1 extends the model to ``SuiteJob -> CaseExecution -> Attempt N ->
Run N``: a retry appends a new attempt (same ``case_id``/``order``, higher
``retry_index``) bound to a *new* Run — old Runs and their evidence are never
overwritten. The job also carries execution controls: ``max_retries``,
``timeout_seconds`` (suite-wide deadline), ``budget`` (execution/cost budget
as ``max_attempts`` / ``max_total_tokens``) and a ``limits`` snapshot of the
concurrency caps in effect at launch.

Status enums are distinct from the judge enums (``success|partial|failed|
unknown``) and must never be mixed with them.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .run_contract import RUN_STATUSES, TERMINAL_STATUSES, utc_now
from .trajectory import write_json

SCHEMA_VERSION = 1

#: Legal values for ``job.json.status`` (SuiteJob lifecycle only).
SUITE_JOB_STATUSES = ("queued", "running", "completed", "failed", "cancelled")
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})

#: CaseAttempt statuses are exactly the Run lifecycle statuses; ``skipped``
#: marks attempts that never started because the job was cancelled.
ATTEMPT_STATUSES = RUN_STATUSES

JOB_JSON = "job.json"

MAX_CASE_IDS = 500

#: Phase 1.1 launch-option caps (server-side ceilings; payloads may go lower).
MAX_JOB_RETRIES = 5
MAX_JOB_TIMEOUT_SECONDS = 86400
MAX_BUDGET_VALUE = 10**9

#: Failure codes that justify an automatic retry: transient agent/executor
#: problems. Validation and definition errors are never retried.
RETRYABLE_FAILURE_CODES = frozenset(
    {
        "agent_timeout",
        "agent_unreachable",
        "agent_http_error",
        "agent_stream_error",
        "invalid_agent_response",
        "executor_interrupted",
        "executor_start_failed",
    }
)


def new_suite_job_id() -> str:
    from .run_contract import new_run_id

    return new_run_id(prefix="suitejob")


def new_attempt_id(order: int, retry_index: int = 0) -> str:
    if retry_index > 0:
        return "attempt-%d-r%d-%s" % (order + 1, retry_index, uuid.uuid4().hex[:8])
    return "attempt-%d-%s" % (order + 1, uuid.uuid4().hex[:8])


def case_execution_id_for(job_id: str, order: int) -> str:
    """Stable CaseExecution id: every attempt of one case (including retries)
    shares it, so a retry chain can always be traced back to one execution."""
    return "%s-case-%d" % (job_id, int(order) + 1)


# ------------------------------------------------------------- suite config


def normalize_test_suite(raw: Any, *, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalize a TestSuite mapping. Raises ``ValueError`` on problems.

    ``existing`` is the previously stored suite on update: ``created_at`` and
    ``suite_id`` are preserved, ``version`` defaults to ``existing + 1`` and
    ``updated_at`` is refreshed.
    """
    if not isinstance(raw, dict):
        raise ValueError("test suite must be an object")
    suite_id = str(raw.get("suite_id") or "").strip()
    if not suite_id or "/" in suite_id or "\\" in suite_id or suite_id in {".", ".."}:
        raise ValueError("suite_id is required and must be a plain identifier")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("test suite %s is missing title" % suite_id)
    description = str(raw.get("description") or "")
    case_ids = raw.get("case_ids")
    if not isinstance(case_ids, list) or not all(isinstance(item, str) for item in case_ids):
        raise ValueError("test suite %s case_ids must be a list of strings" % suite_id)
    case_ids = [item.strip() for item in case_ids]
    if any(not item for item in case_ids):
        raise ValueError("test suite %s case_ids must not contain empty entries" % suite_id)
    if len(case_ids) > MAX_CASE_IDS:
        raise ValueError("test suite %s has too many cases (max %d)" % (suite_id, MAX_CASE_IDS))
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("test suite %s enabled must be a boolean" % suite_id)

    now = utc_now()
    created_at = str((existing or {}).get("created_at") or raw.get("created_at") or now)
    if existing is not None:
        default_version = int(existing.get("version") or 1) + 1
    else:
        default_version = 1
    raw_version = raw.get("version")
    try:
        version = default_version if raw_version is None else int(raw_version)
    except (TypeError, ValueError):
        raise ValueError("test suite %s version must be a positive integer" % suite_id)
    if version <= 0:
        raise ValueError("test suite %s version must be a positive integer" % suite_id)

    return {
        "suite_id": suite_id,
        "version": version,
        "title": title,
        "description": description,
        "case_ids": case_ids,
        "enabled": enabled,
        "created_at": created_at,
        "updated_at": now,
    }


# --------------------------------------------------------------- suite jobs


def normalize_job_options(raw: Any) -> Dict[str, Any]:
    """Normalize Phase 1.1 SuiteJob launch options. Raises ``ValueError``.

    Returned mapping::

        {
          "max_retries": int,                      # per-case retries, 0..MAX_JOB_RETRIES
          "timeout_seconds": float | None,         # suite-wide deadline
          "budget": {"max_attempts": int | None,   # execution budget (incl. retries)
                     "max_total_tokens": int | None}  # cost budget (observed tokens)
        }
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("suite job options must be an object")

    max_retries = raw.get("max_retries", 0)
    if isinstance(max_retries, bool):
        raise ValueError("max_retries must be an integer")
    try:
        max_retries = int(max_retries)
    except (TypeError, ValueError):
        raise ValueError("max_retries must be an integer")
    if not 0 <= max_retries <= MAX_JOB_RETRIES:
        raise ValueError("max_retries must be between 0 and %d" % MAX_JOB_RETRIES)

    timeout_seconds = raw.get("timeout_seconds")
    if timeout_seconds is not None:
        if isinstance(timeout_seconds, bool):
            raise ValueError("timeout_seconds must be a positive number")
        try:
            timeout_seconds = float(timeout_seconds)
        except (TypeError, ValueError):
            raise ValueError("timeout_seconds must be a positive number")
        if not 0 < timeout_seconds <= MAX_JOB_TIMEOUT_SECONDS:
            raise ValueError("timeout_seconds must be between 0 and %d" % MAX_JOB_TIMEOUT_SECONDS)

    budget_raw = raw.get("budget")
    if budget_raw is None:
        budget_raw = {}
    if not isinstance(budget_raw, dict):
        raise ValueError("budget must be an object")
    budget = {}
    for key in ("max_attempts", "max_total_tokens"):
        value = budget_raw.get(key)
        if value is None:
            budget[key] = None
            continue
        if isinstance(value, bool):
            raise ValueError("budget.%s must be a positive integer" % key)
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise ValueError("budget.%s must be a positive integer" % key)
        if not 0 < value <= MAX_BUDGET_VALUE:
            raise ValueError("budget.%s must be between 1 and %d" % (key, MAX_BUDGET_VALUE))
        budget[key] = value

    return {
        "max_retries": max_retries,
        "timeout_seconds": timeout_seconds,
        "budget": budget,
    }


def new_suite_job(
    job_id: str,
    *,
    suite: Dict[str, Any],
    agent: Dict[str, Any],
    judge: bool,
    case_snapshots: List[Dict[str, Any]],
    options: Optional[Dict[str, Any]] = None,
    limits: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a fresh ``queued`` SuiteJob record with one attempt per case.

    ``case_snapshots`` freezes the case id/title/version at launch time, so
    later suite edits never rewrite a running or finished job. ``options`` is
    the normalized Phase 1.1 launch options mapping (see
    :func:`normalize_job_options`); ``limits`` snapshots the concurrency caps
    in effect at launch for display/diagnosis.
    """
    options = normalize_job_options(options)
    attempts = []
    for order, snapshot in enumerate(case_snapshots):
        attempts.append(
            {
                "attempt_id": new_attempt_id(order),
                "case_execution_id": case_execution_id_for(job_id, order),
                "order": order,
                "case_id": str(snapshot.get("case_id") or ""),
                "case_title": str(snapshot.get("title") or ""),
                "case_version": snapshot.get("version"),
                "retry_index": 0,
                "status": "queued",
                "run_id": None,
                "status_reason": None,
                "started_at": None,
                "finished_at": None,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": job_id,
        "status": "queued",
        "status_reason": None,
        "cancel_requested": False,
        "judge": bool(judge),
        "max_retries": options["max_retries"],
        "timeout_seconds": options["timeout_seconds"],
        "budget": options["budget"],
        "budget_used": {"attempts": 0, "total_tokens": 0},
        "limits": dict(limits) if limits else None,
        "recovered": False,
        "suite_ref": {
            "id": suite.get("suite_id"),
            "version": suite.get("version"),
            "title": suite.get("title"),
        },
        "agent_ref": {
            "id": agent.get("agent_id"),
            "name": agent.get("name"),
            "environment": agent.get("environment"),
            "version": agent.get("version"),
            "config_hash": agent.get("config_hash"),
        },
        "attempts": attempts,
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
    }


def validate_job_status(status: Any) -> str:
    text = str(status or "").strip()
    if text not in SUITE_JOB_STATUSES:
        raise ValueError("suite job status must be one of %s, got %r" % (list(SUITE_JOB_STATUSES), status))
    return text


def validate_attempt_status(status: Any) -> str:
    text = str(status or "").strip()
    if text not in ATTEMPT_STATUSES:
        raise ValueError("case attempt status must be one of %s, got %r" % (list(ATTEMPT_STATUSES), status))
    return text


def write_suite_job(path: Path, record: Dict[str, Any]) -> None:
    """Validate and atomically write one ``job.json``."""
    validate_job_status(record.get("status"))
    record = dict(record)
    record["schema_version"] = SCHEMA_VERSION
    record["stats"] = compute_job_stats(record)
    write_json(Path(path), record)


def read_suite_job(path: Path) -> Optional[Dict[str, Any]]:
    """Read one ``job.json``; ``None`` when absent or unparseable."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("status") not in SUITE_JOB_STATUSES:
        return None
    if not isinstance(data.get("attempts"), list):
        return None
    return data


def compute_job_stats(record: Dict[str, Any]) -> Dict[str, int]:
    """Count cases by *final* attempt status; ``total`` is the planned case count.

    With Phase 1.1 retries, several attempts can share one ``order`` (one
    CaseExecution); only the attempt with the highest ``retry_index`` decides
    the case's final status. Earlier attempts still count in ``runs`` (attempts
    that produced a Run) and ``retries`` (attempts with ``retry_index > 0``).
    Skipped/queued cases are counted separately and never folded into
    ``completed`` — pass-rate denominators only include completed cases.
    """
    stats = {"total": 0, "queued": 0, "running": 0, "runs": 0, "retries": 0}
    for status in TERMINAL_STATUSES:
        stats[status] = 0
    finals: Dict[Any, Dict[str, Any]] = {}
    for attempt in record.get("attempts") or []:
        retry_index = int(attempt.get("retry_index") or 0)
        if attempt.get("run_id"):
            stats["runs"] += 1
        if retry_index > 0:
            stats["retries"] += 1
        key = attempt.get("order")
        if key is None:
            key = attempt.get("attempt_id")
        current = finals.get(key)
        if current is None or retry_index >= int(current.get("retry_index") or 0):
            finals[key] = attempt
    for attempt in finals.values():
        status = str(attempt.get("status") or "queued")
        stats["total"] += 1
        stats[status] = stats.get(status, 0) + 1
    return stats


def append_retry_attempt(record: Dict[str, Any], attempt: Dict[str, Any]) -> Dict[str, Any]:
    """Append a fresh ``queued`` retry attempt for a finished attempt's case.

    The retry shares the case identity (``order``/``case_id``) but gets a new
    ``attempt_id`` and will be bound to a *new* Run — the failed attempt and
    its Run stay untouched as evidence.
    """
    retry_index = int(attempt.get("retry_index") or 0) + 1
    order = int(attempt.get("order") or 0)
    retry = {
        "attempt_id": new_attempt_id(order, retry_index),
        "case_execution_id": str(
            attempt.get("case_execution_id") or case_execution_id_for(str(record.get("job_id") or ""), order)
        ),
        "order": order,
        "case_id": str(attempt.get("case_id") or ""),
        "case_title": str(attempt.get("case_title") or ""),
        "case_version": attempt.get("case_version"),
        "retry_index": retry_index,
        "status": "queued",
        "run_id": None,
        "status_reason": None,
        "started_at": None,
        "finished_at": None,
    }
    record.setdefault("attempts", []).append(retry)
    return retry


def is_retryable_failure(
    status: Any,
    status_reason: Any,
    error: Optional[Dict[str, Any]],
) -> bool:
    """Whether a terminal attempt outcome justifies an automatic retry.

    ``timed_out`` is always retryable (the agent may recover); ``failed`` is
    retryable when the run's ``error.json`` says so, or — for attempts without
    a diagnosable error file — when the failure code is a known transient one.
    Cancellation, skips and validation/definition errors are never retried.
    """
    if status == "timed_out":
        return True
    if status != "failed":
        return False
    if isinstance(error, dict) and error.get("retryable") is not None:
        return bool(error.get("retryable"))
    code = str(status_reason or "").split(":", 1)[0].strip()
    return code in RETRYABLE_FAILURE_CODES


def find_attempt(record: Dict[str, Any], attempt_id: str) -> Optional[Dict[str, Any]]:
    for attempt in record.get("attempts") or []:
        if attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


def advance_attempt(
    record: Dict[str, Any],
    attempt_id: str,
    status: str,
    *,
    run_id: Optional[str] = None,
    status_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Advance one attempt's lifecycle in-place (mirrors run status updates)."""
    attempt = find_attempt(record, attempt_id)
    if attempt is None:
        raise KeyError("unknown attempt %s" % attempt_id)
    attempt["status"] = validate_attempt_status(status)
    if status_reason is not None or status in TERMINAL_STATUSES:
        attempt["status_reason"] = status_reason
    if run_id:
        attempt["run_id"] = run_id
    now = utc_now()
    if status == "running" and not attempt.get("started_at"):
        attempt["started_at"] = now
    if status in TERMINAL_STATUSES:
        attempt["finished_at"] = now
    return attempt


def skip_pending_attempts(record: Dict[str, Any], *, reason: str) -> int:
    """Mark every attempt that has not started as ``skipped``; returns count."""
    skipped = 0
    for attempt in record.get("attempts") or []:
        if attempt.get("status") == "queued":
            advance_attempt(record, str(attempt["attempt_id"]), "skipped", status_reason=reason)
            skipped += 1
    return skipped


def mark_job_running(record: Dict[str, Any]) -> None:
    record["status"] = "running"
    if not record.get("started_at"):
        record["started_at"] = utc_now()


def finalize_job(record: Dict[str, Any], status: str, *, status_reason: Optional[str] = None) -> None:
    record["status"] = validate_job_status(status)
    if status_reason is not None or status in TERMINAL_JOB_STATUSES:
        record["status_reason"] = status_reason
    record["finished_at"] = utc_now()


def is_active_job_status(status: Any) -> bool:
    return status in {"queued", "running"}
