"""Phase 5 versioned schema migrations.

Migrations are explicit, versioned and idempotent: each entry is applied in
its own transaction and recorded in ``schema_migrations`` only after it
commits, so a failed migration is never marked applied and a re-run is
always safe. The application never creates tables implicitly at startup —
``auditrail migrate-db`` (or the deploy pipeline) applies them, and the API
readiness check only *reports* pending migrations.

Both dialects share one DDL set: TEXT/INTEGER/REAL columns, UTC ISO-8601
timestamps as TEXT, JSON payloads as TEXT. Natural keys carry uniqueness;
every metadata table has ``tenant_id`` first (single-tenant ``default``
today, the constraint shape is multi-tenant-ready).

The database holds platform metadata and indexes only. Run evidence stays in
``runs/<run_id>/``; no trajectory, full prompt, chain-of-thought or raw
external payload column exists here by design.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .engine import Database, MigrationError
from ..run_contract import utc_now

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# migration 1: initial Phase 5 schema
# ---------------------------------------------------------------------------

_INITIAL_TABLES = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agents (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        agent_id TEXT NOT NULL,
        name TEXT,
        version TEXT,
        adapter TEXT,
        endpoint TEXT,
        capabilities_json TEXT,
        metadata_json TEXT,
        max_concurrency INTEGER,
        description TEXT,
        environment TEXT,
        tags_json TEXT,
        enabled INTEGER,
        source_host TEXT,
        config_hash TEXT,
        created_at TEXT,
        updated_at TEXT,
        deleted_at TEXT,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS test_suites (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        suite_id TEXT NOT NULL,
        version INTEGER,
        payload_json TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT,
        deleted_at TEXT,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, suite_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS test_cases (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        case_id TEXT NOT NULL,
        adapter TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT,
        deleted_at TEXT,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, case_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suite_jobs (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        job_id TEXT NOT NULL,
        suite_id TEXT,
        agent_id TEXT,
        status TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS case_executions (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        execution_id TEXT NOT NULL,
        job_id TEXT,
        case_id TEXT,
        status TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT,
        PRIMARY KEY (tenant_id, execution_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attempts (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        attempt_id TEXT NOT NULL,
        execution_id TEXT,
        status TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT,
        PRIMARY KEY (tenant_id, attempt_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS runs_metadata (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        run_id TEXT NOT NULL,
        agent_id TEXT,
        status TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_jobs (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        import_id TEXT NOT NULL,
        source_system TEXT NOT NULL,
        source_type TEXT,
        kind TEXT,
        status TEXT NOT NULL,
        phase TEXT,
        mode TEXT,
        dry_run INTEGER NOT NULL DEFAULT 0,
        worker_id TEXT,
        lease_version INTEGER NOT NULL DEFAULT 0,
        heartbeat_at TEXT,
        lease_expires_at TEXT,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 5,
        next_retry_at TEXT,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        pause_requested INTEGER NOT NULL DEFAULT 0,
        last_error_code TEXT,
        last_error_retryable INTEGER,
        processed_records INTEGER NOT NULL DEFAULT 0,
        imported_records INTEGER NOT NULL DEFAULT 0,
        checkpoint_json TEXT,
        payload_json TEXT NOT NULL,
        idempotency_key TEXT,
        request_fingerprint TEXT,
        actor TEXT,
        queue_degraded INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, import_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS import_jobs_idempotency_uq
        ON import_jobs (tenant_id, idempotency_key, request_fingerprint)
        WHERE idempotency_key IS NOT NULL
    """,
    """
    CREATE INDEX IF NOT EXISTS import_jobs_status_idx
        ON import_jobs (tenant_id, status)
    """,
    """
    CREATE TABLE IF NOT EXISTS external_traces (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        source_system TEXT NOT NULL,
        source_id TEXT NOT NULL,
        run_id TEXT,
        agent_id TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, source_system, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS external_evaluations (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        source_system TEXT NOT NULL,
        source_id TEXT NOT NULL,
        run_id TEXT,
        agent_id TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, source_system, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cost_records (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        record_id TEXT NOT NULL,
        source_system TEXT NOT NULL,
        source_id TEXT NOT NULL,
        run_id TEXT,
        agent_id TEXT,
        model TEXT,
        total_cost REAL,
        currency TEXT,
        source TEXT,
        price_version TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, source_system, source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS price_versions (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        price_version TEXT NOT NULL,
        status TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT,
        updated_at TEXT,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, price_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        event_id TEXT NOT NULL,
        actor TEXT NOT NULL,
        role TEXT,
        action TEXT NOT NULL,
        resource_type TEXT,
        resource_id TEXT,
        result TEXT,
        reason TEXT,
        request_id TEXT,
        timestamp TEXT NOT NULL,
        created_at TEXT NOT NULL,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, event_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS audit_events_resource_idx
        ON audit_events (tenant_id, resource_type, resource_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS run_index_entries (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        run_id TEXT NOT NULL,
        agent_id TEXT,
        signature TEXT,
        payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        source_ref TEXT,
        migration_id TEXT,
        PRIMARY KEY (tenant_id, run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS users (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        user_id TEXT NOT NULL,
        issuer TEXT,
        subject TEXT NOT NULL,
        email TEXT,
        display_name TEXT,
        roles_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        first_seen_at TEXT,
        last_seen_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, user_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS users_subject_uq
        ON users (tenant_id, issuer, subject)
    """,
    """
    CREATE TABLE IF NOT EXISTS roles (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        role_name TEXT NOT NULL,
        permissions_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, role_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS job_leases (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        job_id TEXT NOT NULL,
        worker_id TEXT NOT NULL,
        lease_version INTEGER NOT NULL,
        acquired_at TEXT NOT NULL,
        heartbeat_at TEXT,
        expires_at TEXT,
        status TEXT NOT NULL DEFAULT 'held',
        PRIMARY KEY (tenant_id, job_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS workers (
        tenant_id TEXT NOT NULL DEFAULT 'default',
        worker_id TEXT NOT NULL,
        pid INTEGER,
        started_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        PRIMARY KEY (tenant_id, worker_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS oidc_states (
        state TEXT PRIMARY KEY,
        nonce TEXT NOT NULL,
        redirect_uri TEXT,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS migration_runs (
        migration_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        target TEXT NOT NULL,
        dry_run INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        stats_json TEXT
    )
    """,
]

