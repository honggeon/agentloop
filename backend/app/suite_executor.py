"""Phase 1.1 suite executor: reliable, bounded batch execution of a SuiteJob.

Each SuiteJob runs on one job thread from a bounded pool. Cases execute in
the TestSuite's defined order; before a case Run starts, the executor acquires
both the global concurrency slot and the per-Agent slot (static caps, no
dynamic scaling), so many jobs may progress concurrently without ever
exceeding the configured ceilings.

Reliability controls (all recorded in ``job.json``):

- **Retry**: a failed/timed-out attempt whose error is retryable gets up to
  ``max_retries`` fresh attempts, each bound to a *new* Run; old Runs and
  their evidence are never overwritten.
- **Run timeout**: each case Run is watched against the case's
  ``timeout_seconds``; on expiry the run is first cancelled cooperatively,
  then forced to ``timed_out`` after a short grace period.
- **Suite timeout**: ``timeout_seconds`` on the job is a suite-wide deadline;
  on expiry the in-flight case is cancelled, pending cases are ``skipped``
  and the job finishes ``failed`` with reason ``suite_timeout``.
- **Budget**: ``budget.max_attempts`` (execution budget, retries included)
  and ``budget.max_total_tokens`` (cost budget over observed LLM tokens)
  stop scheduling new work; pending cases are ``skipped`` and the job
  finishes ``failed`` with reason ``budget_exceeded``.
- **Cancellation**: cooperative, unchanged from Phase 1.

Recovery: ``recover_stale_jobs`` re-submits queued/running jobs whose
executor is gone (process restart). Orphaned in-flight attempts are
reconciled first: a terminal Run's result is adopted, a stale Run is marked
``failed``/``executor_interrupted`` (retryable), and remaining queued cases
continue. Resumed jobs carry ``recovered=true``. There is no cross-machine
scheduling and no distributed lock — recovery is single-deployment only.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from auditrail import run_contract
from auditrail import testsuites as suite_mod
from auditrail.runner import create_platform_run

from backend.app import platform_store, run_executor
from backend.app.config import Settings

_JOB_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="auditrail-suite")
_ACTIVE: Dict[str, Any] = {}
_CANCEL_EVENTS: Dict[str, threading.Event] = {}
_LOCK = threading.Lock()

_POLL_INTERVAL_SECONDS = 0.05
#: Grace period after a run/suite watchdog fires: the stuck run thread first
#: gets a chance to observe the cancel event before its state is forced.
_TIMEOUT_GRACE_SECONDS = 2.0


class _ConcurrencyLimiter:
    """Static global + per-Agent caps on concurrently executing case Runs."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._global_active = 0
        self._agent_active: Dict[str, int] = {}

    def acquire(
        self,
        agent_id: str,
        *,
        global_limit: int,
        agent_limit: int,
        should_abort: Optional[Callable[[], bool]] = None,
    ) -> bool:
        """Block until both slots are free; ``False`` when aborted meanwhile."""
        with self._cond:
            while self._global_active >= global_limit or self._agent_active.get(agent_id, 0) >= agent_limit:
                if should_abort is not None and should_abort():
                    return False
                self._cond.wait(_POLL_INTERVAL_SECONDS)
            self._global_active += 1
            self._agent_active[agent_id] = self._agent_active.get(agent_id, 0) + 1
            return True

    def release(self, agent_id: str) -> None:
        with self._cond:
            self._global_active = max(0, self._global_active - 1)
            remaining = self._agent_active.get(agent_id, 0) - 1
            if remaining > 0:
                self._agent_active[agent_id] = remaining
            else:
                self._agent_active.pop(agent_id, None)
            self._cond.notify_all()


_LIMITER = _ConcurrencyLimiter()


def limits_for(settings: Settings, agent: Dict[str, Any]) -> Tuple[int, int]:
    """Effective (global, per-agent) concurrency caps for one launch."""
    global_limit = max(1, int(getattr(settings, "platform_max_concurrency", 4) or 4))
    agent_limit = agent.get("max_concurrency") or getattr(settings, "platform_agent_max_concurrency", 1) or 1
    return global_limit, max(1, min(64, int(agent_limit)))


