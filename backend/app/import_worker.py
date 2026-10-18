"""Phase 4/5 ImportJob worker.

Executes persisted ImportJobs through the configured store
(``backend.app.job_store``):

- ``storage_mode=file``: the Phase 4 in-process executor on the JSON record
  store. Single-process; no cross-process claims are made or implied.
- ``storage_mode=database|dual``: jobs are claimed with database
  transactions and every write is fenced by ``worker_id + lease_version``
  (see ``auditrail.db.job_store``), so API and worker can run as separate
  processes. A retryable failure is requeued with ``next_retry_at`` backoff
  instead of sleeping while holding the lease.

The worker itself:

- checkpoints every ``import_checkpoint_records`` records (and after every
  HTTP page), atomically with the counter carry-over, so a retry/resume
  continues instead of restarting;
- honors the persisted ``cancel_requested``/``pause_requested`` flags
  between read / normalize / link / persist steps — cancellation never
  fabricates unprocessed records as invalid, never rolls back
  already-imported evidence, and a pause stops at the next batch boundary
  (the record in flight may complete);
- classifies errors as retryable or non-retryable and auto-retries only the
  former, with bounded attempts and capped backoff; everything else needs
  an explicit ``retry``/``resume`` call;
- holds a per-job lease; a crashed process leaves an active job whose lease
  expires and is then marked ``stale`` — never ``completed``.

Phase 5 also routes cost imports (``kind="cost_records"``) through this
same state machine, with observed-data priority and price-versioned
estimation preserved (see ``auditrail.costs``).
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from auditrail import costs as cost_mod
from auditrail.integrations import external_models as ext
from auditrail.integrations import import_jobs
from auditrail.integrations import langfuse as langfuse_adapter
from auditrail.integrations import promptfoo as promptfoo_adapter

from backend.app import integration_service
from backend.app import job_store as job_store_mod
from backend.app.config import Settings, get_settings

_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="auditrail-import")
_LOCK = threading.RLock()
_ACTIVE: Dict[str, Future] = {}
_CANCEL_EVENTS: Dict[str, threading.Event] = {}
#: Test-only fetcher injection for HTTP imports (never a real network default).
_FETCHERS: Dict[str, Any] = {}


def register_import_fetcher(import_id: str, fetcher: Any) -> None:
    """Register a fake HTTP fetcher for one import job (tests only)."""
    with _LOCK:
        _FETCHERS[import_id] = fetcher


def _peek_fetcher(import_id: str) -> Any:
    """Fetchers stay registered for the whole job run (retries included);
    ``_run_job_guarded`` drops the registration when the run ends."""
    with _LOCK:
        return _FETCHERS.get(import_id)


def _store(settings: Settings) -> job_store_mod.JobStore:
    return job_store_mod.job_store(settings)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def start_import_worker(settings: Optional[Settings] = None) -> List[str]:
    """Startup hook: mark expired-lease jobs stale (never completed) and
    return their ids. Does not auto-resume anything — resume is explicit.
    Every stale transition is audited."""
    current = settings or get_settings()
    if not current.import_worker_enabled:
        return []
    stale_ids = _store(current).refresh_stale()
    if stale_ids:
        from backend.app import audit_service, metrics

        for import_id in stale_ids:
            try:
                audit_service.append_event(
                    current,
                    actor="system-worker",
                    role="system",
                    action="import_stale",
                    resource_type="import_job",
                    resource_id=import_id,
                    result="success",
                    reason="worker lease expired; marked stale (never auto-completed)",
                )
            except Exception:
                pass  # audit failure must not block stale recovery
            metrics.counter_inc(
                "auditrail_import_job_stale_total", _help="ImportJobs marked stale", mode="recovery"
            )
    return stale_ids


def stop_import_worker() -> None:
    """Shutdown hook: signal cancellations and stop accepting work."""
    with _LOCK:
        events = list(_CANCEL_EVENTS.values())
        _ACTIVE.clear()
        _CANCEL_EVENTS.clear()
    for event in events:
        event.set()


def is_active(import_id: str) -> bool:
    with _LOCK:
        future = _ACTIVE.get(import_id)
    return bool(future is not None and not future.done())


def deliver_async(import_id: str, settings: Settings) -> bool:
    """Best-effort broker hint for an async job; returns True when degraded.

    In ``queue_mode=redis`` a broker outage marks the job ``queue_degraded``
    (delivery is never faked); the database row stays the queued source of
    truth and the worker's poll still picks the job up. Other queue modes
    need no hint and never degrade.
    """
    from backend.app import queue_adapter

    try:
        queue_adapter.enqueue_hint(import_id, settings)
        return False
    except queue_adapter.QueueUnavailableError:
        store = _store(settings)

        def mark(job: Dict[str, Any]) -> Dict[str, Any]:
            job = dict(job)
            job["queue_degraded"] = True
            return job

        store.mutate(import_id, mark)
        return True


# ---------------------------------------------------------------------------
# submission / control
# ---------------------------------------------------------------------------


def submit_import(
    import_id: str,
    settings: Settings,
    *,
    fetcher: Optional[Any] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> str:
    """Queue an existing persisted job for async execution (idempotent).

    In ``file`` storage mode this drives the in-process pool. In database
    modes the queued row is the submission: an embedded or standalone
    worker claims it transactionally, so this is a no-op beyond fetcher
    registration.
    """
    if fetcher is not None:
        register_import_fetcher(import_id, fetcher)
    if job_store_mod.storage_mode(settings) != "file":
        return import_id
    with _LOCK:
        existing = _ACTIVE.get(import_id)
        if existing is not None and not existing.done():
            return import_id
        event = threading.Event()
        _CANCEL_EVENTS[import_id] = event
        future = _POOL.submit(_run_job_guarded, import_id, settings, event, sleeper, False)
        _ACTIVE[import_id] = future
    return import_id


def execute_import_inline(
    import_id: str,
    settings: Settings,
    *,
    fetcher: Optional[Any] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> Dict[str, Any]:
    """Run a job synchronously (small/dry-run compatibility path)."""
    if fetcher is not None:
        register_import_fetcher(import_id, fetcher)
    event = threading.Event()
    return _run_job_guarded(import_id, settings, event, sleeper, False)


def request_cancel(import_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    """Persist the cooperative cancel flag and signal any in-process worker.

    Idempotent: repeated cancels return the current job. Terminal jobs raise
    ``ImportJobError(import_invalid_state)``. A queued job no worker holds
    is finalized ``cancelled`` immediately, atomically with the flag write;
    already-written evidence is never rolled back.
    """
    store = _store(settings)

    def apply(job: Dict[str, Any]) -> Dict[str, Any]:
        updated = import_jobs.request_cancel(job)
        if str(updated.get("status") or "") in ("queued", "validating") and not updated.get("worker_id"):
            updated = import_jobs.transition(
                updated,
                "cancelled",
                error={"code": "import_cancelled", "message": "cancelled before execution", "retryable": False},
            )
        return updated

    updated = store.mutate(import_id, apply)
    if updated is None:
        return None
    with _LOCK:
        event = _CANCEL_EVENTS.get(import_id)
    if event is not None:
        event.set()
    elif (
        store.mode == "file"
        and not is_active(import_id)
        and str(updated.get("status") or "") in ("queued", "validating")
    ):
        # File mode, no in-process worker holds this job (queued before
        # submission or the pool is busy): finalize the cancel now so the
        # job never lingers in an active status with the flag set.
        def finalize(job: Dict[str, Any]) -> Dict[str, Any]:
            if str(job.get("status") or "") in ("queued", "validating"):
                return import_jobs.transition(
                    job,
                    "cancelled",
                    error={"code": "import_cancelled", "message": "cancelled before execution", "retryable": False},
                )
            return job

        finalized = store.mutate(import_id, finalize)
        if finalized is not None:
            return finalized
    return updated


def request_pause(import_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    """Persist the cooperative pause flag (boundary stop).

    Running jobs pause at the next batch/page boundary; queued jobs move to
    ``paused`` immediately. Terminal/stale jobs raise
    ``ImportJobError(import_invalid_state)``.
    """
    store = _store(settings)

    def apply(job: Dict[str, Any]) -> Dict[str, Any]:
        return import_jobs.request_pause(job)

    return store.mutate(import_id, apply)


def prepare_resume(import_id: str, settings: Settings) -> Dict[str, Any]:
    """Re-queue a paused job to continue from its checkpoint."""
    store = _store(settings)
    updated = store.mutate(import_id, import_jobs.prepare_resume)
    if updated is None:
        raise KeyError(import_id)
    return updated


def prepare_retry(import_id: str, settings: Settings) -> Dict[str, Any]:
    """Requeue a failed/partial/stale job to resume from its checkpoint.

    Raises ``ImportJobError(import_invalid_state)`` for any other status and
    when the bounded retry count is exhausted. Never deletes imported
    evidence and never clears the checkpoint (unless the source declared
    ``restart_from_beginning``).
    """
    store = _store(settings)

    def apply(job: Dict[str, Any]) -> Dict[str, Any]:
        status = str(job.get("status") or "")
        if status not in import_jobs.RESUMABLE_STATUSES:
            raise import_jobs.ImportJobError(
                "import_invalid_state",
                "only failed/partial/stale import jobs can be retried (current: %s)" % status,
                retryable=False,
            )
        if int(job.get("retry_count") or 0) >= int(settings.import_max_retries):
            raise import_jobs.ImportJobError(
                "import_invalid_state",
                "import job exhausted the maximum retry count (%d)" % int(settings.import_max_retries),
                retryable=False,
            )
        job = dict(job)
        job["retry_count"] = int(job.get("retry_count") or 0) + 1
        return import_jobs.transition(job, "queued", phase="validating")

    updated = store.mutate(import_id, apply)
    if updated is None:
        raise KeyError(import_id)
    return updated


# ---------------------------------------------------------------------------
# execution core
# ---------------------------------------------------------------------------


def _run_job_guarded(
    import_id: str,
    settings: Settings,
    cancel_event: threading.Event,
    sleeper: Optional[Callable[[float], None]],
    requeue_on_retry: bool,
) -> Dict[str, Any]:
    try:
        return _run_job(import_id, settings, cancel_event, sleeper, requeue_on_retry)
    finally:
        with _LOCK:
            _ACTIVE.pop(import_id, None)
            _CANCEL_EVENTS.pop(import_id, None)
            _FETCHERS.pop(import_id, None)


def _load_owned_job(
    import_id: str,
    settings: Settings,
    worker_id: str,
    store: job_store_mod.JobStore,
) -> Dict[str, Any]:
    """Reload the job and verify this worker still owns the lease."""
    job = store.load(import_id)
    if job is None:
        raise import_jobs.ImportJobError("import_not_found", "import job vanished", retryable=False)
    import_jobs.assert_worker_owner(job, worker_id)
    return job


def _persist(
    job: Dict[str, Any],
    settings: Settings,
    worker_id: str,
    store: job_store_mod.JobStore,
) -> Dict[str, Any]:
    return store.save(job, worker_id=worker_id)


def _run_job(
    import_id: str,
    settings: Settings,
    cancel_event: threading.Event,
    sleeper: Optional[Callable[[float], None]],
    requeue_on_retry: bool,
) -> Dict[str, Any]:
    store = _store(settings)
    worker_id = import_jobs.new_worker_id()
    job = store.claim(import_id, worker_id, lease_seconds=int(settings.import_lease_seconds))
    if job is None:
        raise import_jobs.ImportJobError(
            "import_not_found", "unknown import job or live lease held by another worker", retryable=False
        )
    if str(job.get("status") or "") in import_jobs.TERMINAL_STATUSES:
        return job  # idempotent: never re-execute a terminal job
    return _drive(job, settings, cancel_event, sleeper, requeue_on_retry, store)


def execute_claimed_job(
    job: Dict[str, Any],
    settings: Settings,
    *,
    cancel_event: Optional[threading.Event] = None,
    sleeper: Optional[Callable[[float], None]] = None,
) -> Dict[str, Any]:
    """Drive a job this process already claimed (cross-process worker loop).

    The claim (transaction + fencing token) happened in the caller; this
    entry owns execution, checkpointing and the terminal write under that
    lease. Retryable failures are requeued with backoff (the lease is
    released; no worker sleeps while holding a claim).
    """
    store = _store(settings)
    return _drive(
        dict(job),
        settings,
        cancel_event or threading.Event(),
        sleeper,
        True,
        store,
    )


def _drive(
    job: Dict[str, Any],
    settings: Settings,
    cancel_event: threading.Event,
    sleeper: Optional[Callable[[float], None]],
    requeue_on_retry: bool,
    store: job_store_mod.JobStore,
) -> Dict[str, Any]:
    import_id = str(job["import_id"])
    worker_id = str(job.get("worker_id") or "")
    # Backoff sleeps are cancel-aware: a cancel request interrupts the wait
    # immediately instead of being noticed only after the backoff elapsed.
    sleep = sleeper or cancel_event.wait
    backoff_attempts = 0
    max_retries = int(settings.import_max_retries)
    while True:
        try:
            return _execute(import_id, settings, worker_id, cancel_event, store)
        except import_jobs.StaleWorkerError:
            raise  # another worker owns the job now; never touch its state
        except (import_jobs.ImportJobError, langfuse_adapter.LangfuseSourceError) as exc:
            code = str(getattr(exc, "code", "invalid_import"))
            retryable = bool(getattr(exc, "retryable", False))
            message = str(exc)[:300]
            if code == "import_cancelled":
                job = _load_owned_job(import_id, settings, worker_id, store)
                job = import_jobs.transition(
                    job, "cancelled", error={"code": code, "message": message, "retryable": False}
                )
                return _persist(job, settings, worker_id, store)
            if code == "import_paused":
                job = _load_owned_job(import_id, settings, worker_id, store)
                job = import_jobs.transition(job, "paused")
                return _persist(job, settings, worker_id, store)
            if retryable and backoff_attempts < max_retries:
                backoff_attempts += 1
                job = _load_owned_job(import_id, settings, worker_id, store)
                job = dict(job)
                job["error"] = {"code": code, "message": message, "retryable": True}
                if requeue_on_retry and store.mode == "database":
                    # Release the lease and let the scheduler re-drive after
                    # the backoff — no worker holds a claim while sleeping.
                    delay = min(2.0**backoff_attempts, import_jobs.MAX_BACKOFF_SECONDS)
                    return store.requeue_retryable(job, worker_id, delay_seconds=delay)
                job["retry_count"] = int(job.get("retry_count") or 0) + 1
                job = store.heartbeat(job, worker_id, lease_seconds=int(settings.import_lease_seconds))
                _persist(job, settings, worker_id, store)
                sleep(min(2.0**backoff_attempts, import_jobs.MAX_BACKOFF_SECONDS))
                continue
            job = _load_owned_job(import_id, settings, worker_id, store)
            job = import_jobs.transition(
                job, "failed", error={"code": code, "message": message, "retryable": retryable}
            )
            return _persist(job, settings, worker_id, store)
        except Exception as exc:  # noqa: BLE001 - redacted type-only message
            job = _load_owned_job(import_id, settings, worker_id, store)
            job = import_jobs.transition(
                job,
                "failed",
                error={
                    "code": "internal_error",
                    "message": "import failed: %s" % type(exc).__name__,
                    "retryable": False,
                },
            )
            return _persist(job, settings, worker_id, store)


def _check_cancel(
    import_id: str,
    settings: Settings,
    worker_id: str,
    cancel_event: threading.Event,
    store: job_store_mod.JobStore,
    *,
    disk: bool = False,
) -> None:
    """Cooperative cancel/pause check. The in-memory event is consulted at
    every record; the persisted flags are re-read at batch/page boundaries
    (``disk=True``) so a control request issued from another process lands
    too."""
    if cancel_event.is_set():
        raise import_jobs.ImportJobError("import_cancelled", "import job cancellation was requested", retryable=False)
    if disk:
        job = _load_owned_job(import_id, settings, worker_id, store)
        if job.get("cancel_requested"):
            raise import_jobs.ImportJobError(
                "import_cancelled", "import job cancellation was requested", retryable=False
            )
        if job.get("pause_requested"):
            raise import_jobs.ImportJobError("import_paused", "import job pause was requested", retryable=False)


def _execute(
    import_id: str,
    settings: Settings,
    worker_id: str,
    cancel_event: threading.Event,
    store: job_store_mod.JobStore,
) -> Dict[str, Any]:
    job = _load_owned_job(import_id, settings, worker_id, store)
    if str(job.get("status") or "") == "validating":
        job = import_jobs.transition(job, "running", phase="reading")
    else:
        job = dict(job)
        job["phase"] = "reading"
    _persist(job, settings, worker_id, store)

    source_system = str(job.get("source_system") or "")
    kind = str(job.get("kind") or "")
    if kind == "cost_records":
        job = _execute_costs(job, settings, worker_id, cancel_event, store)
    elif source_system == "langfuse":
        job = _execute_langfuse(job, settings, worker_id, cancel_event, store)
    elif source_system == "promptfoo":
        job = _execute_promptfoo(job, settings, worker_id, cancel_event, store)
    else:
        raise import_jobs.ImportJobError(
            "invalid_import", "unsupported import source %r" % source_system, retryable=False
        )

    _check_cancel(import_id, settings, worker_id, cancel_event, store, disk=True)
    return _finalize(job, settings, worker_id, store)


def _finalize(
    job: Dict[str, Any],
    settings: Settings,
    worker_id: str,
    store: job_store_mod.JobStore,
) -> Dict[str, Any]:
    """Build the Phase 3-compatible report and land the terminal status."""
    job = _load_owned_job(str(job["import_id"]), settings, worker_id, store)
    job = dict(job)
    job["phase"] = "finalizing"

    if str(job.get("kind") or "") == "cost_records":
        report = _cost_report(job)
    else:
        report = ext.new_import_report(str(job.get("source_system") or ""), dry_run=bool(job.get("dry_run")))
        report["import_id"] = str(job["import_id"])
        report["kind"] = job.get("kind")
        report["total_records"] = int(job.get("processed_records") or 0)
        report["duplicate_records"] = int(job.get("duplicate_records") or 0)
        report["unlinked_records"] = int(job.get("unlinked_records") or 0)
        report["invalid_records"] = int(job.get("invalid_records") or 0)
        report["imported_records"] = int(job.get("imported_records") or 0)
        report["warnings"] = list(job.get("warnings") or [])[: ext.MAX_IMPORT_ERRORS]
        report["errors"] = list(job.get("errors") or [])[: ext.MAX_IMPORT_ERRORS]
        if str(job.get("source_system") or "") == "promptfoo":
            report["draft_count"] = int(job.get("processed_records") or 0) - int(job.get("invalid_records") or 0)
        ext.finalize_import_report(report)

    job["report"] = report
    job["total_records"] = int(job.get("processed_records") or 0)
    if job.get("dry_run"):
        status = "completed"
    elif str(report.get("status") or "") in ("partial", "completed_with_errors"):
        status = "partial"
    elif str(report.get("status") or "") == "failed":
        status = "failed"
    else:
        status = "completed"
    job = import_jobs.transition(job, status)
    persisted = _persist(job, settings, worker_id, store)
    try:
        from backend.app import metrics

        metrics.record_import_finished(persisted)
    except Exception:
        pass  # metrics must never fail an import
    # Cost imports keep the legacy report directory readable: the same
    # report is mirrored to ``cost_imports/<import_id>.json``.
    if str(job.get("kind") or "") == "cost_records" and not job.get("dry_run"):
        try:
            cost_mod.save_import_report(Path(settings.platform_dir), report)
        except Exception:
            pass  # the job record is authoritative; the mirror is compat
    # Phase 4: hint the (optional, disabled-by-default) run-index watcher so
    # cost-bearing index entries refresh with reason=import_complete.
    try:
        from backend.app import run_index_watch

        run_index_watch.notify_import_complete(settings)
    except Exception:
        pass  # the watcher is strictly optional; a hint failure never fails an import
    return persisted


def _cost_report(job: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy-shaped cost import report (field compatibility with Phase 2)."""
    report = {
        "schema_version": cost_mod.SCHEMA_VERSION,
        "import_id": str(job["import_id"]),
        "status": "completed",
        "source_system": str(job.get("source_system") or ""),
        "kind": "cost_records",
        "source_file": job.get("source_file_name"),
        "dry_run": bool(job.get("dry_run")),
        "total_records": int(job.get("processed_records") or 0),
        "imported_records": int(job.get("imported_records") or 0),
        "duplicate_records": int(job.get("duplicate_records") or 0),
        "invalid_records": int(job.get("invalid_records") or 0),
        "errors": list(job.get("errors") or [])[: cost_mod.MAX_IMPORT_ERRORS],
        "warnings": list(job.get("warnings") or [])[: cost_mod.MAX_IMPORT_ERRORS],
        "imported_at": job.get("finished_at") or job.get("started_at"),
    }
    if report["invalid_records"]:
        report["status"] = "completed_with_errors" if report["imported_records"] else "failed"
    if job.get("dry_run"):
        report["status"] = "dry_run"
    return report