#: (version, name, statements). Append-only: never edit an applied migration.
MIGRATIONS: List[Any] = [
    (1, "initial_phase5_schema", _INITIAL_TABLES),
]


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def current_version(db: Database) -> int:
    """Highest applied migration; 0 when nothing has been applied."""
    try:
        row = db.query_one("SELECT MAX(version) AS version FROM schema_migrations")
    except Exception:
        return 0  # schema_migrations itself does not exist yet
    if row is None or row.get("version") is None:
        return 0
    return int(row["version"])


def pending_migrations(db: Database) -> List[Any]:
    applied = current_version(db)
    return [entry for entry in MIGRATIONS if int(entry[0]) > applied]


def apply_migrations(db: Database, *, target_version: Optional[int] = None) -> List[int]:
    """Apply pending migrations in order; returns the versions applied now.

    Each migration commits in its own transaction and is recorded only after
    success: a failure raises :class:`MigrationError` and leaves the failed
    version unapplied, so a re-run is always safe.
    """
    applied_now: List[int] = []
    for version, name, statements in MIGRATIONS:
        if int(version) <= current_version(db):
            continue
        if target_version is not None and int(version) > int(target_version):
            break
        try:
            with db.transaction(immediate=True):
                for statement in statements:
                    db.execute(statement)
                db.execute(
                    "INSERT INTO schema_migrations (version, name, applied_at) VALUES (%s, %s, %s)",
                    (int(version), str(name), utc_now()),
                )
        except Exception as exc:
            raise MigrationError(
                "migration %d (%s) failed: %s" % (int(version), name, type(exc).__name__)
            ) from exc
        applied_now.append(int(version))
    return applied_now


def migration_status(db: Database) -> Dict[str, Any]:
    """Readiness/admin view: applied versions and what is still pending."""
    rows: List[Dict[str, Any]] = []
    try:
        rows = db.query("SELECT version, name, applied_at FROM schema_migrations ORDER BY version")
    except Exception:
        rows = []
    applied = [int(row["version"]) for row in rows]
    pending = [int(entry[0]) for entry in MIGRATIONS if int(entry[0]) not in applied]
    return {
        "schema_version_target": max(int(entry[0]) for entry in MIGRATIONS),
        "current_version": max(applied) if applied else 0,
        "applied": rows,
        "pending": pending,
        "up_to_date": not pending,
    }
