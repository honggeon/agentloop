"""Phase 1 suite executor: sequential batch execution of a SuiteJob.

One single-worker executor thread runs one SuiteJob at a time. For each
CaseAttempt (in the TestSuite's defined order) it creates a fresh platform Run
and submits it to the Phase 0 single-run executor, waiting for its terminal
state before starting the next case — a failed case never blocks the rest of
the suite.

Cancellation is cooperative: ``request_cancel`` sets ``cancel_requested`` in
``job.json`` and signals the in-flight run's cancel event, so the running case
finishes as ``cancelled`` and every case that has not started is marked
``skipped``. Completed runs and all their artifacts are always preserved.

Like Phase 0, the in-process ``_ACTIVE`` registry is the source of truth for
"is anyone executing this job"; a queued/running job whose executor is gone is
reported as ``stale`` instead of pretending to progress. There is no automatic
retry and no cross-process recovery in Phase 1.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from auditrail import run_contract
from auditrail import testsuites as suite_mod
from auditrail.runner import create_platform_run

from backend.app import platform_store, run_executor
from backend.app.config import Settings

_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="auditrail-suite")
_ACTIVE: Dict[str, Any] = {}
_CANCEL_EVENTS: Dict[str, threading.Event] = {}
_LOCK = threading.Lock()

_POLL_INTERVAL_SECONDS = 0.05


def submit(
    job_id: str,
    *,
    settings: Settings,
    agent: Dict[str, Any],
    policy,
    judge_runner: Optional[Callable[[Path], None]] = None,
) -> str:
    """Submit one SuiteJob for background execution; returns the job_id."""
    try:
        future = _EXECUTOR.submit(
            _run_job,
            job_id,
            settings=settings,
            agent=agent,
            policy=policy,
            judge_runner=judge_runner,
        )
    except Exception as exc:
        # The executor itself refused the task: the job must not sit queued.
        record = platform_store.get_suite_job(job_id, settings)
        if record is not None:
            suite_mod.skip_pending_attempts(record, reason="executor_start_failed")
            suite_mod.finalize_job(record, "failed", status_reason="executor_start_failed: %s" % type(exc).__name__)
            _save_job(record, settings)
        return job_id
    with _LOCK:
        _ACTIVE[job_id] = future
    future.add_done_callback(lambda _f, jid=job_id: _forget(jid))
    return job_id


def request_cancel(job_id: str) -> None:
    """Signal the in-flight run of an active job to finish as cancelled."""
    with _LOCK:
        event = _CANCEL_EVENTS.get(job_id)
    if event is not None:
        event.set()


def is_active(job_id: str) -> bool:
    with _LOCK:
        future = _ACTIVE.get(job_id)
    return bool(future is not None and not future.done())


def is_stale(record: Dict[str, Any]) -> bool:
    """A queued/running job nobody in this process is executing."""
    if not suite_mod.is_active_job_status(record.get("status")):
        return False
    return not is_active(str(record.get("job_id") or ""))


def shutdown() -> None:
    _EXECUTOR.shutdown(wait=False, cancel_futures=False)


def _forget(job_id: str) -> None:
    with _LOCK:
        _ACTIVE.pop(job_id, None)
        _CANCEL_EVENTS.pop(job_id, None)


def _save_job(record: Dict[str, Any], settings: Settings) -> None:
    """Persist a job record without clobbering a concurrent cancel request.

    The cancel endpoint writes ``cancel_requested`` to ``job.json`` while the
    executor holds an older in-memory copy; always merge the stored flag back
    before writing, or a stale save would silently undo the cancellation.
    """
    stored = platform_store.get_suite_job(str(record.get("job_id") or ""), settings)
    if stored is not None:
        record["cancel_requested"] = bool(record.get("cancel_requested")) or bool(stored.get("cancel_requested"))
    platform_store.save_suite_job(record, settings)


def _run_job(
    job_id: str,
    *,
    settings: Settings,
    agent: Dict[str, Any],
    policy,
    judge_runner: Optional[Callable[[Path], None]],
) -> None:
    record = platform_store.get_suite_job(job_id, settings)
    if record is None:
        return
    try:
        if record.get("cancel_requested"):
            suite_mod.skip_pending_attempts(record, reason="cancelled before start")
            suite_mod.finalize_job(record, "cancelled", status_reason="cancelled before start")
            _save_job(record, settings)
            return
        suite_mod.mark_job_running(record)
        _save_job(record, settings)

        for attempt in list(record.get("attempts") or []):
            record = platform_store.get_suite_job(job_id, settings) or record
            if record.get("cancel_requested"):
                suite_mod.skip_pending_attempts(record, reason="suite job cancelled")
                break
            if attempt.get("status") != "queued":
                continue
            _execute_attempt(record, attempt, settings=settings, agent=agent, policy=policy, judge_runner=judge_runner)
            _save_job(record, settings)

        record = platform_store.get_suite_job(job_id, settings) or record
        suite_mod.skip_pending_attempts(record, reason="suite job cancelled" if record.get("cancel_requested") else "not executed")
        if record.get("cancel_requested"):
            suite_mod.finalize_job(record, "cancelled", status_reason="suite job cancelled")
        else:
            suite_mod.finalize_job(record, "completed")
        _save_job(record, settings)
    except Exception as exc:  # executor defect: the job must be diagnosable
        record = platform_store.get_suite_job(job_id, settings) or record
        suite_mod.skip_pending_attempts(record, reason="executor_interrupted")
        suite_mod.finalize_job(record, "failed", status_reason="executor_interrupted: %s" % run_contract.safe_message_for(exc))
        _save_job(record, settings)


def _execute_attempt(
    record: Dict[str, Any],
    attempt: Dict[str, Any],
    *,
    settings: Settings,
    agent: Dict[str, Any],
    policy,
    judge_runner: Optional[Callable[[Path], None]],
) -> None:
    job_id = str(record["job_id"])
    attempt_id = str(attempt["attempt_id"])
    case = platform_store.get_test_case(str(attempt.get("case_id") or ""), settings)
    if case is None:
        suite_mod.advance_attempt(record, attempt_id, "failed", status_reason="test_case_not_found")
        return
    if case.get("adapter") != "http_session":
        suite_mod.advance_attempt(
            record, attempt_id, "failed", status_reason="unsupported_adapter: %s" % case.get("adapter")
        )
        return

    run_id = run_contract.new_run_id()
    suite_ref = {
        "id": (record.get("suite_ref") or {}).get("id"),
        "version": (record.get("suite_ref") or {}).get("version"),
        "job_id": job_id,
        "attempt_id": attempt_id,
    }
    run_dir = create_platform_run(settings.runs_dir, run_id, agent=agent, test_case=case, suite_ref=suite_ref)
    suite_mod.advance_attempt(record, attempt_id, "running", run_id=run_id)
    _save_job(record, settings)

    cancel_event = threading.Event()
    with _LOCK:
        _CANCEL_EVENTS[job_id] = cancel_event
    if record.get("cancel_requested"):
        cancel_event.set()
    try:
        run_executor.submit(
            run_dir,
            agent=agent,
            test_case=case,
            judge=bool(record.get("judge")),
            policy=policy,
            judge_runner=judge_runner,
            cancel_event=cancel_event,
        )
        final = _wait_for_run(run_dir, job_id=job_id, cancel_event=cancel_event, settings=settings)
    finally:
        with _LOCK:
            if _CANCEL_EVENTS.get(job_id) is cancel_event:
                _CANCEL_EVENTS.pop(job_id, None)
    suite_mod.advance_attempt(
        record,
        attempt_id,
        str(final.get("status") or "failed"),
        status_reason=final.get("status_reason"),
    )


def _wait_for_run(
    run_dir: Path,
    *,
    job_id: str,
    cancel_event: threading.Event,
    settings: Settings,
) -> Dict[str, Any]:
    """Wait for one case run's terminal state, propagating cancel requests."""
    run_id = run_dir.name
    while True:
        record = platform_store.get_suite_job(job_id, settings)
        if record is not None and record.get("cancel_requested"):
            cancel_event.set()
        state = run_contract.read_run_record(run_dir)
        if state is not None and state.get("status") in run_contract.TERMINAL_STATUSES:
            return state
        if not run_executor.is_active(run_id):
            # The executor is gone without a terminal record (executor start
            # failure is handled inside submit, so this is defensive).
            return state or {"status": "failed", "status_reason": "executor_interrupted"}
        time.sleep(_POLL_INTERVAL_SECONDS)