# ---------------------------------------------------------------------------
# shared batch plumbing
# ---------------------------------------------------------------------------


def _flush(
    job: Dict[str, Any],
    settings: Settings,
    worker_id: str,
    cancel_event: threading.Event,
    store: job_store_mod.JobStore,
    *,
    position: Dict[str, Any],
    merge: Optional[Callable[[], int]] = None,
) -> Dict[str, Any]:
    """Persist one batch: merge records (unless dry-run), then checkpoint.

    Counters accumulate in memory between flushes; each flush re-reads the
    stored job (ownership + cancel/pause check), carries the counters over,
    merges the batch idempotently and writes the checkpoint atomically.
    """
    _check_cancel(str(job["import_id"]), settings, worker_id, cancel_event, store, disk=True)
    on_disk = _load_owned_job(str(job["import_id"]), settings, worker_id, store)
    duplicates = 0
    dry_run = bool(on_disk.get("dry_run"))
    if not dry_run and merge is not None:
        # merge() may append warnings to the in-memory job; run it before the
        # counter/warning carry-over so nothing is lost.
        duplicates = merge()
    merged_job = dict(on_disk)
    for key in (
        "processed_records",
        "imported_records",
        "duplicate_records",
        "unlinked_records",
        "invalid_records",
        "failed_records",
        "warnings",
        "errors",
        "total_records",
        "source_file_name",
    ):
        if key in job:
            merged_job[key] = job.get(key)
    merged_job["phase"] = "persisting"
    if duplicates:
        merged_job["duplicate_records"] = int(merged_job.get("duplicate_records") or 0) + duplicates
        merged_job["imported_records"] = max(int(merged_job.get("imported_records") or 0) - duplicates, 0)
    merged_job = import_jobs.update_checkpoint(merged_job, **position)
    merged_job = store.heartbeat(merged_job, worker_id, lease_seconds=int(settings.import_lease_seconds))
    return _persist(merged_job, settings, worker_id, store)


