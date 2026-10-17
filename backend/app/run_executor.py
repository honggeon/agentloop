"""Single-run executor for POST /api/runs (Phase 0).

A run is created ``queued`` and immediately submitted to a bounded executor
pool, which atomically advances ``queued -> running`` and finally
``completed|failed|timed_out|cancelled`` (see ``auditrail.runner``). There is
no queue, no auto-retry and no cross-process recovery for standalone runs:
the in-process ``_ACTIVE`` registry is the source of truth for "is anyone
executing this run", and a queued/running record whose executor is gone is
reported as ``stale`` by the status endpoint instead of pretending to
progress. Suite-driven retries and recovery live in
``backend.app.suite_executor`` (Phase 1.1) and always create new Runs.

Executor startup failure itself must still push the run out of ``queued``
into ``failed`` with an ``error.json``.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from auditrail import runner as core_runner
from auditrail.run_contract import read_run_record, update_run_status, write_error

#: Bounded worker pool. Real concurrency is governed by the suite executor's
#: global/per-agent limiters (Phase 1.1); this pool is sized generously so it
#: never becomes the hidden bottleneck, and workers still serialize on the
#: limiters before touching the agent.
_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="auditrail-run")
_ACTIVE: Dict[str, Any] = {}
_LOCK = threading.Lock()


def submit(
    run_dir: Path,
    *,
    agent: Dict[str, Any],
    test_case: Dict[str, Any],
    judge: bool,
    policy,
    judge_runner: Optional[Callable[[Path], None]] = None,
    cancel_event: Optional[Any] = None,
) -> str:
    """Submit one run for background execution; returns the run_id."""
    run_dir = Path(run_dir)
    run_id = run_dir.name
    try:
        future = _EXECUTOR.submit(
            core_runner.execute_run,
            run_dir,
            agent=agent,
            test_case=test_case,
            judge=judge,
            policy=policy,
            judge_runner=judge_runner,
            cancel_event=cancel_event,
        )
    except Exception as exc:
        # The executor itself refused the task: the run must not sit queued.
        write_error(
            run_dir,
            phase="executor",
            error_type="executor_start_failed",
            retryable=True,
            safe_message="executor could not start the run: %s" % type(exc).__name__,
        )
        update_run_status(run_dir, "failed", status_reason="executor_start_failed")
        return run_id
    with _LOCK:
        _ACTIVE[run_id] = future
    future.add_done_callback(lambda _f, rid=run_id: _forget(rid))
    return run_id


def _forget(run_id: str) -> None:
    with _LOCK:
        _ACTIVE.pop(run_id, None)


def is_active(run_id: str) -> bool:
    with _LOCK:
        future = _ACTIVE.get(run_id)
    return bool(future is not None and not future.done())


def is_stale(run_dir: Path) -> bool:
    """A queued/running run nobody in this process is executing (Phase 0 has no
    cross-process recovery, so this marks runs orphaned by a restart)."""
    record = read_run_record(Path(run_dir))
    if not record or record.get("status") not in {"queued", "running"}:
        return False
    return not is_active(str(record.get("run_id") or Path(run_dir).name))


def shutdown() -> None:
    _EXECUTOR.shutdown(wait=False, cancel_futures=False)
