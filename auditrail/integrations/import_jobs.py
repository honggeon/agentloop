"""Phase 4 resumable asynchronous import jobs (ImportJob).

Phase 3 imports ran synchronously inside the HTTP request and persisted a
one-shot import report. Phase 4 upgrades every external import to an
``ImportJob`` — a persisted, checkpointed state machine that can run
synchronously (small dry-runs) or asynchronously on the in-process worker:

- the job record lives at ``platform/external/imports/<import_id>.json`` and
  every write is atomic (``auditrail.trajectory.write_json``), so a crashed
  process never leaves a half-written job;
- a job never *claims* completion it did not reach: terminal statuses are
  written only by the worker that owns the job's current lease;
- cancellation is cooperative and persisted (``cancel_requested``); workers
  check the flag between read/normalize/link/persist steps;
- ``checkpoint`` carries source-appropriate resume positions (JSONL byte
  offset / line number, JSON array index, HTTP cursor/page, promptfoo case
  index); sources that cannot checkpoint set ``restart_from_beginning``;
- stale detection is lease based (``worker_id`` + ``heartbeat_at`` +
  ``lease_expires_at``): an expired lease marks the job ``stale`` — never
  ``completed`` — and an old worker can never overwrite a newer worker's
  state (:func:`assert_worker_owner`);
- import jobs only read sources and write external evidence; they never
  execute an Agent, never modify formal run artifacts and never touch
  ``evaluation.json``/``agent_judge.json``.

This module is core logic: no ``backend`` imports, no network, no model
credentials.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..run_contract import utc_now
from ..trajectory import write_json
from . import external_models as ext

SCHEMA_VERSION = 1

#: ImportJob lifecycle statuses.
JOB_STATUSES = (
    "queued",
    "validating",
    "running",
    "paused",
    "completed",
    "partial",
    "failed",
    "cancelled",
    "stale",
)

#: ImportJob processing phases (``phase`` is informational; ``status`` is
#: authoritative for lifecycle decisions).
JOB_PHASES = (
    "validating",
    "reading",
    "normalizing",
    "linking",
    "persisting",
    "finalizing",
)

ACTIVE_STATUSES = frozenset({"queued", "validating", "running", "paused"})
TERMINAL_STATUSES = frozenset({"completed", "partial", "failed", "cancelled"})
#: Statuses from which retry/resume is allowed.
RESUMABLE_STATUSES = frozenset({"failed", "partial", "stale"})
CANCELLABLE_STATUSES = frozenset({"queued", "validating", "running", "paused"})

#: Source types understood by the worker. ``restart_from_beginning`` marks
#: sources that cannot checkpoint (none currently — all supported sources
#: have a resume position).
SOURCE_TYPES = ("file", "http", "json", "jsonl", "yaml")

#: Default worker lease; a heartbeat older than this makes the job stale.
DEFAULT_LEASE_SECONDS = 60
#: Checkpoint write cadence bounds (records / seconds between checkpoints).
DEFAULT_CHECKPOINT_RECORDS = 100
DEFAULT_CHECKPOINT_SECONDS = 5.0
#: Retry policy: bounded attempts with capped backoff.
MAX_RETRY_COUNT = 5
MAX_BACKOFF_SECONDS = 60.0

#: Structured error codes surfaced by the import API. ``retryable`` decides
#: whether the worker may re-drive the job after the error.
RETRYABLE_ERROR_CODES = frozenset(
    {
        "source_unavailable",
        "source_rate_limited",
        "source_timeout",
        "worker_interrupted",
    }
)
NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "source_schema_error",
        "source_unauthorized",
        "source_rejected",
        "invalid_import",
        "import_cancelled",
        "import_conflict",
    }
)


class ImportJobError(ValueError):
    """A structured import failure. ``retryable`` is explicit, never guessed
    from raw exception text, and ``message`` must already be redacted."""

    def __init__(self, code: str, message: str, *, retryable: Optional[bool] = None) -> None:
        code = str(code or "invalid_import")
        if retryable is None:
            retryable = code in RETRYABLE_ERROR_CODES
        super().__init__(str(message)[:300])
        self.code = code
        self.retryable = bool(retryable)


class StaleWorkerError(RuntimeError):
    """Raised when a worker tries to persist state for a job whose lease it
    no longer owns — the old worker must never overwrite the new one."""


def classify_error_code(code: str) -> bool:
    """Whether an error code is retryable; unknown codes are not."""
    return str(code or "") in RETRYABLE_ERROR_CODES


# ---------------------------------------------------------------------------
# job construction and transitions
# ---------------------------------------------------------------------------


def new_import_job(
    *,
    source_system: str,
    source_type: str,
    kind: str,
    source_fingerprint: str,
    mode: str = "async",
    dry_run: bool = False,
    options: Optional[Dict[str, Any]] = None,
    restart_from_beginning: bool = False,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a fresh ``queued`` ImportJob record (not yet persisted)."""
    source_type = str(source_type or "file").strip()
    if source_type not in SOURCE_TYPES:
        raise ImportJobError("invalid_import", "unsupported source_type %r" % source_type, retryable=False)
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "import_job",
        "import_id": "import-%s" % uuid.uuid4().hex[:12],
        "source_system": str(source_system or "")[:64],
        "source_type": source_type,
        "kind": str(kind or "")[:64],
        "mode": "sync" if mode == "sync" else "async",
        "dry_run": bool(dry_run),
        "status": "queued",
        "phase": "validating",
        "created_at": utc_now(),
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
        "cursor": None,
        "page": 0,
        "total_records": None,
        "processed_records": 0,
        "imported_records": 0,
        "duplicate_records": 0,
        "unlinked_records": 0,
        "invalid_records": 0,
        "failed_records": 0,
        "retry_count": 0,
        "checkpoint_at": None,
        "checkpoint": None,
        "restart_from_beginning": bool(restart_from_beginning),
        "source_fingerprint": str(source_fingerprint or "")[:128],
        "options": _redact_options(options or {}),
        "error": None,
        "warnings": [],
        "worker_id": None,
        "heartbeat_at": None,
        "lease_expires_at": None,
        "actor": (str(actor)[:64] if actor else None),
        # Filled by the worker at finalization: the Phase 3 import report.
        "report": None,
    }


