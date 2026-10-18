"""Phase 5 storage-mode selector: one ImportJob store interface over the
Phase 4 file store and the Phase 5 database store.

The mode comes from server-owned settings (``AUDITRAIL_STORAGE_MODE``):

- ``file`` (default): the Phase 4 JSON record store. Single-process;
  cross-process reliability is explicitly *not* claimed.
- ``database`` / ``dual``: the database store (transactional claim,
  heartbeat and fencing tokens). Cross-process workers are safe because
  claiming is a database transaction. In ``dual`` mode the file record is
  mirrored best-effort after every database write so operators keep a
  readable file copy; the database stays authoritative and a mirror failure
  never fabricates job state (it is reported, not hidden).

This module is HTTP-assembly glue: the state machine lives in
``auditrail.integrations.import_jobs``, the database implementation in
``auditrail.db.job_store``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol

from auditrail.db import Database, StorageUnavailableError, migration_status
from auditrail.db.job_store import DbJobStore
from auditrail.integrations import import_jobs

from backend.app.config import Settings, get_settings, resolved_database_url

# ---------------------------------------------------------------------------
# store protocol
# ---------------------------------------------------------------------------


class JobStore(Protocol):
    """What the worker and routes need from an ImportJob store."""

    mode: str  # "file" | "database"

    def load(self, import_id: str) -> Optional[Dict[str, Any]]: ...

    def save(self, job: Dict[str, Any], *, worker_id: Optional[str] = None) -> Dict[str, Any]: ...

    def list_jobs(self) -> List[Dict[str, Any]]: ...

    def claim(self, import_id: str, worker_id: str, *, lease_seconds: int) -> Optional[Dict[str, Any]]: ...

    def claim_next(self, worker_id: str, *, lease_seconds: int) -> Optional[Dict[str, Any]]: ...

    def heartbeat(self, job: Dict[str, Any], worker_id: str, *, lease_seconds: int) -> Dict[str, Any]: ...

    def mutate(self, import_id: str, mutator) -> Optional[Dict[str, Any]]: ...

    def refresh_stale(self) -> List[str]: ...

    def requeue_retryable(self, job, worker_id: str, *, delay_seconds: float) -> Dict[str, Any]: ...

    def ping(self) -> bool: ...


# ---------------------------------------------------------------------------
# file store (Phase 4 semantics, single process)
# ---------------------------------------------------------------------------


class FileJobStore:
    """Adapter over the Phase 4 JSON record store (unchanged semantics)."""

    mode = "file"

    def __init__(self, platform_dir: Path) -> None:
        self._dir = Path(platform_dir)

    def load(self, import_id: str) -> Optional[Dict[str, Any]]:
        return import_jobs.load_import_job(self._dir, import_id)

    def save(self, job: Dict[str, Any], *, worker_id: Optional[str] = None) -> Dict[str, Any]:
        return import_jobs.save_import_job(self._dir, job, worker_id=worker_id)

    def list_jobs(self) -> List[Dict[str, Any]]:
        return import_jobs.list_import_jobs(self._dir)

    def claim(self, import_id: str, worker_id: str, *, lease_seconds: int) -> Optional[Dict[str, Any]]:
        job = self.load(import_id)
        if job is None:
            return None
        if str(job.get("status") or "") in import_jobs.TERMINAL_STATUSES:
            return job
        job = import_jobs.acquire_lease(job, worker_id, lease_seconds=lease_seconds)
        job["lease_version"] = int(job.get("lease_version") or 0) + 1
        job["attempt_count"] = int(job.get("attempt_count") or 0) + 1
        job = import_jobs.transition(job, "validating", phase="validating")
        return self.save(job, worker_id=worker_id)

    def claim_next(self, worker_id: str, *, lease_seconds: int) -> Optional[Dict[str, Any]]:
        # The file store is single-process: work is submitted explicitly, so
        # there is no cross-process queue to poll.
        return None

    def heartbeat(self, job: Dict[str, Any], worker_id: str, *, lease_seconds: int) -> Dict[str, Any]:
        return import_jobs.heartbeat(job, worker_id, lease_seconds=lease_seconds)

    def mutate(self, import_id: str, mutator) -> Optional[Dict[str, Any]]:
        job = self.load(import_id)
        if job is None:
            return None
        updated = mutator(job)
        return self.save(updated)

    def refresh_stale(self) -> List[str]:
        return import_jobs.refresh_stale_jobs(self._dir)

    def requeue_retryable(self, job, worker_id: str, *, delay_seconds: float) -> Dict[str, Any]:
        raise NotImplementedError("file store retries in-process (no requeue)")

    def ping(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# database store (Phase 5 cross-process semantics)
# ---------------------------------------------------------------------------


class DatabaseJobStore:
    """Database-backed store; in ``dual`` mode file records are mirrored."""

    mode = "database"

    def __init__(self, db: Database, *, tenant_id: str, platform_dir: Optional[Path], mirror_files: bool) -> None:
        self._store = DbJobStore(db, tenant_id=tenant_id)
        self._db = db
        self._dir = Path(platform_dir) if platform_dir else None
        self._mirror = bool(mirror_files and platform_dir)

    def _mirror_file(self, job: Dict[str, Any]) -> None:
        if not self._mirror or self._dir is None:
            return
        try:
            import_jobs.save_import_job(self._dir, job)
        except Exception:
            # The mirror is best-effort: the database record is authoritative
            # and already written. A mirror failure is surfaced by the
            # consistency check, never by corrupting job state.
            pass

    def load(self, import_id: str) -> Optional[Dict[str, Any]]:
        return self._store.load_job(import_id)

    def save(self, job: Dict[str, Any], *, worker_id: Optional[str] = None) -> Dict[str, Any]:
        saved = self._store.save_job(
            job, worker_id=worker_id, lease_version=int(job.get("lease_version") or 0)
        )
        self._mirror_file(saved)
        return saved

    def list_jobs(self) -> List[Dict[str, Any]]:
        return self._store.list_jobs()

    def claim(self, import_id: str, worker_id: str, *, lease_seconds: int) -> Optional[Dict[str, Any]]:
        job = self._store.claim_specific(import_id, worker_id, lease_seconds=lease_seconds)
        if job is not None:
            self._mirror_file(job)
        return job

    def claim_next(self, worker_id: str, *, lease_seconds: int) -> Optional[Dict[str, Any]]:
        job = self._store.claim_next_queued(worker_id, lease_seconds=lease_seconds)
        if job is not None:
            self._mirror_file(job)
        return job

    def heartbeat(self, job: Dict[str, Any], worker_id: str, *, lease_seconds: int) -> Dict[str, Any]:
        return self._store.heartbeat(job, worker_id, lease_seconds=lease_seconds)

    def mutate(self, import_id: str, mutator) -> Optional[Dict[str, Any]]:
        updated = self._store.mutate_job(import_id, mutator)
        if updated is not None:
            self._mirror_file(updated)
        return updated

    def refresh_stale(self) -> List[str]:
        stale = self._store.recover_stale()
        if self._mirror:
            for import_id in stale:
                record = self._store.load_job(import_id)
                if record is not None:
                    self._mirror_file(record)
        return stale

    def requeue_retryable(self, job, worker_id: str, *, delay_seconds: float) -> Dict[str, Any]:
        updated = self._store.requeue_retryable(job, worker_id, delay_seconds=delay_seconds)
        self._mirror_file(updated)
        return updated

    def ping(self) -> bool:
        return self._db.ping()

    # -- database-only introspection (admin/health) ----------------------

    @property
    def raw(self) -> DbJobStore:
        return self._store

    @property
    def database(self) -> Database:
        return self._db


# ---------------------------------------------------------------------------
# selector
# ---------------------------------------------------------------------------

_LOCK = threading.RLock()
_CACHED: Dict[str, "DatabaseJobStore"] = {}


def storage_mode(settings: Optional[Settings] = None) -> str:
    current = settings or get_settings()
    return str(getattr(current, "storage_mode", "file") or "file")


def database_for_settings(settings: Settings) -> Database:
    """Open (and cache per-URL) the configured database handle."""
    url = resolved_database_url(settings)
    with _LOCK:
        store = _CACHED.get(url)
        if store is not None:
            return store.database
    db = Database(url)
    # Force a connection now so a misconfigured URL fails at startup, not on
    # first request.
    if not db.ping():
        db.close()
        raise StorageUnavailableError("database is unreachable")
    return db


def job_store(settings: Optional[Settings] = None) -> JobStore:
    """The ImportJob store for the configured storage mode.

    ``file`` mode never touches a database. ``database``/``dual`` open the
    configured database; a missing schema is reported by the readiness check
    (the API never auto-migrates).
    """
    current = settings or get_settings()
    mode = storage_mode(current)
    if mode == "file":
        return FileJobStore(Path(current.platform_dir))
    url = resolved_database_url(current)
    with _LOCK:
        cached = _CACHED.get(url)
        if cached is not None and cached._mirror == (mode == "dual"):
            return cached
    db = database_for_settings(current)
    store = DatabaseJobStore(
        db,
        tenant_id=str(getattr(current, "tenant_id", "default") or "default"),
        platform_dir=Path(current.platform_dir),
        mirror_files=(mode == "dual"),
    )
    with _LOCK:
        _CACHED[url] = store
    return store


def reset_cached_stores() -> None:
    """Drop cached database handles (tests, settings changes)."""
    with _LOCK:
        stores = list(_CACHED.values())
        _CACHED.clear()
    for store in stores:
        try:
            store.database.close()
        except Exception:
            pass


def migration_state(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Migration status for readiness/admin; never raises."""
    current = settings or get_settings()
    if storage_mode(current) == "file":
        return {"storage_mode": "file", "required": False, "up_to_date": True, "pending": []}
    try:
        status = migration_status(database_for_settings(current))
    except StorageUnavailableError:
        return {"storage_mode": storage_mode(current), "required": True, "up_to_date": False, "error": "storage_unavailable"}
    status["storage_mode"] = storage_mode(current)
    status["required"] = True
    return status