def submit(
    job_id: str,
    *,
    settings: Settings,
    agent: Dict[str, Any],
    policy,
    judge_runner: Optional[Callable[[Path], None]] = None,
) -> str:
    """Submit one SuiteJob for background execution; returns the job_id."""
    with _LOCK:
        existing = _ACTIVE.get(job_id)
        if existing is not None and not existing.done():
            return job_id  # already executing (e.g. duplicate recovery trigger)
    global_limit, agent_limit = limits_for(settings, agent)
    try:
        future = _JOB_POOL.submit(
            _run_job,
            job_id,
            settings=settings,
            agent=agent,
            policy=policy,
            judge_runner=judge_runner,
            global_limit=global_limit,
            agent_limit=agent_limit,
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


def recover_stale_jobs(
    settings: Settings,
    *,
    policy=None,
    judge_runner: Optional[Callable[[Path], None]] = None,
) -> list:
    """Resume queued/running jobs whose executor is gone; returns job ids.

    In-process and cross-process staleness look identical to this process, so
    both are handled here. Jobs whose Agent config was deleted cannot resume
    and are finalized ``failed`` instead of pretending to progress.
    """
    resumed = []
    for record in platform_store.list_suite_jobs(settings):
        job_id = str(record.get("job_id") or "")
        if not suite_mod.is_active_job_status(record.get("status")) or is_active(job_id):
            continue
        agent_id = str((record.get("agent_ref") or {}).get("id") or "")
        agent = platform_store.get_agent(agent_id, settings)
        if agent is None:
            suite_mod.skip_pending_attempts(record, reason="recovery_failed: agent_not_found")
            suite_mod.finalize_job(record, "failed", status_reason="recovery_failed: agent_not_found")
            platform_store.save_suite_job(record, settings)
            continue
        record["recovered"] = True
        platform_store.save_suite_job(record, settings)
        submit(
            job_id,
            settings=settings,
            agent=agent,
            policy=policy,
            judge_runner=judge_runner if record.get("judge") else None,
        )
        resumed.append(job_id)
    return resumed


def shutdown() -> None:
    _JOB_POOL.shutdown(wait=False, cancel_futures=False)


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
    global_limit: int,
    agent_limit: int,
) -> None:
    record = platform_store.get_suite_job(job_id, settings)
    if record is None:
        return
    try:
        _reconcile_orphan_attempts(record, settings)
        if record.get("cancel_requested"):
            suite_mod.skip_pending_attempts(record, reason="cancelled before start")
            suite_mod.finalize_job(record, "cancelled", status_reason="cancelled before start")
            _save_job(record, settings)
            return
        suite_mod.mark_job_running(record)
        _save_job(record, settings)
        deadline = _job_deadline(record)

        stop_reason: Optional[str] = None
        index = 0
        while index < len(record.get("attempts") or []):
            attempt = record["attempts"][index]
            index += 1
            if attempt.get("status") != "queued":
                continue
            record = platform_store.get_suite_job(job_id, settings) or record
            if record.get("cancel_requested"):
                suite_mod.skip_pending_attempts(record, reason="suite job cancelled")
                stop_reason = "cancelled"
                break
            if _deadline_exceeded(deadline):
                suite_mod.skip_pending_attempts(record, reason="suite_timeout")
                stop_reason = "suite_timeout"
                break
            if _budget_exhausted(record):
                suite_mod.skip_pending_attempts(record, reason="budget_exceeded")
                stop_reason = "budget_exceeded"
                break
            final, stop = _execute_attempt(
                record,
                attempt,
                settings=settings,
                agent=agent,
                policy=policy,
                judge_runner=judge_runner,
                global_limit=global_limit,
                agent_limit=agent_limit,
                job_deadline=deadline,
            )
            _save_job(record, settings)
            if final is None:
                # Aborted while waiting for a concurrency slot: the attempt
                # stays queued and is handled by the finalize step below.
                stop_reason = stop or "cancelled"
                break
            if stop == "suite_timeout":
                suite_mod.skip_pending_attempts(record, reason="suite_timeout")
                stop_reason = "suite_timeout"
                break
            record = platform_store.get_suite_job(job_id, settings) or record
            # ``record`` was reloaded: rebind the attempt to the fresh object,
            # whose status ``_execute_attempt`` advanced to a terminal state.
            attempt = suite_mod.find_attempt(record, str(attempt.get("attempt_id") or "")) or attempt
            if (
                not record.get("cancel_requested")
                and not _deadline_exceeded(deadline)
                and not _budget_exhausted(record)
                and _should_retry(record, attempt, settings)
            ):
                suite_mod.append_retry_attempt(record, attempt)
                _save_job(record, settings)

        record = platform_store.get_suite_job(job_id, settings) or record
        if record.get("cancel_requested") or stop_reason == "cancelled":
            suite_mod.skip_pending_attempts(record, reason="suite job cancelled")
            suite_mod.finalize_job(record, "cancelled", status_reason="suite job cancelled")
        elif stop_reason == "suite_timeout":
            suite_mod.skip_pending_attempts(record, reason="suite_timeout")
            suite_mod.finalize_job(record, "failed", status_reason="suite_timeout: job exceeded its timeout_seconds")
        elif stop_reason == "budget_exceeded":
            suite_mod.skip_pending_attempts(record, reason="budget_exceeded")
            suite_mod.finalize_job(record, "failed", status_reason="budget_exceeded: job exhausted its budget")
        else:
            suite_mod.skip_pending_attempts(record, reason="not executed")
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
    global_limit: int,
    agent_limit: int,
    job_deadline: Optional[float],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Run one queued attempt to a terminal state.

    Returns ``(final_state, stop_flag)``; ``final_state=None`` means the
    attempt never started (aborted while waiting for a concurrency slot) and
    the job loop must stop. ``stop_flag`` is ``suite_timeout`` when the
    suite-wide deadline fired while this attempt was in flight.
    """
    job_id = str(record["job_id"])
    attempt_id = str(attempt["attempt_id"])
    case = platform_store.get_test_case(str(attempt.get("case_id") or ""), settings)
    if case is None:
        suite_mod.advance_attempt(record, attempt_id, "failed", status_reason="test_case_not_found")
        return {"status": "failed", "status_reason": "test_case_not_found"}, None
    if case.get("adapter") != "http_session":
        reason = "unsupported_adapter: %s" % case.get("adapter")
        suite_mod.advance_attempt(record, attempt_id, "failed", status_reason=reason)
        return {"status": "failed", "status_reason": reason}, None

    agent_id = str(agent.get("agent_id") or "")
    acquired = _LIMITER.acquire(
        agent_id,
        global_limit=global_limit,
        agent_limit=agent_limit,
        should_abort=lambda: _deadline_exceeded(job_deadline) or _job_cancel_requested(job_id, settings),
    )
    if not acquired:
        stop = "suite_timeout" if _deadline_exceeded(job_deadline) else "cancelled"
        return None, stop

    try:
        run_id = run_contract.new_run_id()
        job_suite_ref = record.get("suite_ref") or {}
        suite_id = job_suite_ref.get("id")
        suite_version = job_suite_ref.get("version")
        suite_ref = {
            # ``id``/``version`` are the Phase 1 keys; the explicit aliases
            # make the Phase 2 linkage self-describing. All optional, never
            # inferred from directory names.
            "id": suite_id,
            "suite_id": suite_id,
            "version": suite_version,
            "suite_version": suite_version,
            "job_id": job_id,
            "case_execution_id": str(
                attempt.get("case_execution_id")
                or suite_mod.case_execution_id_for(job_id, int(attempt.get("order") or 0))
            ),
            "attempt_id": attempt_id,
            "retry_index": int(attempt.get("retry_index") or 0),
        }
        run_dir = create_platform_run(settings.runs_dir, run_id, agent=agent, test_case=case, suite_ref=suite_ref)
        suite_mod.advance_attempt(record, attempt_id, "running", run_id=run_id)
        used = record.setdefault("budget_used", {"attempts": 0, "total_tokens": 0})
        used["attempts"] = int(used.get("attempts") or 0) + 1
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
            final, stop = _wait_for_run(
                run_dir,
                job_id=job_id,
                cancel_event=cancel_event,
                settings=settings,
                timeout_seconds=float(case.get("timeout_seconds") or 120),
                job_deadline=job_deadline,
            )
        finally:
            with _LOCK:
                if _CANCEL_EVENTS.get(job_id) is cancel_event:
                    _CANCEL_EVENTS.pop(job_id, None)
        tokens = _run_total_tokens(run_dir)
        if tokens > 0:
            used["total_tokens"] = int(used.get("total_tokens") or 0) + tokens
        reason = final.get("status_reason")
        if stop == "suite_timeout":
            reason = "suite_timeout"
        elif stop == "run_timeout" and not reason:
            reason = "run_timeout"
        suite_mod.advance_attempt(record, attempt_id, str(final.get("status") or "failed"), status_reason=reason)
        return final, stop
    finally:
        _LIMITER.release(agent_id)


def _wait_for_run(
    run_dir: Path,
    *,
    job_id: str,
    cancel_event: threading.Event,
    settings: Settings,
    timeout_seconds: Optional[float] = None,
    job_deadline: Optional[float] = None,
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Wait for one case run's terminal state.

    Propagates job cancel requests into the run, enforces the per-run watchdog
    (``timeout_seconds``) and the suite-wide deadline. Watchdogs first signal
    cooperative cancellation; after ``_TIMEOUT_GRACE_SECONDS`` the run state is
    forced (the runner never resurrects a terminal record, so a late-finishing
    worker cannot overwrite it).
    """
    run_id = run_dir.name
    started = time.monotonic()
    stop: Optional[str] = None
    force_at: Optional[float] = None
    while True:
        record = platform_store.get_suite_job(job_id, settings)
        if record is not None and record.get("cancel_requested"):
            cancel_event.set()
        state = run_contract.read_run_record(run_dir)
        if state is not None and state.get("status") in run_contract.TERMINAL_STATUSES:
            return state, stop
        now = time.monotonic()
        if stop is None and job_deadline is not None and now >= job_deadline:
            stop = "suite_timeout"
            cancel_event.set()
            force_at = now + _TIMEOUT_GRACE_SECONDS
        elif stop is None and timeout_seconds is not None and now - started >= timeout_seconds:
            stop = "run_timeout"
            cancel_event.set()
            force_at = now + _TIMEOUT_GRACE_SECONDS
        if stop is not None and force_at is not None and now >= force_at:
            status = "cancelled" if stop == "suite_timeout" else "timed_out"
            reason = "%s: forced after grace period" % stop
            return _force_terminal(run_dir, status, reason), stop
        if not run_executor.is_active(run_id):
            # The executor is gone without a terminal record (executor start
            # failure is handled inside submit, so this is defensive).
            return state or {"status": "failed", "status_reason": "executor_interrupted"}, stop
        time.sleep(_POLL_INTERVAL_SECONDS)


