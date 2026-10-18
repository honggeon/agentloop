"""Phase 5 cross-process ImportJob worker loop.

Used two ways, with identical semantics:

- standalone process: ``python -m backend.app.worker_main`` (the production
  worker deployment unit);
- embedded thread inside the API process (``queue_mode=local_worker`` on a
  single host): started from the FastAPI startup hook.

Both claim jobs with database transactions (``auditrail.db.job_store``), so
any mix of embedded/standalone workers is safe: one job has exactly one
lease owner, fenced by ``lease_version``. The loop:

- registers itself in the ``workers`` table and heartbeats (health/admin
  visibility);
- on startup marks expired-lease jobs ``stale`` (never ``completed``);
- picks up due queued jobs (``claim_next``; in redis mode a delivery hint
  shortens pickup latency but the database poll is the correctness path);
- executes with ``requeue_on_retry=True`` — a retryable failure releases
  the lease and reschedules via ``next_retry_at`` instead of sleeping while
  holding the claim;
- stops cleanly on signal: in-flight jobs keep their lease, expire, and are
  recovered as ``stale`` by the next worker — never faked as finished.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

from auditrail.integrations import import_jobs

from backend.app import import_worker, job_store as job_store_mod, queue_adapter
from backend.app.config import Settings, get_settings


class ImportWorkerLoop:
    """Polling claim-execute loop over the database job store."""

    def __init__(self, settings: Optional[Settings] = None, *, worker_id: Optional[str] = None) -> None:
        self._settings = settings or get_settings()
        self._worker_id = worker_id or import_jobs.new_worker_id()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_claim_at: Optional[float] = None
        self._executed = 0
        self._claim_errors = 0

    @property
    def worker_id(self) -> str:
        return self._worker_id

    # ------------------------------------------------------------------
    # single steps
    # ------------------------------------------------------------------

    def recover_stale(self) -> list:
        store = job_store_mod.job_store(self._settings)
        return store.refresh_stale()

    def run_once(self) -> bool:
        """Claim and execute at most one job; returns True when one ran."""
        store = job_store_mod.job_store(self._settings)
        # A redis hint shortens pickup latency but is never required: the
        # database claim below is the correctness path either way.
        queue_adapter.dequeue_hint(
            self._settings, timeout_seconds=max(int(self._settings.worker_poll_interval_ms / 1000), 1)
        )
        try:
            job = store.claim_next(self._worker_id, lease_seconds=int(self._settings.import_lease_seconds))
        except Exception:
            self._claim_errors += 1
            time.sleep(min(2.0**min(self._claim_errors, 5), 30.0))
            return False
        self._claim_errors = 0
        if job is None:
            return False
        self._last_claim_at = time.monotonic()
        # No cancel event is passed: stopping the loop prevents *new* claims
        # but never force-cancels an in-flight job. An interrupted job keeps
        # its lease, expires, and is recovered as ``stale`` — it is never
        # rewritten as cancelled or completed by a dying worker.
        import_worker.execute_claimed_job(job, self._settings)
        self._executed += 1
        return True

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _register(self) -> None:
        store = job_store_mod.job_store(self._settings)
        raw = getattr(store, "raw", None)
        if raw is not None:
            try:
                raw.register_worker(self._worker_id, pid=os.getpid())
            except Exception:
                pass  # registry visibility is best-effort; claiming is not

    def _heartbeat(self) -> None:
        store = job_store_mod.job_store(self._settings)
        raw = getattr(store, "raw", None)
        if raw is not None:
            try:
                raw.heartbeat_worker(self._worker_id)
            except Exception:
                pass

    def _deregister(self) -> None:
        try:
            store = job_store_mod.job_store(self._settings)
            raw = getattr(store, "raw", None)
            if raw is not None:
                raw.stop_worker(self._worker_id)
        except Exception:
            pass

    def run_forever(self) -> None:
        """Blocking loop (standalone process)."""
        self._register()
        self.recover_stale()
        last_heartbeat = time.monotonic()
        try:
            while not self._stop.is_set():
                if time.monotonic() - last_heartbeat >= max(int(self._settings.worker_heartbeat_seconds), 1):
                    self._heartbeat()
                    last_heartbeat = time.monotonic()
                worked = self.run_once()
                if not worked:
                    self._stop.wait(max(int(self._settings.worker_poll_interval_ms), 100) / 1000.0)
        finally:
            self._deregister()

    def start_background(self) -> threading.Thread:
        """Embedded mode: daemon thread inside the API process."""
        if self._thread is not None:
            return self._thread
        self._thread = threading.Thread(
            target=self.run_forever, name="auditrail-import-worker", daemon=True
        )
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10.0)
            self._thread = None

    def status(self) -> Dict[str, Any]:
        return {
            "worker_id": self._worker_id,
            "running": self._thread is not None and self._thread.is_alive(),
            "executed_jobs": self._executed,
            "claim_errors": self._claim_errors,
            "mode": job_store_mod.storage_mode(self._settings),
        }


# ---------------------------------------------------------------------------
# embedded singleton (API process) and CLI entry
# ---------------------------------------------------------------------------

_EMBEDDED: Optional[ImportWorkerLoop] = None
_LOCK = threading.RLock()


def start_embedded_worker(settings: Optional[Settings] = None) -> Optional[ImportWorkerLoop]:
    """Startup hook: run the worker loop in-process for database queue modes."""
    global _EMBEDDED
    current = settings or get_settings()
    if queue_adapter.queue_mode(current) == "inline":
        return None
    if not current.import_worker_enabled:
        return None
    if job_store_mod.storage_mode(current) == "file":
        return None
    with _LOCK:
        if _EMBEDDED is not None:
            return _EMBEDDED
        loop = ImportWorkerLoop(current)
        loop.start_background()
        _EMBEDDED = loop
        return loop


def stop_embedded_worker() -> None:
    global _EMBEDDED
    with _LOCK:
        loop = _EMBEDDED
        _EMBEDDED = None
    if loop is not None:
        loop.stop()


def embedded_worker_status() -> Optional[Dict[str, Any]]:
    with _LOCK:
        loop = _EMBEDDED
    return loop.status() if loop is not None else None


def main() -> None:
    """Standalone worker entry: ``python -m backend.app.worker_main``."""
    from backend.app.config import validate_startup_settings

    settings = get_settings()
    validate_startup_settings(settings)
    if job_store_mod.storage_mode(settings) == "file":
        raise SystemExit(
            "worker requires AUDITRAIL_STORAGE_MODE=database|dual (file mode is single-process inline)"
        )
    loop = ImportWorkerLoop(settings)
    print(
        "import worker %s started (mode=%s, queue=%s)"
        % (loop.worker_id, job_store_mod.storage_mode(settings), queue_adapter.queue_mode(settings))
    )
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.stop()


if __name__ == "__main__":
    main()
