"""Phase 5 cross-process ImportJob store (database backend).

This is the reliable task infrastructure behind ``queue_mode=local_worker``
and ``queue_mode=redis``:

- **claim is a transaction**: picking a queued job, stamping the worker and
  incrementing the fencing token (``lease_version``) happen atomically, so
  two workers in different processes can never own the same job;
- **fencing token**: every worker write (checkpoint, heartbeat, terminal
  status) is conditioned on ``worker_id + lease_version`` — an old worker
  whose lease was taken over gets zero rows and must stop
  (:class:`~auditrail.integrations.import_jobs.StaleWorkerError`), it can
  never overwrite the new owner's state;
- **heartbeat honesty**: a failed heartbeat raises; the worker never keeps
  claiming health it cannot prove;
- **stale recovery**: an expired lease marks the job ``stale`` — never
  ``completed`` — and resume is explicit;
- **retry backoff without holding a lease**: a retryable failure requeues
  the job with ``next_retry_at`` instead of sleeping inside the claim.

The full ImportJob record (same shape as the Phase 4 file store) is the
``payload_json`` column; the indexed columns are projections for queries.
All timestamps are UTC ISO-8601 text (dialect portable).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..integrations import import_jobs
from ..run_contract import utc_now
from .engine import Database, dumps_json, loads_json


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse(text: Any) -> Optional[datetime]:
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


#: Columns projected out of the payload record for indexed access.
def _row_for(record: Dict[str, Any], tenant_id: str) -> Dict[str, Any]:
    error = record.get("error") if isinstance(record.get("error"), dict) else {}
    checkpoint = record.get("checkpoint")
    return {
        "tenant_id": tenant_id,
        "import_id": str(record.get("import_id") or ""),
        "source_system": str(record.get("source_system") or ""),
        "source_type": record.get("source_type"),
        "kind": record.get("kind"),
        "status": str(record.get("status") or "queued"),
        "phase": record.get("phase"),
        "mode": record.get("mode"),
        "dry_run": 1 if record.get("dry_run") else 0,
        "worker_id": record.get("worker_id"),
        "lease_version": int(record.get("lease_version") or 0),
        "heartbeat_at": record.get("heartbeat_at"),
        "lease_expires_at": record.get("lease_expires_at"),
        "attempt_count": int(record.get("attempt_count") or 0),
        "max_attempts": int(record.get("max_attempts") or import_jobs.MAX_RETRY_COUNT),
        "next_retry_at": record.get("next_retry_at"),
        "cancel_requested": 1 if record.get("cancel_requested") else 0,
        "pause_requested": 1 if record.get("pause_requested") else 0,
        "last_error_code": error.get("code"),
        "last_error_retryable": (None if not error else (1 if error.get("retryable") else 0)),
        "processed_records": int(record.get("processed_records") or 0),
        "imported_records": int(record.get("imported_records") or 0),
        "checkpoint_json": dumps_json(checkpoint) if isinstance(checkpoint, dict) else None,
        "payload_json": dumps_json(record),
        "idempotency_key": record.get("idempotency_key"),
        "request_fingerprint": record.get("request_fingerprint") or record.get("source_fingerprint"),
        "actor": record.get("actor"),
        "queue_degraded": 1 if record.get("queue_degraded") else 0,
        "created_at": record.get("created_at") or utc_now(),
        "updated_at": utc_now(),
        "started_at": record.get("started_at"),
        "finished_at": record.get("finished_at"),
        "source_ref": record.get("source_ref"),
        "migration_id": record.get("migration_id"),
    }


_INSERT_COLUMNS = list(_row_for(import_jobs.new_import_job(
    source_system="x", source_type="file", kind="k", source_fingerprint="fp"
), "default").keys())


class DbJobStore:
    """Database-backed ImportJob store. One instance per process."""

    def __init__(self, db: Database, *, tenant_id: str = "default") -> None:
        self._db = db
        self._tenant = str(tenant_id or "default")

    # ------------------------------------------------------------------
    # row <-> record
    # ------------------------------------------------------------------

    def _record_from_row(self, row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        record = loads_json(row.get("payload_json"), default=None)
        if not isinstance(record, dict):
            return None
        # Projections that may have moved after the payload was written.
        record["lease_version"] = int(row.get("lease_version") or record.get("lease_version") or 0)
        record["attempt_count"] = int(row.get("attempt_count") or record.get("attempt_count") or 0)
        record["worker_id"] = row.get("worker_id")
        record["heartbeat_at"] = row.get("heartbeat_at")
        record["lease_expires_at"] = row.get("lease_expires_at")
        record["next_retry_at"] = row.get("next_retry_at")
        record["pause_requested"] = bool(row.get("pause_requested"))
        record["cancel_requested"] = bool(row.get("cancel_requested"))
        record["queue_degraded"] = bool(row.get("queue_degraded"))
        record["status"] = row.get("status") or record.get("status")
        # Migration lineage columns are projected back so migrated records
        # stay traceable to their file source.
        if row.get("source_ref"):
            record["source_ref"] = row.get("source_ref")
        if row.get("migration_id"):
            record["migration_id"] = row.get("migration_id")
        return record

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def save_job(
        self,
        job: Dict[str, Any],
        *,
        worker_id: Optional[str] = None,
        lease_version: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Insert or update one job.

        With ``worker_id``/``lease_version`` the update is fenced: it only
        lands when the row is still owned by that exact lease, otherwise
        :class:`import_jobs.StaleWorkerError` is raised.
        """
        import_id = str(job.get("import_id") or "")
        if not import_id:
            raise ValueError("import_id is required")
        row = _row_for(job, self._tenant)
        if worker_id is None:
            self._upsert(row)
            return job
        fenced_version = int(lease_version if lease_version is not None else row["lease_version"])
        assignments = ", ".join("%s = %s" % (column, "%s") for column in _INSERT_COLUMNS if column != "tenant_id")
        values = tuple(row[column] for column in _INSERT_COLUMNS if column != "tenant_id")
        updated = self._db.execute(
            "UPDATE import_jobs SET %s WHERE tenant_id = %%s AND import_id = %%s "
            "AND worker_id = %%s AND lease_version = %%s" % assignments,
            values + (self._tenant, import_id, str(worker_id), fenced_version),
        )
        if updated == 0:
            raise import_jobs.StaleWorkerError(
                "import job %s is not owned by %s at lease_version %d" % (import_id, worker_id, fenced_version)
            )
        self._touch_lease(import_id, str(worker_id), fenced_version, heartbeat=row["heartbeat_at"], expires=row["lease_expires_at"])
        return job

    def _upsert(self, row: Dict[str, Any]) -> None:
        columns = ", ".join(_INSERT_COLUMNS)
        placeholders = ", ".join(["%s"] * len(_INSERT_COLUMNS))
        updates = ", ".join(
            "%s = excluded.%s" % (column, column)
            for column in _INSERT_COLUMNS
            if column not in ("tenant_id", "import_id")
        )
        sql = (
            "INSERT INTO import_jobs (%s) VALUES (%s) ON CONFLICT (tenant_id, import_id) DO UPDATE SET %s"
            % (columns, placeholders, updates)
        )
        self._db.execute(sql, tuple(row[column] for column in _INSERT_COLUMNS))

    def _touch_lease(
        self,
        import_id: str,
        worker_id: str,
        lease_version: int,
        *,
        heartbeat: Optional[str],
        expires: Optional[str],
    ) -> None:
        now = utc_now()
        self._db.execute(
            "INSERT INTO job_leases (tenant_id, job_id, worker_id, lease_version, acquired_at, heartbeat_at, expires_at, status)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tenant_id, job_id) DO UPDATE SET worker_id = excluded.worker_id,"
            " lease_version = excluded.lease_version, heartbeat_at = excluded.heartbeat_at,"
            " expires_at = excluded.expires_at, status = excluded.status",
            (self._tenant, import_id, worker_id, int(lease_version), now, heartbeat, expires, "held"),
        )

    def load_job(self, import_id: str) -> Optional[Dict[str, Any]]:
        row = self._db.query_one(
            "SELECT * FROM import_jobs WHERE tenant_id = %s AND import_id = %s",
            (self._tenant, str(import_id)),
        )
        return self._record_from_row(row)

    def list_jobs(self) -> List[Dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM import_jobs WHERE tenant_id = %s ORDER BY created_at DESC",
            (self._tenant,),
        )
        records = []
        for row in rows:
            record = self._record_from_row(row)
            if record is not None:
                records.append(record)
        return records

    def mutate_job(self, import_id: str, mutator: Callable[[Dict[str, Any]], Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Read-modify-write one job atomically (API flag writes: cancel,
        pause, retry scheduling). ``mutator`` receives the current record and
        returns the replacement. Returns ``None`` when the job is unknown."""
        with self._db.transaction(immediate=True):
            row = self._db.query_one(
                "SELECT * FROM import_jobs WHERE tenant_id = %s AND import_id = %s"
                + (" FOR UPDATE" if self._db.dialect == "postgresql" else ""),
                (self._tenant, str(import_id)),
            )
            record = self._record_from_row(row)
            if record is None:
                return None
            updated = mutator(record)
            self._upsert(_row_for(updated, self._tenant))
            return updated

    # ------------------------------------------------------------------
    # claim / heartbeat / fencing
    # ------------------------------------------------------------------

    def claim_next_queued(self, worker_id: str, *, lease_seconds: int) -> Optional[Dict[str, Any]]:
        """Atomically claim the oldest runnable queued job for ``worker_id``.

        Runnable means ``status = queued`` and ``next_retry_at`` due. The
        claim bumps ``lease_version`` (fencing token) and ``attempt_count``,
        stamps the lease and moves the job to ``validating`` in one
        transaction — two workers can never claim the same job.
        """
        now = _now()
        with self._db.transaction(immediate=True):
            row = self._db.query_one(
                "SELECT import_id, lease_version FROM import_jobs"
                " WHERE tenant_id = %s AND status = %s"
                " AND (next_retry_at IS NULL OR next_retry_at <= %s)"
                " ORDER BY created_at LIMIT 1"
                + (" FOR UPDATE" if self._db.dialect == "postgresql" else ""),
                (self._tenant, "queued", _iso(now)),
            )
            if row is None:
                return None
            return self._claim_row(str(row["import_id"]), worker_id, lease_seconds, now)

    def claim_specific(self, import_id: str, worker_id: str, *, lease_seconds: int) -> Optional[Dict[str, Any]]:
        """Claim one specific queued job (inline/forced execution).

        ``None`` when the job is not claimable (not queued). A terminal job
        returns its record unchanged — re-executing a terminal job is an
        idempotent no-op, never a re-run. Active jobs with an expired lease
        are *not* claimed here: they go through ``recover_stale`` and an
        explicit resume first.
        """
        now = _now()
        with self._db.transaction(immediate=True):
            row = self._db.query_one(
                "SELECT import_id, status FROM import_jobs"
                " WHERE tenant_id = %s AND import_id = %s"
                + (" FOR UPDATE" if self._db.dialect == "postgresql" else ""),
                (self._tenant, str(import_id)),
            )
            if row is None:
                return None
            status = str(row.get("status") or "")
            if status in import_jobs.TERMINAL_STATUSES:
                return self.load_job(import_id)
            if status != "queued":
                return None
            return self._claim_row(str(import_id), worker_id, lease_seconds, now)

    def _claim_row(self, import_id: str, worker_id: str, lease_seconds: int, now: datetime) -> Optional[Dict[str, Any]]:
        """Claim inside an open transaction (caller holds it)."""
        record = self.load_job(import_id)
        if record is None:
            return None
        record = dict(record)
        record["attempt_count"] = int(record.get("attempt_count") or 0) + 1
        record["next_retry_at"] = None
        record = import_jobs.acquire_lease(record, worker_id, lease_seconds=lease_seconds)
        record["lease_version"] = int(record.get("lease_version") or 0) + 1
        if str(record.get("status") or "") == "queued":
            record = import_jobs.transition(record, "validating", phase="validating")
        updated = self._db.execute(
            "UPDATE import_jobs SET status = %s, phase = %s, worker_id = %s, lease_version = %s,"
            " heartbeat_at = %s, lease_expires_at = %s, attempt_count = %s, next_retry_at = NULL,"
            " started_at = %s, updated_at = %s, payload_json = %s"
            " WHERE tenant_id = %s AND import_id = %s AND lease_version = %s",
            (
                record["status"],
                record.get("phase"),
                worker_id,
                int(record["lease_version"]),
                record.get("heartbeat_at"),
                record.get("lease_expires_at"),
                int(record["attempt_count"]),
                record.get("started_at"),
                utc_now(),
                dumps_json(record),
                self._tenant,
                import_id,
                int(record["lease_version"]) - 1,
            )
        )
        if updated == 0:
            return None  # lost the race: another worker claimed first
        self._touch_lease(
            import_id,
            worker_id,
            int(record["lease_version"]),
            heartbeat=record.get("heartbeat_at"),
            expires=record.get("lease_expires_at"),
        )
        return record

    def heartbeat(self, job: Dict[str, Any], worker_id: str, *, lease_seconds: int) -> Dict[str, Any]:
        """Fenced heartbeat: refresh the lease only while this worker still
        owns it. A stale worker gets :class:`StaleWorkerError` and must stop
        — it never keeps claiming health it cannot prove."""
        import_id = str(job.get("import_id") or "")
        now = _now()
        heartbeat_at = _iso(now)
        expires_at = _iso(now + timedelta(seconds=max(int(lease_seconds), 1)))
        updated = self._db.execute(
            "UPDATE import_jobs SET heartbeat_at = %s, lease_expires_at = %s, updated_at = %s"
            " WHERE tenant_id = %s AND import_id = %s AND worker_id = %s AND lease_version = %s",
            (
                heartbeat_at,
                expires_at,
                utc_now(),
                self._tenant,
                import_id,
                str(worker_id),
                int(job.get("lease_version") or 0),
            ),
        )
        if updated == 0:
            raise import_jobs.StaleWorkerError(
                "import job %s is not owned by %s at lease_version %d"
                % (import_id, worker_id, int(job.get("lease_version") or 0))
            )
        self._touch_lease(
            import_id,
            str(worker_id),
            int(job.get("lease_version") or 0),
            heartbeat=heartbeat_at,
            expires=expires_at,
        )
        refreshed = dict(job)
        refreshed["heartbeat_at"] = heartbeat_at
        refreshed["lease_expires_at"] = expires_at
        return refreshed

    # ------------------------------------------------------------------
    # stale recovery / scheduling
    # ------------------------------------------------------------------

    def recover_stale(self) -> List[str]:
        """Mark active jobs with an expired lease ``stale``; returns ids.

        A stale job keeps its checkpoint and is resumable — it is never
        flipped to ``completed``.
        """
        now = _iso(_now())
        stale_ids: List[str] = []
        with self._db.transaction(immediate=True):
            rows = self._db.query(
                "SELECT import_id FROM import_jobs WHERE tenant_id = %s"
                " AND status IN ('queued', 'validating', 'running', 'paused')"
                " AND lease_expires_at IS NOT NULL AND lease_expires_at < %s",
                (self._tenant, now),
            )
            for row in rows:
                record = self.load_job(str(row["import_id"]))
                if record is None:
                    continue
                record = dict(record)
                record["status"] = "stale"
                record["worker_id"] = None
                import_jobs.record_warning(
                    record, "worker lease expired; job marked stale (never auto-completed)"
                )
                self._upsert(_row_for(record, self._tenant))
                stale_ids.append(str(record.get("import_id")))
        return stale_ids

    def requeue_retryable(
        self,
        job: Dict[str, Any],
        worker_id: str,
        *,
        delay_seconds: float,
    ) -> Dict[str, Any]:
        """Release the lease and requeue a retryable job with backoff
        (``next_retry_at``) instead of sleeping while holding the claim."""
        record = import_jobs.transition(dict(job), "queued", phase="validating")
        record["retry_count"] = int(job.get("retry_count") or 0) + 1
        record["next_retry_at"] = _iso(_now() + timedelta(seconds=max(float(delay_seconds), 0.0)))
        self.save_job(record, worker_id=worker_id, lease_version=int(job.get("lease_version") or 0))
        return record

    def find_by_idempotency(self, key: str, fingerprint: str) -> Optional[Dict[str, Any]]:
        row = self._db.query_one(
            "SELECT * FROM import_jobs WHERE tenant_id = %s AND idempotency_key = %s"
            " AND request_fingerprint = %s",
            (self._tenant, str(key), str(fingerprint)),
        )
        return self._record_from_row(row)

    def count_by_status(self) -> Dict[str, int]:
        rows = self._db.query(
            "SELECT status, COUNT(*) AS count FROM import_jobs WHERE tenant_id = %s GROUP BY status",
            (self._tenant,),
        )
        return {str(row["status"]): int(row["count"]) for row in rows}

    def count_stale(self) -> int:
        row = self._db.query_one(
            "SELECT COUNT(*) AS count FROM import_jobs WHERE tenant_id = %s AND status = %s",
            (self._tenant, "stale"),
        )
        return int(row["count"]) if row else 0

    def recent_failures(self, *, limit: int = 10) -> List[Dict[str, Any]]:
        rows = self._db.query(
            "SELECT import_id, source_system, kind, status, last_error_code, finished_at, updated_at"
            " FROM import_jobs WHERE tenant_id = %s AND status IN ('failed', 'partial', 'stale')"
            " ORDER BY updated_at DESC LIMIT %s",
            (self._tenant, int(limit)),
        )
        return rows

    # ------------------------------------------------------------------
    # worker registry (health/admin visibility)
    # ------------------------------------------------------------------

    def register_worker(self, worker_id: str, *, pid: Optional[int] = None) -> None:
        now = utc_now()
        self._db.execute(
            "INSERT INTO workers (tenant_id, worker_id, pid, started_at, heartbeat_at, status)"
            " VALUES (%s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (tenant_id, worker_id) DO UPDATE SET heartbeat_at = excluded.heartbeat_at,"
            " status = excluded.status, pid = excluded.pid",
            (self._tenant, str(worker_id), pid, now, now, "running"),
        )

    def heartbeat_worker(self, worker_id: str) -> None:
        self._db.execute(
            "UPDATE workers SET heartbeat_at = %s, status = %s WHERE tenant_id = %s AND worker_id = %s",
            (utc_now(), "running", self._tenant, str(worker_id)),
        )

    def stop_worker(self, worker_id: str) -> None:
        self._db.execute(
            "UPDATE workers SET status = %s, heartbeat_at = %s WHERE tenant_id = %s AND worker_id = %s",
            ("stopped", utc_now(), self._tenant, str(worker_id)),
        )

    def list_workers(self) -> List[Dict[str, Any]]:
        rows = self._db.query(
            "SELECT worker_id, pid, started_at, heartbeat_at, status FROM workers"
            " WHERE tenant_id = %s ORDER BY started_at DESC",
            (self._tenant,),
        )
        now = _now()
        for row in rows:
            beat = _parse(row.get("heartbeat_at"))
            row["seconds_since_heartbeat"] = (
                round((now - beat).total_seconds(), 3) if beat is not None else None
            )
        return rows
