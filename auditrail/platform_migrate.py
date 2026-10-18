"""Phase 5 file -> database platform migration.

Moves platform metadata from the Phase 0-4 JSON files into the database.
Hard rules:

- **dry-run by default**: ``dry_run=True`` performs all reads, validation
  and duplicate detection without writing anything;
- **idempotent**: rows carry ``source_ref`` (``file:<path>#<key>``) and a
  ``migration_id``; re-running yields duplicates, never double inserts;
- **non-blocking**: one malformed record is counted ``invalid`` with a
  warning and the migration continues;
- **non-destructive**: source files are never modified or deleted; run
  evidence under ``runs/`` is never touched (runs are not migrated — the
  file contract stays authoritative);
- **rollback** deletes only rows carrying the given ``migration_id`` — no
  ``DROP TABLE``, no destructive operations.

This module is core logic: no ``backend`` imports.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .db.engine import Database, dumps_json, loads_json
from .run_contract import utc_now

MIGRATION_SOURCE = "file"
MIGRATION_TARGET = "database"

#: Bounded per-entity problem detail in reports.
_MAX_PROBLEMS = 50

Report = Dict[str, Any]


def _new_entity_report() -> Dict[str, Any]:
    return {"total": 0, "imported": 0, "skipped": 0, "duplicate": 0, "invalid": 0, "warnings": [], "errors": []}


def _problem(report: Dict[str, Any], key: str, message: str) -> None:
    report["invalid"] += 1
    if len(report[key]) < _MAX_PROBLEMS:
        report[key].append(str(message)[:300])


# ---------------------------------------------------------------------------
# file readers (Phase 0-4 layouts)
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _read_list_file(path: Path) -> List[Dict[str, Any]]:
    data = _read_json(path)
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [item for item in data["items"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            records.append({"__invalid__": True, "__line__": line[:80]})
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _read_dir_json(directory: Path) -> List[Tuple[str, Dict[str, Any]]]:
    if not directory.is_dir():
        return []
    out: List[Tuple[str, Dict[str, Any]]] = []
    for path in sorted(directory.glob("*.json")):
        data = _read_json(path)
        if isinstance(data, dict):
            out.append((path.name, data))
    return out


# ---------------------------------------------------------------------------
# entity specs: (name, reader, key extractor, insert)
# ---------------------------------------------------------------------------

_AGENTS_COLS = (
    "tenant_id, agent_id, name, version, adapter, endpoint, capabilities_json, metadata_json,"
    " max_concurrency, description, environment, tags_json, enabled, source_host, config_hash,"
    " created_at, updated_at, source_ref, migration_id"
)


def _insert(table: str, columns: str, values: Tuple[Any, ...], db: Database, dry_run: bool, conflict: str) -> bool:
    """Insert one row; returns True when (would be) inserted, False for a
    duplicate. Dry-run performs only the existence check (no writes), so its
    report shows what *would* be imported."""
    select_sql = "SELECT 1 FROM %s WHERE %s LIMIT 1" % (table, conflict[0])
    exists = db.query_one(select_sql, conflict[1]) is not None
    if exists:
        return False
    if dry_run:
        return True
    placeholders = ", ".join(["%s"] * len(values))
    db.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, columns, placeholders), values)
    return True


def migrate_platform(
    platform_dir: Path,
    db: Database,
    *,
    dry_run: bool = True,
    tenant_id: str = "default",
) -> Report:
    """Migrate file-based platform metadata into the database.

    Returns the structured report; on a real run also registers the
    ``migration_runs`` row used by verify/rollback.
    """
    platform_dir = Path(platform_dir)
    migration_id = "mig-%s" % uuid.uuid4().hex[:12]
    tenant = str(tenant_id or "default")
    report: Report = {
        "schema_version": 1,
        "migration_id": migration_id,
        "source": MIGRATION_SOURCE,
        "target": MIGRATION_TARGET,
        "dry_run": bool(dry_run),
        "tenant_id": tenant,
        "started_at": utc_now(),
        "entities": {},
        "warnings": [],
        "errors": [],
    }

    now = utc_now()

    def source_ref(path: str, key: str) -> str:
        return "file:%s#%s" % (path, key)

    # ---- agents ---------------------------------------------------------
    entity = _new_entity_report()
    for record in _read_list_file(platform_dir / "agents.json"):
        entity["total"] += 1
        agent_id = str(record.get("agent_id") or "").strip()
        if not agent_id:
            _problem(entity, "errors", "agent without agent_id")
            continue
        ref = source_ref("agents.json", agent_id)
        values = (
            tenant, agent_id, record.get("name"), record.get("version"), record.get("adapter"),
            record.get("endpoint"), dumps_json(record.get("capabilities") or []),
            dumps_json(record.get("metadata") or {}), record.get("max_concurrency"),
            record.get("description"), record.get("environment"), dumps_json(record.get("tags") or []),
            (1 if record.get("enabled", True) else 0), record.get("source_host"), record.get("config_hash"),
            record.get("created_at"), record.get("updated_at"), ref, migration_id,
        )
        conflict = ("tenant_id = %s AND agent_id = %s", (tenant, agent_id))
        try:
            if _insert("agents", _AGENTS_COLS, values, db, dry_run, conflict):
                entity["imported"] += 1
            else:
                entity["duplicate"] += 1
        except Exception as exc:
            _problem(entity, "errors", "agent %s: %s" % (agent_id, type(exc).__name__))
    report["entities"]["agents"] = entity

    # ---- test cases ------------------------------------------------------
    entity = _new_entity_report()
    for record in _read_list_file(platform_dir / "test_cases.json"):
        entity["total"] += 1
        case_id = str(record.get("case_id") or record.get("id") or "").strip()
        if not case_id:
            _problem(entity, "errors", "test case without case_id")
            continue
        values = (
            tenant, case_id, record.get("adapter"), dumps_json(record),
            record.get("created_at"), record.get("updated_at"),
            source_ref("test_cases.json", case_id), migration_id,
        )
        conflict = ("tenant_id = %s AND case_id = %s", (tenant, case_id))
        try:
            if _insert("test_cases", "tenant_id, case_id, adapter, payload_json, created_at, updated_at, source_ref, migration_id", values, db, dry_run, conflict):
                entity["imported"] += 1
            else:
                entity["duplicate"] += 1
        except Exception as exc:
            _problem(entity, "errors", "test case %s: %s" % (case_id, type(exc).__name__))
    report["entities"]["test_cases"] = entity

    # ---- test suites ------------------------------------------------------
    entity = _new_entity_report()
    for record in _read_list_file(platform_dir / "test_suites.json"):
        entity["total"] += 1
        suite_id = str(record.get("suite_id") or "").strip()
        if not suite_id:
            _problem(entity, "errors", "test suite without suite_id")
            continue
        values = (
            tenant, suite_id, record.get("version"), dumps_json(record),
            record.get("created_at"), record.get("updated_at"),
            source_ref("test_suites.json", suite_id), migration_id,
        )
        conflict = ("tenant_id = %s AND suite_id = %s", (tenant, suite_id))
        try:
            if _insert("test_suites", "tenant_id, suite_id, version, payload_json, created_at, updated_at, source_ref, migration_id", values, db, dry_run, conflict):
                entity["imported"] += 1
            else:
                entity["duplicate"] += 1
        except Exception as exc:
            _problem(entity, "errors", "test suite %s: %s" % (suite_id, type(exc).__name__))
    report["entities"]["test_suites"] = entity

    # ---- suite jobs --------------------------------------------------------
    entity = _new_entity_report()
    for name, record in _read_dir_json(platform_dir / "suite_jobs"):
        entity["total"] += 1
        job_id = str(record.get("job_id") or "").strip()
        if not job_id:
            _problem(entity, "errors", "suite job without job_id (%s)" % name)
            continue
        values = (
            tenant, job_id, record.get("suite_id"), record.get("agent_id"), record.get("status"),
            dumps_json(record), record.get("created_at"), record.get("updated_at") or now,
            source_ref("suite_jobs/%s" % name, job_id), migration_id,
        )
        conflict = ("tenant_id = %s AND job_id = %s", (tenant, job_id))
        try:
            if _insert("suite_jobs", "tenant_id, job_id, suite_id, agent_id, status, payload_json, created_at, updated_at, source_ref, migration_id", values, db, dry_run, conflict):
                entity["imported"] += 1
            else:
                entity["duplicate"] += 1
        except Exception as exc:
            _problem(entity, "errors", "suite job %s: %s" % (job_id, type(exc).__name__))
    report["entities"]["suite_jobs"] = entity

    # ---- import jobs -------------------------------------------------------
    entity = _new_entity_report()
    imports_dir = platform_dir / "external" / "imports"
    for name, record in _read_dir_json(imports_dir):
        if name == "payloads":
            continue
        entity["total"] += 1
        import_id = str(record.get("import_id") or "").strip()
        if not import_id:
            _problem(entity, "errors", "import record without import_id (%s)" % name)
            continue
        error = record.get("error") if isinstance(record.get("error"), dict) else {}
        values = (
            tenant, import_id, str(record.get("source_system") or "unknown")[:64],
            record.get("source_type"), record.get("kind"), str(record.get("status") or "completed"),
            record.get("phase"), record.get("mode"), (1 if record.get("dry_run") else 0),
            record.get("worker_id"), int(record.get("lease_version") or 0),
            record.get("heartbeat_at"), record.get("lease_expires_at"),
            int(record.get("attempt_count") or 0), int(record.get("max_attempts") or 5),
            record.get("next_retry_at"), (1 if record.get("cancel_requested") else 0),
            (1 if record.get("pause_requested") else 0),
            error.get("code"), (None if not error else (1 if error.get("retryable") else 0)),
            int(record.get("processed_records") or 0), int(record.get("imported_records") or 0),
            (dumps_json(record.get("checkpoint")) if isinstance(record.get("checkpoint"), dict) else None),
            dumps_json(record), record.get("idempotency_key"),
            record.get("request_fingerprint") or record.get("source_fingerprint"),
            record.get("actor"), 0,
            record.get("created_at") or record.get("imported_at") or now, now,
            record.get("started_at"), record.get("finished_at"),
            source_ref("external/imports/%s" % name, import_id), migration_id,
        )
        conflict = ("tenant_id = %s AND import_id = %s", (tenant, import_id))
        columns = (
            "tenant_id, import_id, source_system, source_type, kind, status, phase, mode, dry_run,"
            " worker_id, lease_version, heartbeat_at, lease_expires_at, attempt_count, max_attempts,"
            " next_retry_at, cancel_requested, pause_requested, last_error_code, last_error_retryable,"
            " processed_records, imported_records, checkpoint_json, payload_json, idempotency_key,"
            " request_fingerprint, actor, queue_degraded, created_at, updated_at, started_at, finished_at,"
            " source_ref, migration_id"
        )
        try:
            if _insert("import_jobs", columns, values, db, dry_run, conflict):
                entity["imported"] += 1
            else:
                entity["duplicate"] += 1
        except Exception as exc:
            _problem(entity, "errors", "import job %s: %s" % (import_id, type(exc).__name__))
    report["entities"]["import_jobs"] = entity

    # ---- external traces / evaluations -------------------------------------
    for entity_name, filename, table in (
        ("external_traces", "traces.jsonl", "external_traces"),
        ("external_evaluations", "evaluations.jsonl", "external_evaluations"),
    ):
        entity = _new_entity_report()
        for record in _read_jsonl(platform_dir / "external" / filename):
            entity["total"] += 1
            if record.get("__invalid__"):
                _problem(entity, "errors", "%s: unparsable line" % filename)
                continue
            source_system = str(record.get("source_system") or "").strip()
            source_id = str(record.get("source_id") or "").strip()
            if not source_system or not source_id:
                _problem(entity, "errors", "%s record without source_system/source_id" % filename)
                continue
            values = (
                tenant, source_system[:64], source_id[:128], record.get("run_id"), record.get("agent_id"),
                dumps_json(record), record.get("imported_at") or now, now,
                source_ref("external/%s" % filename, "%s:%s" % (source_system, source_id)), migration_id,
            )
            conflict = ("tenant_id = %s AND source_system = %s AND source_id = %s", (tenant, source_system[:64], source_id[:128]))
            try:
                if _insert(table, "tenant_id, source_system, source_id, run_id, agent_id, payload_json, created_at, updated_at, source_ref, migration_id", values, db, dry_run, conflict):
                    entity["imported"] += 1
                else:
                    entity["duplicate"] += 1
            except Exception as exc:
                _problem(entity, "errors", "%s %s: %s" % (filename, source_id, type(exc).__name__))
        report["entities"][entity_name] = entity

    # ---- cost records ------------------------------------------------------
    entity = _new_entity_report()
    for record in _read_jsonl(platform_dir / "cost_records.jsonl"):
        entity["total"] += 1
        if record.get("__invalid__"):
            _problem(entity, "errors", "cost_records: unparsable line")
            continue
        source_system = str(record.get("source_system") or "").strip()
        source_id = str(record.get("source_id") or "").strip()
        if not source_system or not source_id:
            _problem(entity, "errors", "cost record without source_system/source_id")
            continue
        values = (
            tenant, str(record.get("record_id") or "")[:64], source_system[:64], source_id[:128],
            record.get("run_id"), record.get("agent_id"), record.get("model"), record.get("total_cost"),
            record.get("currency"), record.get("source"), record.get("price_version"),
            dumps_json(record), record.get("imported_at") or now, now,
            source_ref("cost_records.jsonl", "%s:%s" % (source_system, source_id)), migration_id,
        )
        conflict = ("tenant_id = %s AND source_system = %s AND source_id = %s", (tenant, source_system[:64], source_id[:128]))
        try:
            if _insert("cost_records", "tenant_id, record_id, source_system, source_id, run_id, agent_id, model, total_cost, currency, source, price_version, payload_json, created_at, updated_at, source_ref, migration_id", values, db, dry_run, conflict):
                entity["imported"] += 1
            else:
                entity["duplicate"] += 1
        except Exception as exc:
            _problem(entity, "errors", "cost record %s: %s" % (source_id, type(exc).__name__))
    report["entities"]["cost_records"] = entity

    # ---- price versions ----------------------------------------------------
    entity = _new_entity_report()
    for name, record in _read_dir_json(platform_dir / "price_versions"):
        entity["total"] += 1
        price_version = str(record.get("price_version") or name[:-5]).strip()
        if not price_version:
            _problem(entity, "errors", "price version without id (%s)" % name)
            continue
        values = (
            tenant, price_version, record.get("status"), dumps_json(record),
            record.get("created_at"), record.get("updated_at"),
            source_ref("price_versions/%s" % name, price_version), migration_id,
        )
        conflict = ("tenant_id = %s AND price_version = %s", (tenant, price_version))
        try:
            if _insert("price_versions", "tenant_id, price_version, status, payload_json, created_at, updated_at, source_ref, migration_id", values, db, dry_run, conflict):
                entity["imported"] += 1
            else:
                entity["duplicate"] += 1
        except Exception as exc:
            _problem(entity, "errors", "price version %s: %s" % (price_version, type(exc).__name__))
    report["entities"]["price_versions"] = entity

    # ---- audit events -------------------------------------------------------
    entity = _new_entity_report()
    for record in _read_jsonl(platform_dir / "audit" / "audit_events.jsonl"):
        entity["total"] += 1
        if record.get("__invalid__"):
            _problem(entity, "errors", "audit_events: unparsable line")
            continue
        event_id = str(record.get("event_id") or "").strip()
        if not event_id:
            _problem(entity, "errors", "audit event without event_id")
            continue
        values = (
            tenant, event_id, str(record.get("actor") or "unknown")[:64], record.get("role"),
            str(record.get("action") or "unknown"), record.get("resource_type"), record.get("resource_id"),
            record.get("result"), record.get("reason"), record.get("request_id"),
            record.get("timestamp") or now, now,
            source_ref("audit/audit_events.jsonl", event_id), migration_id,
        )
        conflict = ("tenant_id = %s AND event_id = %s", (tenant, event_id))
        try:
            if _insert("audit_events", "tenant_id, event_id, actor, role, action, resource_type, resource_id, result, reason, request_id, timestamp, created_at, source_ref, migration_id", values, db, dry_run, conflict):
                entity["imported"] += 1
            else:
                entity["duplicate"] += 1
        except Exception as exc:
            _problem(entity, "errors", "audit event %s: %s" % (event_id, type(exc).__name__))
    report["entities"]["audit_events"] = entity

    # ---- run index entries (cache copy; runs themselves are never migrated)
    entity = _new_entity_report()
    index_data = _read_json(platform_dir / "run_index.json")
    entries = index_data.get("entries") if isinstance(index_data, dict) else None
    if isinstance(entries, dict):
        for run_id, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            entity["total"] += 1
            run_id_text = str(run_id).strip()
            if not run_id_text:
                _problem(entity, "errors", "run index entry without run_id")
                continue
            values = (
                tenant, run_id_text, entry.get("agent_id"), entry.get("signature"),
                dumps_json(entry), now,
                source_ref("run_index.json", run_id_text), migration_id,
            )
            conflict = ("tenant_id = %s AND run_id = %s", (tenant, run_id_text))
            try:
                if _insert("run_index_entries", "tenant_id, run_id, agent_id, signature, payload_json, updated_at, source_ref, migration_id", values, db, dry_run, conflict):
                    entity["imported"] += 1
                else:
                    entity["duplicate"] += 1
            except Exception as exc:
                _problem(entity, "errors", "run index %s: %s" % (run_id_text, type(exc).__name__))
    report["entities"]["run_index_entries"] = entity

    report["finished_at"] = utc_now()
    report["status"] = "completed"

    if not dry_run:
        db.execute(
            "INSERT INTO migration_runs (migration_id, source, target, dry_run, status, started_at, finished_at, stats_json)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (migration_id) DO UPDATE SET status = excluded.status, finished_at = excluded.finished_at,"
            " stats_json = excluded.stats_json",
            (
                migration_id, MIGRATION_SOURCE, MIGRATION_TARGET, 0, "completed",
                report["started_at"], report["finished_at"], dumps_json(_summary(report)),
            ),
        )
    return report


def _summary(report: Report) -> Dict[str, Any]:
    return {
        name: {key: counts[key] for key in ("total", "imported", "skipped", "duplicate", "invalid")}
        for name, counts in (report.get("entities") or {}).items()
    }


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------


def _valid_source_records(path: Path) -> List[Dict[str, Any]]:
    """JSONL records that pass the migration's validity rule (parseable and
    carrying source_system + source_id) — the same rule the migrator
    applies, so verify counts are comparable."""
    return [
        record
        for record in _read_jsonl(path)
        if not record.get("__invalid__")
        and str(record.get("source_system") or "").strip()
        and str(record.get("source_id") or "").strip()
    ]


def _file_counts(platform_dir: Path) -> Dict[str, int]:
    platform_dir = Path(platform_dir)
    counts = {
        "agents": len(_read_list_file(platform_dir / "agents.json")),
        "test_cases": len(_read_list_file(platform_dir / "test_cases.json")),
        "test_suites": len(_read_list_file(platform_dir / "test_suites.json")),
        "suite_jobs": len(_read_dir_json(platform_dir / "suite_jobs")),
        "import_jobs": len([
            item for item in _read_dir_json(platform_dir / "external" / "imports")
        ]),
        "external_traces": len(_valid_source_records(platform_dir / "external" / "traces.jsonl")),
        "external_evaluations": len(_valid_source_records(platform_dir / "external" / "evaluations.jsonl")),
        "cost_records": len(_valid_source_records(platform_dir / "cost_records.jsonl")),
        "price_versions": len(_read_dir_json(platform_dir / "price_versions")),
        "audit_events": len([
            record
            for record in _read_jsonl(platform_dir / "audit" / "audit_events.jsonl")
            if not record.get("__invalid__") and str(record.get("event_id") or "").strip()
        ]),
    }
    return counts


_DB_COUNT_QUERIES = {
    "agents": "SELECT COUNT(*) AS c FROM agents WHERE tenant_id = %s AND deleted_at IS NULL",
    "test_cases": "SELECT COUNT(*) AS c FROM test_cases WHERE tenant_id = %s AND deleted_at IS NULL",
    "test_suites": "SELECT COUNT(*) AS c FROM test_suites WHERE tenant_id = %s AND deleted_at IS NULL",
    "suite_jobs": "SELECT COUNT(*) AS c FROM suite_jobs WHERE tenant_id = %s",
    "import_jobs": "SELECT COUNT(*) AS c FROM import_jobs WHERE tenant_id = %s",
    "external_traces": "SELECT COUNT(*) AS c FROM external_traces WHERE tenant_id = %s",
    "external_evaluations": "SELECT COUNT(*) AS c FROM external_evaluations WHERE tenant_id = %s",
    "cost_records": "SELECT COUNT(*) AS c FROM cost_records WHERE tenant_id = %s",
    "price_versions": "SELECT COUNT(*) AS c FROM price_versions WHERE tenant_id = %s",
    "audit_events": "SELECT COUNT(*) AS c FROM audit_events WHERE tenant_id = %s",
}


def verify_platform_migration(
    platform_dir: Path,
    db: Database,
    *,
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """Compare file-side counts with database counts per entity.

    ``consistent`` is True when every file-side record is represented in the
    database (database may legitimately hold *more* — records created
    directly in database mode have no file counterpart).
    """
    tenant = str(tenant_id or "default")
    file_counts = _file_counts(Path(platform_dir))
    entities: Dict[str, Any] = {}
    consistent = True
    for name, query in _DB_COUNT_QUERIES.items():
        row = db.query_one(query, (tenant,))
        db_count = int(row["c"]) if row else 0
        file_count = file_counts.get(name, 0)
        match = db_count >= file_count
        if not match:
            consistent = False
        entities[name] = {"file": file_count, "database": db_count, "consistent": match}
    return {
        "schema_version": 1,
        "tenant_id": tenant,
        "consistent": consistent,
        "entities": entities,
        "checked_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------

_ROLLBACK_TABLES = (
    "agents",
    "test_cases",
    "test_suites",
    "suite_jobs",
    "import_jobs",
    "external_traces",
    "external_evaluations",
    "cost_records",
    "price_versions",
    "audit_events",
    "run_index_entries",
)


def rollback_platform_migration(
    db: Database,
    migration_id: str,
    *,
    dry_run: bool = True,
    tenant_id: str = "default",
) -> Dict[str, Any]:
    """Delete only rows carrying ``migration_id`` (never files, never runs,
    never whole tables). Dry-run reports what would be deleted."""
    tenant = str(tenant_id or "default")
    migration_id = str(migration_id or "").strip()
    if not migration_id:
        raise ValueError("migration_id is required")
    report: Dict[str, Any] = {
        "schema_version": 1,
        "migration_id": migration_id,
        "dry_run": bool(dry_run),
        "tenant_id": tenant,
        "tables": {},
        "started_at": utc_now(),
    }
    with db.transaction(immediate=True):
        for table in _ROLLBACK_TABLES:
            row = db.query_one(
                "SELECT COUNT(*) AS c FROM %s WHERE tenant_id = %%s AND migration_id = %%s" % table,
                (tenant, migration_id),
            )
            count = int(row["c"]) if row else 0
            deleted = 0
            if count and not dry_run:
                deleted = db.execute(
                    "DELETE FROM %s WHERE tenant_id = %%s AND migration_id = %%s" % table,
                    (tenant, migration_id),
                )
            report["tables"][table] = {"matched": count, "deleted": deleted if not dry_run else 0}
        if not dry_run:
            db.execute(
                "UPDATE migration_runs SET status = %s, finished_at = %s WHERE migration_id = %s",
                ("rolled_back", utc_now(), migration_id),
            )
    report["finished_at"] = utc_now()
    report["status"] = "completed"
    report["note"] = "仅删除带本次 migration_id 标记的数据库记录；文件与 runs/ 证据不受影响"
    return report


def latest_migration_id(db: Database) -> Optional[str]:
    row = db.query_one(
        "SELECT migration_id FROM migration_runs WHERE status = %s ORDER BY started_at DESC LIMIT 1",
        ("completed",),
    )
    return str(row["migration_id"]) if row else None