# ---------------------------------------------------------------------------
# langfuse execution (file + http), batched and checkpointed
# ---------------------------------------------------------------------------


def _execute_langfuse(
    job: Dict[str, Any],
    settings: Settings,
    worker_id: str,
    cancel_event: threading.Event,
    store: job_store_mod.JobStore,
) -> Dict[str, Any]:
    platform_dir = Path(settings.platform_dir)
    import_id = str(job["import_id"])
    kind = str(job.get("kind") or "traces")
    payload = integration_service.load_import_payload(import_id, settings) or {}
    association = payload.get("association") if isinstance(payload.get("association"), dict) else {}
    context = integration_service.build_link_context(settings)
    trace_lookup = None
    if kind == "scores":
        stored = ext.load_external_traces(platform_dir)
        trace_lookup = {
            str(item.get("source_id") or ""): item.get("run_id") for item in stored if item.get("source_id")
        }

    checkpoint = import_jobs.resume_position(job)
    batch_size = max(int(settings.import_checkpoint_records), 1)

    def normalize_link(raw: Any) -> Dict[str, Any]:
        if kind == "traces":
            trace = langfuse_adapter.normalize_trace(raw)
            result = langfuse_adapter.link_trace(
                trace,
                context,
                run_id_field=str(association.get("run_id_field") or "metadata.run_id"),
                session_id_field=association.get("session_id_field"),
            )
            return result["trace"]
        return langfuse_adapter.normalize_score(raw)

    def process(raws: List[Any]) -> List[Dict[str, Any]]:
        job["phase"] = "linking" if kind == "traces" else "normalizing"
        batch: List[Dict[str, Any]] = []
        for raw in raws:
            job["processed_records"] = int(job.get("processed_records") or 0) + 1
            if raw is None:  # malformed source line
                job["invalid_records"] = int(job.get("invalid_records") or 0) + 1
                continue
            try:
                record = normalize_link(raw)
            except Exception as exc:
                job["invalid_records"] = int(job.get("invalid_records") or 0) + 1
                errors = job.setdefault("errors", [])
                if len(errors) < ext.MAX_IMPORT_ERRORS:
                    errors.append({"error": str(exc)[:200]})
                continue
            if kind == "scores" and trace_lookup is not None and isinstance(raw, dict):
                trace_id = raw.get("traceId") or raw.get("trace_id")
                if record.get("run_id") is None and trace_id:
                    linked_run = trace_lookup.get(str(trace_id))
                    if linked_run:
                        record["run_id"] = linked_run
                        record["provenance"]["association"] = "trace:%s" % str(trace_id)[:64]
                        record["provenance"]["confidence"] = "strong"
            if record.get("run_id") is None:
                job["unlinked_records"] = int(job.get("unlinked_records") or 0) + 1
            job["imported_records"] = int(job.get("imported_records") or 0) + 1
            batch.append(record)
        return batch

    def merge_batch(batch: List[Dict[str, Any]]) -> Callable[[], int]:
        def do_merge() -> int:
            if not batch:
                return 0
            if kind == "traces":
                existing = ext.load_external_traces(platform_dir)
                merged, duplicates, warnings = ext.merge_external_records(existing, batch, id_field="trace")
                ext.save_external_traces(platform_dir, merged)
            else:
                existing = ext.load_external_evaluations(platform_dir)
                merged, duplicates, warnings = ext.merge_external_records(existing, batch, id_field="evaluation")
                ext.save_external_evaluations(platform_dir, merged)
            for message in warnings:
                import_jobs.record_warning(job, message)
            return duplicates

        return do_merge

    if str(job.get("source_type") or "") == "http":
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        normalized = langfuse_adapter.validate_config(config)
        if not normalized.get("enabled"):
            raise import_jobs.ImportJobError(
                "invalid_import",
                "langfuse HTTP import is disabled; set config.enabled=true explicitly",
                retryable=False,
            )
        fetcher = _peek_fetcher(import_id)

        def should_cancel() -> bool:
            if cancel_event.is_set():
                return True
            current = store.load(import_id) or {}
            return bool(current.get("cancel_requested"))

        for page_info in langfuse_adapter.iter_http_pages(
            normalized,
            kind,
            fetcher=fetcher,
            start_cursor=checkpoint.get("cursor"),
            start_page=checkpoint.get("page"),
            should_cancel=should_cancel,
        ):
            _check_cancel(import_id, settings, worker_id, cancel_event, store)
            for message in page_info.get("warnings") or []:
                import_jobs.record_warning(job, message)
            batch = process(page_info.get("records") or [])
            position: Dict[str, Any] = {"page": int(page_info.get("page") or 0)}
            if page_info.get("next_cursor"):
                position["cursor"] = page_info.get("next_cursor")
            elif page_info.get("next_page"):
                position["page"] = page_info.get("next_page")
            job = _flush(
                job,
                settings,
                worker_id,
                cancel_event,
                store,
                position=position,
                merge=merge_batch(batch),
            )
        return job

    # file source: JSONL streams with byte offsets, JSON arrays resume by index
    path = integration_service.resolve_import_source(payload.get("source_file"), settings)
    start_index = int(checkpoint.get("index") or 0)
    start_offset = int(checkpoint.get("byte_offset") or 0)
    pending: List[Any] = []
    pending_since_flush = 0
    last_index = start_index
    last_offset = start_offset
    for index, raw, offset in langfuse_adapter.iter_file_records(
        path, start_index=start_index, start_byte_offset=start_offset
    ):
        _check_cancel(import_id, settings, worker_id, cancel_event, store)
        pending.append(raw)
        pending_since_flush += 1
        last_index, last_offset = index, offset
        if pending_since_flush >= batch_size:
            batch = process(pending)
            job = _flush(
                job,
                settings,
                worker_id,
                cancel_event,
                store,
                position={"index": last_index, "byte_offset": last_offset},
                merge=merge_batch(batch),
            )
            pending, pending_since_flush = [], 0
    batch = process(pending)
    job = _flush(
        job,
        settings,
        worker_id,
        cancel_event,
        store,
        position={"index": last_index, "byte_offset": last_offset},
        merge=merge_batch(batch),
    )
    return job