def _force_terminal(run_dir: Path, status: str, reason: str) -> Dict[str, Any]:
    """Force a stuck run into a terminal state unless it already reached one."""
    state = run_contract.read_run_record(run_dir)
    if state is not None and state.get("status") in run_contract.TERMINAL_STATUSES:
        return state
    if state is None:
        return {"status": status, "status_reason": reason}
    try:
        return run_contract.update_run_status(run_dir, status, status_reason=reason)
    except Exception:
        return {"status": status, "status_reason": reason}


def _reconcile_orphan_attempts(record: Dict[str, Any], settings: Settings) -> None:
    """Resolve attempts left ``running`` by a dead executor (recovery path).

    A terminal Run's result is adopted as-is; a stale Run (nobody executing
    it) is marked ``failed``/``executor_interrupted`` with an ``error.json``
    so the attempt becomes eligible for the job's retry policy.
    """
    for attempt in record.get("attempts") or []:
        if attempt.get("status") != "running":
            continue
        attempt_id = str(attempt["attempt_id"])
        run_id = str(attempt.get("run_id") or "")
        run_dir = _run_dir_for(settings, run_id)
        state = run_contract.read_run_record(run_dir) if run_dir is not None else None
        if state is not None and state.get("status") in run_contract.TERMINAL_STATUSES:
            suite_mod.advance_attempt(record, attempt_id, str(state["status"]), status_reason=state.get("status_reason"))
        elif run_id and run_executor.is_active(run_id):
            continue  # someone in this process is still executing it
        else:
            if state is not None and run_dir is not None:
                run_contract.write_error(
                    run_dir,
                    phase="executor",
                    error_type="executor_interrupted",
                    retryable=True,
                    safe_message="executor restarted while the run was in flight",
                )
                run_contract.update_run_status(run_dir, "failed", status_reason="executor_interrupted")
            suite_mod.advance_attempt(record, attempt_id, "failed", status_reason="executor_interrupted")
            # executor_interrupted is retryable: queue a fresh attempt (new
            # Run) when the job's retry policy allows it.
            if suite_mod.is_retryable_failure("failed", "executor_interrupted", None) and int(
                attempt.get("retry_index") or 0
            ) < int(record.get("max_retries") or 0):
                suite_mod.append_retry_attempt(record, attempt)