def _redact_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """Persist only non-sensitive option keys; config blocks keep the env var
    *name* (never the token value) and no header material at all."""
    allowed: Dict[str, Any] = {}
    for key, value in options.items():
        name = str(key)
        lowered = name.lower()
        if any(fragment in lowered for fragment in ("token", "secret", "password", "authorization", "cookie")):
            continue
        if isinstance(value, dict):
            allowed[name] = _redact_options(value)
        elif isinstance(value, (str, int, float, bool)) or value is None:
            allowed[name] = value
    return allowed


def validate_transition(current: Any, target: str) -> str:
    """Validate a status transition; raises ``ImportJobError`` when illegal."""
    current = str(current or "")
    target = str(target or "")
    if target not in JOB_STATUSES:
        raise ImportJobError("import_invalid_state", "unknown import job status %r" % target, retryable=False)
    legal = {
        "queued": {"validating", "cancelled", "stale"},
        "validating": {"running", "failed", "cancelled", "stale"},
        "running": {"completed", "partial", "failed", "cancelled", "paused", "stale"},
        "paused": {"running", "cancelled", "stale"},
        "stale": {"queued"},
        "failed": {"queued"},
        "partial": {"queued"},
        # Terminal self-transitions are idempotent no-ops (repeat cancel).
        "completed": {"completed"},
        "cancelled": {"cancelled"},
    }
    if target not in legal.get(current, set()):
        raise ImportJobError(
            "import_invalid_state",
            "import job cannot move from %s to %s" % (current or "unknown", target),
            retryable=False,
        )
    return target