# ---------------------------------------------------------------------------
# promptfoo execution (file, batched by case index)
# ---------------------------------------------------------------------------


def _execute_promptfoo(
    job: Dict[str, Any],
    settings: Settings,
    worker_id: str,
    cancel_event: threading.Event,
    store: job_store_mod.JobStore,
) -> Dict[str, Any]:
    platform_dir = Path(settings.platform_dir)
    import_id = str(job["import_id"])
    payload = integration_service.load_import_payload(import_id, settings) or {}
    path = integration_service.resolve_import_source(payload.get("source_file"), settings)
    config = promptfoo_adapter.read_config_file(path)
    checkpoint = import_jobs.resume_position(job)
    start_index = int(checkpoint.get("index") or 0)
    batch_size = max(int(settings.import_checkpoint_records), 1)

    tests_raw = config.get("tests") or []
    if isinstance(tests_raw, dict):
        tests_raw = [tests_raw]
    if not isinstance(tests_raw, list):
        raise import_jobs.ImportJobError(
            "source_schema_error", "promptfoo config tests must be a list", retryable=False
        )
    tests_raw = tests_raw[: promptfoo_adapter.MAX_TESTS]

    providers_raw = config.get("providers") or config.get("provider") or []
    if isinstance(providers_raw, (str, dict)):
        providers_raw = [providers_raw]
    provider_warnings: List[str] = []
    providers: List[Dict[str, Any]] = []
    for item in (providers_raw if isinstance(providers_raw, list) else [])[: promptfoo_adapter.MAX_PROVIDERS]:
        declaration = promptfoo_adapter.map_provider(item, provider_warnings)
        if declaration is not None:
            providers.append(declaration)
    for message in provider_warnings:
        import_jobs.record_warning(job, message)
    default_provider = providers[0] if providers else None

    job["phase"] = "normalizing"
    job["total_records"] = len(tests_raw)

    def flush_drafts(batch: List[Dict[str, Any]], index: int) -> Callable[[], int]:
        def do_merge() -> int:
            if not batch:
                return 0
            existing = [
                {**draft, "source_id": draft.get("case_id"), "source_system": promptfoo_adapter.SOURCE_SYSTEM}
                for draft in promptfoo_adapter.load_drafts(platform_dir)
            ]
            merged, duplicates, warnings = ext.merge_external_records(existing, batch, id_field="promptfoo draft")
            for message in warnings:
                import_jobs.record_warning(job, message)
            cleaned = []
            for record in merged:
                record = dict(record)
                record.pop("source_id", None)
                record.pop("source_system", None)
                cleaned.append(record)
            promptfoo_adapter._save_drafts(platform_dir, cleaned)
            return duplicates

        return do_merge

    pending: List[Dict[str, Any]] = []
    pending_since_flush = 0
    last_index = start_index
    for position, raw in enumerate(tests_raw, start=1):
        if position <= start_index:
            continue
        _check_cancel(import_id, settings, worker_id, cancel_event, store)
        item_warnings: List[str] = []
        job["processed_records"] = int(job.get("processed_records") or 0) + 1
        last_index = position
        try:
            draft = promptfoo_adapter.map_test(raw, position, item_warnings)
        except ValueError as exc:
            job["invalid_records"] = int(job.get("invalid_records") or 0) + 1
            errors = job.setdefault("errors", [])
            if len(errors) < ext.MAX_IMPORT_ERRORS:
                errors.append({"error": str(exc)[:200], "line": position})
            continue
        for message in item_warnings:
            import_jobs.record_warning(job, message)
        if draft.get("provider_declaration") is None:
            draft["provider_declaration"] = default_provider
        job["imported_records"] = int(job.get("imported_records") or 0) + 1
        pending.append({**draft, "source_id": draft.get("case_id"), "source_system": promptfoo_adapter.SOURCE_SYSTEM})
        pending_since_flush += 1
        if pending_since_flush >= batch_size:
            job = _flush(
                job,
                settings,
                worker_id,
                cancel_event,
                store,
                position={"index": last_index},
                merge=flush_drafts(pending, last_index),
            )
            pending, pending_since_flush = [], 0
    job = _flush(
        job,
        settings,
        worker_id,
        cancel_event,
        store,
        position={"index": last_index},
        merge=flush_drafts(pending, last_index),
    )
    return job