def _run_dir_for(settings: Settings, run_id: str) -> Optional[Path]:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in {".", ".."}:
        return None
    run_dir = Path(settings.runs_dir) / run_id
    return run_dir if run_dir.is_dir() else None


def _should_retry(record: Dict[str, Any], attempt: Dict[str, Any], settings: Settings) -> bool:
    if str(attempt.get("status") or "") not in {"failed", "timed_out"}:
        return False
    if int(attempt.get("retry_index") or 0) >= int(record.get("max_retries") or 0):
        return False
    error = None
    run_dir = _run_dir_for(settings, str(attempt.get("run_id") or ""))
    if run_dir is not None:
        error = run_contract.read_error(run_dir)
    return suite_mod.is_retryable_failure(attempt.get("status"), attempt.get("status_reason"), error)


def _budget_exhausted(record: Dict[str, Any]) -> bool:
    budget = record.get("budget") or {}
    used = record.get("budget_used") or {}
    max_attempts = budget.get("max_attempts")
    if max_attempts is not None and int(used.get("attempts") or 0) >= int(max_attempts):
        return True
    max_tokens = budget.get("max_total_tokens")
    if max_tokens is not None and int(used.get("total_tokens") or 0) >= int(max_tokens):
        return True
    return False


def _run_total_tokens(run_dir: Path) -> int:
    """Sum observed LLM tokens for one run; ``0`` when nothing was recorded."""
    path = Path(run_dir) / "llm_calls.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return 0
    entries = data if isinstance(data, list) else (data.get("llm_calls") or data.get("items")) if isinstance(data, dict) else None
    if not isinstance(entries, list):
        return 0
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        usage = entry.get("usage") if isinstance(entry.get("usage"), dict) else {}
        for container in (entry, usage):
            for key in ("totalTokens", "total_tokens", "totalTokensEstimated"):
                value = container.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    total += int(value)
                    break
            else:
                continue
            break
    return total


def _job_deadline(record: Dict[str, Any]) -> Optional[float]:
    """Suite-wide deadline on the monotonic clock, surviving process restarts."""
    timeout = record.get("timeout_seconds")
    if timeout is None:
        return None
    started_at = _parse_timestamp(record.get("started_at"))
    if started_at is None:
        return time.monotonic() + float(timeout)
    elapsed = max(0.0, time.time() - started_at)
    return time.monotonic() + max(0.0, float(timeout) - elapsed)


def _parse_timestamp(text: Any) -> Optional[float]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _deadline_exceeded(deadline: Optional[float]) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _job_cancel_requested(job_id: str, settings: Settings) -> bool:
    record = platform_store.get_suite_job(job_id, settings)
    return bool(record is not None and record.get("cancel_requested"))