def transition(
    job: Dict[str, Any],
    status: str,
    *,
    phase: Optional[str] = None,
    error: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Advance the job to ``status`` (validated), refreshing timestamps."""
    validate_transition(job.get("status"), status)
    job = dict(job)
    job["status"] = status
    if phase is not None:
        if phase not in JOB_PHASES:
            raise ImportJobError("import_invalid_state", "unknown import phase %r" % phase, retryable=False)
        job["phase"] = phase
    now = utc_now()
    if status in {"validating", "running"} and not job.get("started_at"):
        job["started_at"] = now
    if status in TERMINAL_STATUSES:
        job["finished_at"] = now
    if status == "queued":
        # Re-queue for retry/resume: the lease belongs to the next worker.
        job["started_at"] = None
        job["finished_at"] = None
        job["cancel_requested"] = False
        job["worker_id"] = None
        job["heartbeat_at"] = None
        job["lease_expires_at"] = None
    if error is not None:
        job["error"] = {
            "code": str(error.get("code") or "invalid_import")[:64],
            "message": str(error.get("message") or "")[:300],
            "retryable": bool(error.get("retryable")),
        }
    elif status in TERMINAL_STATUSES and status != "failed":
        job["error"] = None
    return job


def record_warning(job: Dict[str, Any], message: str) -> None:
    warnings = job.setdefault("warnings", [])
    if len(warnings) < ext.MAX_IMPORT_ERRORS:
        warnings.append(str(message)[:200])


# ---------------------------------------------------------------------------
# lease / staleness
# ---------------------------------------------------------------------------


def new_worker_id() -> str:
    return "worker-%s" % uuid.uuid4().hex[:12]


def acquire_lease(job: Dict[str, Any], worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Dict[str, Any]:
    """Stamp the job with the owning worker and a fresh lease."""
    from datetime import datetime, timedelta, timezone

    job = dict(job)
    job["worker_id"] = str(worker_id)
    now = datetime.now(timezone.utc)
    job["heartbeat_at"] = now.isoformat().replace("+00:00", "Z")
    job["lease_expires_at"] = (now + timedelta(seconds=max(int(lease_seconds), 1))).isoformat().replace(
        "+00:00", "Z"
    )
    return job


def heartbeat(job: Dict[str, Any], worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> Dict[str, Any]:
    """Refresh the lease; refuses to refresh a lease owned by another worker."""
    assert_worker_owner(job, worker_id)
    return acquire_lease(job, worker_id, lease_seconds=lease_seconds)


def assert_worker_owner(job: Dict[str, Any], worker_id: str) -> None:
    owner = job.get("worker_id")
    if owner is not None and str(owner) != str(worker_id):
        raise StaleWorkerError(
            "import job %s is owned by %s, not %s" % (job.get("import_id"), owner, worker_id)
        )


def is_lease_expired(job: Dict[str, Any], *, now: Optional[Any] = None) -> bool:
    """Whether the recorded lease has expired (job may be stale)."""
    expires = job.get("lease_expires_at")
    if not isinstance(expires, str) or not expires.strip():
        return False
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(expires.strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    return current > parsed


def job_is_stale(job: Dict[str, Any]) -> bool:
    """A job in an active status whose lease expired (or whose process clearly
    vanished) is stale — never silently completed."""
    if str(job.get("status") or "") == "stale":
        return True
    if str(job.get("status") or "") not in ACTIVE_STATUSES:
        return False
    return is_lease_expired(job)


# ---------------------------------------------------------------------------
# cancellation
# ---------------------------------------------------------------------------


def request_cancel(job: Dict[str, Any]) -> Dict[str, Any]:
    """Persist the cooperative cancel flag; idempotent on repeat calls.

    Terminal jobs are returned unchanged except ``cancel_requested`` for
    cancellable/active jobs; already-cancelled jobs keep their state.
    """
    job = dict(job)
    status = str(job.get("status") or "")
    if status == "cancelled":
        return job
    if status in TERMINAL_STATUSES:
        raise ImportJobError(
            "import_invalid_state",
            "import job is already %s and cannot be cancelled" % status,
            retryable=False,
        )
    job["cancel_requested"] = True
    return job


def raise_if_cancelled(job: Dict[str, Any]) -> None:
    if job.get("cancel_requested"):
        raise ImportJobError("import_cancelled", "import job cancellation was requested", retryable=False)


# ---------------------------------------------------------------------------
# checkpoints
# ---------------------------------------------------------------------------


def update_checkpoint(
    job: Dict[str, Any],
    *,
    cursor: Optional[str] = None,
    page: Optional[int] = None,
    byte_offset: Optional[int] = None,
    index: Optional[int] = None,
    last_source_id: Optional[str] = None,
    processed_records: Optional[int] = None,
) -> Dict[str, Any]:
    """Merge a checkpoint into the job record (persisted by the caller).

    Only the fields meaningful for the source type are expected to be set;
    ``checkpoint_at`` is refreshed on every update.
    """
    job = dict(job)
    checkpoint = dict(job.get("checkpoint") or {})
    if cursor is not None:
        checkpoint["cursor"] = str(cursor)[:256]
        job["cursor"] = str(cursor)[:256]
    if page is not None:
        checkpoint["page"] = int(page)
        job["page"] = int(page)
    if byte_offset is not None:
        checkpoint["byte_offset"] = max(int(byte_offset), 0)
    if index is not None:
        checkpoint["index"] = max(int(index), 0)
    if last_source_id is not None:
        checkpoint["last_source_id"] = str(last_source_id)[:128]
    if processed_records is not None:
        checkpoint["processed_records"] = max(int(processed_records), 0)
        job["processed_records"] = max(int(processed_records), 0)
    checkpoint["checkpoint_at"] = utc_now()
    job["checkpoint"] = checkpoint
    job["checkpoint_at"] = checkpoint["checkpoint_at"]
    return job


def resume_position(job: Dict[str, Any]) -> Dict[str, Any]:
    """The checkpoint a retry/resume starts from; empty when the source must
    restart from the beginning or no checkpoint was ever written."""
    if job.get("restart_from_beginning"):
        return {}
    checkpoint = job.get("checkpoint")
    if not isinstance(checkpoint, dict):
        return {}
    return {
        key: checkpoint.get(key)
        for key in ("cursor", "page", "byte_offset", "index", "last_source_id", "processed_records")
        if checkpoint.get(key) is not None
    }


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def _job_path(platform_dir: Path, import_id: str) -> Path:
    if not import_id or "/" in import_id or "\\" in import_id or import_id in {".", ".."}:
        raise ValueError("invalid import_id")
    return Path(platform_dir) / ext.EXTERNAL_DIR / ext.IMPORTS_DIR / ("%s.json" % import_id)


def save_import_job(
    platform_dir: Path,
    job: Dict[str, Any],
    *,
    worker_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically persist the job. When ``worker_id`` is given, the on-disk
    job must still be owned by that worker (or untouched) — an old worker can
    never overwrite a newer worker's state."""
    path = _job_path(platform_dir, str(job.get("import_id") or ""))
    if worker_id is not None and path.is_file():
        on_disk = load_import_job(platform_dir, str(job.get("import_id") or ""))
        if on_disk is not None:
            assert_worker_owner(on_disk, worker_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, job)
    return job


def load_import_job(platform_dir: Path, import_id: str) -> Optional[Dict[str, Any]]:
    try:
        path = _job_path(platform_dir, import_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_import_jobs(platform_dir: Path) -> List[Dict[str, Any]]:
    """All import records (Phase 4 jobs and legacy Phase 3 reports), newest
    first. Legacy reports are surfaced as completed-job views so old history
    stays readable."""
    directory = Path(platform_dir) / ext.EXTERNAL_DIR / ext.IMPORTS_DIR
    if not directory.is_dir():
        return []
    records: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("import_id"):
            records.append(data)
    records.sort(key=lambda item: str(item.get("created_at") or item.get("imported_at") or ""), reverse=True)
    return records


def is_job_record(record: Dict[str, Any]) -> bool:
    return record.get("record_type") == "import_job"


def job_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """Public view of one import record.

    Phase 4 jobs are returned as-is (plus the embedded Phase 3 report when
    finished); legacy one-shot reports are mapped to a completed job shape so
    old history remains readable through the new API.
    """
    if is_job_record(record):
        view = dict(record)
        report = record.get("report") if isinstance(record.get("report"), dict) else None
        if report is not None:
            # Additive compatibility: legacy readers see the report counters.
            for key in ("dry_run", "errors", "imported_at", "skipped_records"):
                if key in report and key not in view:
                    view[key] = report.get(key)
        return view
    # Legacy Phase 3 import report -> completed job view.
    legacy_status = str(record.get("status") or "imported")
    status = {
        "imported": "completed",
        "dry_run": "completed",
        "partial": "partial",
        "failed": "failed",
        "pending": "failed",
    }.get(legacy_status, "completed")
    return {
        "schema_version": SCHEMA_VERSION,
        "record_type": "import_job",
        "legacy_report": True,
        "import_id": record.get("import_id"),
        "source_system": record.get("source_system"),
        "source_type": "file",
        "kind": record.get("kind"),
        "mode": "sync",
        "dry_run": bool(record.get("dry_run")),
        "status": status,
        "phase": "finalizing",
        "created_at": record.get("imported_at"),
        "started_at": record.get("imported_at"),
        "finished_at": record.get("imported_at"),
        "cancel_requested": False,
        "cursor": None,
        "page": 0,
        "total_records": record.get("total_records"),
        "processed_records": record.get("total_records"),
        "imported_records": record.get("imported_records"),
        "duplicate_records": record.get("duplicate_records"),
        "unlinked_records": record.get("unlinked_records"),
        "invalid_records": record.get("invalid_records"),
        "failed_records": 0,
        "retry_count": 0,
        "checkpoint_at": None,
        "checkpoint": None,
        "restart_from_beginning": True,
        "source_fingerprint": None,
        "error": None,
        "warnings": list(record.get("warnings") or []),
        "worker_id": None,
        "heartbeat_at": None,
        "lease_expires_at": None,
        "report": record,
    }


def job_summary(record: Dict[str, Any]) -> Dict[str, Any]:
    """Compact list-row view for the imports listing."""
    view = job_view(record)
    return {
        "import_id": view.get("import_id"),
        "source_system": view.get("source_system"),
        "source_type": view.get("source_type"),
        "kind": view.get("kind"),
        "mode": view.get("mode"),
        "status": view.get("status"),
        "phase": view.get("phase"),
        "dry_run": bool(view.get("dry_run")),
        "total_records": view.get("total_records"),
        "processed_records": view.get("processed_records"),
        "imported_records": view.get("imported_records"),
        "duplicate_records": view.get("duplicate_records"),
        "unlinked_records": view.get("unlinked_records"),
        "invalid_records": view.get("invalid_records"),
        "failed_records": view.get("failed_records"),
        "retry_count": view.get("retry_count"),
        "cancel_requested": bool(view.get("cancel_requested")),
        "created_at": view.get("created_at"),
        "started_at": view.get("started_at"),
        "finished_at": view.get("finished_at"),
        "checkpoint_at": view.get("checkpoint_at"),
        "error": view.get("error"),
        "legacy_report": bool(view.get("legacy_report")),
        # Phase 3 list compatibility.
        "imported_at": view.get("finished_at") or view.get("created_at"),
    }


def refresh_stale_jobs(platform_dir: Path) -> List[str]:
    """Mark active jobs with an expired lease as ``stale``; returns the ids.

    Called on worker startup and before status reads. A stale job keeps its
    checkpoint and is resumable — it is never flipped to ``completed``.
    """
    stale_ids: List[str] = []
    for record in list_import_jobs(platform_dir):
        if not is_job_record(record):
            continue
        if str(record.get("status") or "") not in ACTIVE_STATUSES:
            continue
        if not is_lease_expired(record):
            continue
        updated = dict(record)
        updated["status"] = "stale"
        record_warning(updated, "worker lease expired; job marked stale (never auto-completed)")
        save_import_job(platform_dir, updated)
        stale_ids.append(str(record.get("import_id") or ""))
    return stale_ids