# ---------------------------------------------------------------------------
# cost import execution (Phase 5: unified into the ImportJob state machine)
# ---------------------------------------------------------------------------


def _execute_costs(
    job: Dict[str, Any],
    settings: Settings,
    worker_id: str,
    cancel_event: threading.Event,
    store: job_store_mod.JobStore,
) -> Dict[str, Any]:
    """Import one JSONL cost log through the ImportJob state machine.

    Semantics preserved from the Phase 2 pipeline (``auditrail.costs``):

    - idempotent on ``(source_system, source_id)`` — re-importing is a
      duplicate, never a second record;
    - observed data (run ``llm_calls.json``) is never touched, so imports
      can never overwrite it;
    - records without an explicit cost are estimated only from a configured
      price table and keep the ``price_version``;
    - token counts never become currency; a missing cost stays ``null``.
    """
    platform_dir = Path(settings.platform_dir)
    import_id = str(job["import_id"])
    payload = integration_service.load_import_payload(import_id, settings) or {}
    path = integration_service.resolve_import_source(payload.get("source_file"), settings)
    job["source_file_name"] = path.name
    default_run_id = payload.get("run_id")
    default_agent_id = payload.get("agent_id")
    source_system = str(job.get("source_system") or "")
    checkpoint = import_jobs.resume_position(job)
    start_index = int(checkpoint.get("index") or 0)
    start_offset = int(checkpoint.get("byte_offset") or 0)
    batch_size = max(int(settings.import_checkpoint_records), 1)
    price_table = cost_mod.load_price_table(platform_dir)
    imported_at = job.get("started_at") or job.get("created_at")

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise import_jobs.ImportJobError(
            "invalid_import", "source_file is not readable: %s" % type(exc).__name__, retryable=False
        ) from exc
    if size > cost_mod.MAX_IMPORT_BYTES:
        raise import_jobs.ImportJobError(
            "invalid_import",
            "source_file exceeds the %d byte limit" % cost_mod.MAX_IMPORT_BYTES,
            retryable=False,
        )

    job["phase"] = "normalizing"

    def process(raws: List[Any]) -> List[Dict[str, Any]]:
        batch: List[Dict[str, Any]] = []
        for raw in raws:
            job["processed_records"] = int(job.get("processed_records") or 0) + 1
            if raw is None or not isinstance(raw, dict):
                job["invalid_records"] = int(job.get("invalid_records") or 0) + 1
                errors = job.setdefault("errors", [])
                if len(errors) < cost_mod.MAX_IMPORT_ERRORS:
                    errors.append({"error": "invalid JSONL line" if raw is None else "line is not a JSON object"})
                continue
            if default_run_id and not raw.get("run_id"):
                raw = {**raw, "run_id": default_run_id}
            if default_agent_id and not raw.get("agent_id"):
                raw = {**raw, "agent_id": default_agent_id}
            try:
                record = cost_mod.normalize_cost_record(raw, source="imported")
            except ValueError as exc:
                job["invalid_records"] = int(job.get("invalid_records") or 0) + 1
                errors = job.setdefault("errors", [])
                if len(errors) < cost_mod.MAX_IMPORT_ERRORS:
                    errors.append({"error": str(exc)[:200]})
                continue
            record["source_system"] = source_system
            if not record.get("source_id"):
                record["source_id"] = "line-%d" % int(job["processed_records"])
            record["record_id"] = cost_mod.record_id_for(source_system, str(record["source_id"]))
            record["imported_at"] = imported_at
            if record.get("total_cost") is None:
                # No explicit cost: estimate only from a configured price.
                estimate = cost_mod.estimate_cost(
                    price_table,
                    str(record.get("model") or ""),
                    int(record.get("input_tokens") or 0),
                    int(record.get("output_tokens") or 0),
                )
                if estimate is not None:
                    record.update(estimate)
                    record["source"] = "estimated"
            job["imported_records"] = int(job.get("imported_records") or 0) + 1
            batch.append(record)
        return batch

    def merge_batch(batch: List[Dict[str, Any]]) -> Callable[[], int]:
        def do_merge() -> int:
            if not batch:
                return 0
            existing = cost_mod.load_cost_records(platform_dir)
            seen = {
                (str(item.get("source_system") or ""), str(item.get("source_id") or ""))
                for item in existing
                if item.get("source_id")
            }
            duplicates = 0
            merged = list(existing)
            for record in batch:
                key = (str(record.get("source_system") or ""), str(record.get("source_id") or ""))
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                merged.append(record)
            if duplicates or len(merged) != len(existing):
                cost_mod.save_cost_records(platform_dir, merged)
            return duplicates

        return do_merge

    pending: List[Any] = []
    pending_since_flush = 0
    last_index = start_index
    last_offset = start_offset
    line_count = start_index
    for index, raw, offset in langfuse_adapter.iter_file_records(
        path, start_index=start_index, start_byte_offset=start_offset
    ):
        _check_cancel(import_id, settings, worker_id, cancel_event, store)
        line_count += 1
        if line_count > cost_mod.MAX_IMPORT_LINES:
            import_jobs.record_warning(job, "import stopped at the %d line limit" % cost_mod.MAX_IMPORT_LINES)
            break
        pending.append(raw)
        pending_since_flush += 1
        last_index, last_offset = index, offset
        if pending_since_flush >= batch_size:
            batch = process(pending)
            job = _flush(
                job,
                settings,
                worker_id,
                cancel_event,
                store,
                position={"index": last_index, "byte_offset": last_offset},
                merge=merge_batch(batch),
            )
            pending, pending_since_flush = [], 0
    batch = process(pending)
    job = _flush(
        job,
        settings,
        worker_id,
        cancel_event,
        store,
        position={"index": last_index, "byte_offset": last_offset},
        merge=merge_batch(batch),
    )
    return job
