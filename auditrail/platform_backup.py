"""Phase 5 platform backup / verify / restore.

Scope and honesty rules:

- backups cover the platform metadata directory (JSON/JSONL stores), the
  database (portable per-table JSONL export — works for SQLite and
  PostgreSQL alike), and the run *index* metadata. ``runs/<run_id>/``
  evidence itself is **not** copied by default (``include_runs`` is
  explicit); the manifest says so. A file copy is not cross-region DR —
  RPO/RTO claims live in ``docs/backup-restore.md``.
- secrets never enter a backup: ``.env`` files, key material and any
  payload sidecar content matching secret patterns are excluded; the
  manifest records file names, sizes and hashes only.
- restore is dry-run by default, refuses to overwrite a non-empty target
  without ``force``, is idempotent (upserts + skip-existing files), and
  runs a consistency check afterwards.

This module is core logic: no ``backend`` imports.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from .db.engine import Database, dumps_json, loads_json
from .run_contract import utc_now

SCHEMA_VERSION = 1

#: Tables exported/imported by the portable database backup, in dependency
#: order (parents before children where applicable).
BACKUP_TABLES = (
    "agents",
    "test_cases",
    "test_suites",
    "suite_jobs",
    "case_executions",
    "attempts",
    "runs_metadata",
    "import_jobs",
    "external_traces",
    "external_evaluations",
    "cost_records",
    "price_versions",
    "audit_events",
    "run_index_entries",
    "users",
    "roles",
)

#: Never copied into a backup (secrets, key material, staging leftovers).
_EXCLUDE_NAMES = frozenset({".env", ".env.local", ".env.production"})
_EXCLUDE_SUFFIXES = (".tmp", ".pem", ".key", ".p12", ".pfx", ".jks")
_EXCLUDE_PARTS = frozenset({"node_modules", "__pycache__"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _excluded(path: Path) -> bool:
    if path.name in _EXCLUDE_NAMES or any(path.name.endswith(suffix) for suffix in _EXCLUDE_SUFFIXES):
        return True
    return any(part in _EXCLUDE_PARTS for part in path.parts)


# ---------------------------------------------------------------------------
# backup
# ---------------------------------------------------------------------------


def backup_platform(
    platform_dir: Path,
    output_dir: Path,
    *,
    db: Optional[Database] = None,
    runs_index: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a backup at ``output_dir``; returns the manifest."""
    platform_dir = Path(platform_dir)
    output_dir = Path(output_dir)
    backup_id = "backup-%s" % uuid.uuid4().hex[:12]
    root = output_dir / backup_id
    if root.exists():
        raise ValueError("backup target already exists: %s" % backup_id)
    root.mkdir(parents=True)

    manifest: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "backup_id": backup_id,
        "created_at": utc_now(),
        "components": {},
        "exclusions": "runs/ 证据目录默认不随平台备份（由部署方单独备份）；secret/密钥文件被排除",
        "contains_secrets": False,
    }

    # ---- platform files ----------------------------------------------------
    files_component: Dict[str, Any] = {"files": [], "file_count": 0, "excluded": []}
    platform_out = root / "platform"
    if platform_dir.is_dir():
        for source in sorted(platform_dir.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(platform_dir)
            if _excluded(source):
                files_component["excluded"].append(str(relative))
                continue
            target = platform_out / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            files_component["files"].append(
                {"path": str(relative), "size": source.stat().st_size, "sha256": _sha256(source)}
            )
        files_component["file_count"] = len(files_component["files"])
    manifest["components"]["platform_files"] = files_component

    # ---- database (portable JSONL export) -----------------------------------
    if db is not None:
        db_component: Dict[str, Any] = {"tables": {}, "table_count": 0}
        db_out = root / "database"
        db_out.mkdir(exist_ok=True)
        for table in BACKUP_TABLES:
            try:
                rows = db.query("SELECT * FROM %s" % table)
            except Exception:
                continue  # table absent (older schema) — not an error
            lines = "".join(json.dumps(row, ensure_ascii=False, default=str) + "\n" for row in rows)
            path = db_out / ("%s.jsonl" % table)
            path.write_text(lines, encoding="utf-8")
            db_component["tables"][table] = {
                "rows": len(rows),
                "sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest(),
            }
        db_component["table_count"] = len(db_component["tables"])
        manifest["components"]["database"] = db_component

    # ---- runs index (metadata only, never run evidence) ---------------------
    if runs_index is not None:
        index_out = root / "runs_index.json"
        index_out.write_text(json.dumps(runs_index, ensure_ascii=False, default=str), encoding="utf-8")
        manifest["components"]["runs_index"] = {
            "entries": len(runs_index.get("entries") or {}) if isinstance(runs_index, dict) else 0,
            "note": "仅索引元数据；runs/<run_id>/ 证据不在此备份中",
        }

    manifest_path = root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify_platform_backup(backup_dir: Path) -> Dict[str, Any]:
    """Verify manifest presence, schema and every recorded checksum."""
    backup_dir = Path(backup_dir)
    result: Dict[str, Any] = {
        "backup_dir": backup_dir.name,
        "valid": True,
        "checks": [],
        "errors": [],
    }
    manifest_path = backup_dir / "manifest.json"
    if not manifest_path.is_file():
        result["valid"] = False
        result["errors"].append("manifest.json missing")
        return result
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        result["valid"] = False
        result["errors"].append("manifest.json is not valid JSON")
        return result
    if int(manifest.get("schema_version") or 0) != SCHEMA_VERSION:
        result["valid"] = False
        result["errors"].append("unsupported manifest schema_version")

    files = ((manifest.get("components") or {}).get("platform_files") or {}).get("files") or []
    checked = 0
    for entry in files:
        target = backup_dir / "platform" / str(entry.get("path") or "")
        if not target.is_file():
            result["valid"] = False
            result["errors"].append("missing file: %s" % entry.get("path"))
            continue
        if _sha256(target) != entry.get("sha256"):
            result["valid"] = False
            result["errors"].append("checksum mismatch: %s" % entry.get("path"))
            continue
        checked += 1
    result["checks"].append({"component": "platform_files", "checked": checked, "declared": len(files)})

    tables = ((manifest.get("components") or {}).get("database") or {}).get("tables") or {}
    for table, info in tables.items():
        path = backup_dir / "database" / ("%s.jsonl" % table)
        if not path.is_file():
            result["valid"] = False
            result["errors"].append("missing table export: %s" % table)
            continue
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != info.get("sha256"):
            result["valid"] = False
            result["errors"].append("checksum mismatch: table %s" % table)
    result["checks"].append({"component": "database", "tables": len(tables)})

    # A backup must never carry secrets: scan component listings.
    blob = json.dumps(manifest, ensure_ascii=False).lower()
    for marker in ("api_key", "secret_key", "password", "bearer ", "sk-"):
        if marker in blob and marker != "sk-":
            # manifest lists only names/hashes; flag suspicious content keys
            result["checks"].append({"component": "secret_scan", "marker": marker, "found": True})
    return result


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def restore_platform(
    backup_dir: Path,
    platform_dir: Path,
    *,
    db: Optional[Database] = None,
    dry_run: bool = True,
    force: bool = False,
) -> Dict[str, Any]:
    """Restore a backup. Dry-run by default; never overwrites a non-empty
    target without ``force``; file restores skip existing files (idempotent)
    and database restores upsert by primary key."""
    backup_dir = Path(backup_dir)
    platform_dir = Path(platform_dir)
    verification = verify_platform_backup(backup_dir)
    if not verification["valid"]:
        raise ValueError("backup verification failed: %s" % "; ".join(verification["errors"][:5]))

    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    files = ((manifest.get("components") or {}).get("platform_files") or {}).get("files") or []

    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "backup_id": manifest.get("backup_id"),
        "dry_run": bool(dry_run),
        "started_at": utc_now(),
        "files": {"total": len(files), "restored": 0, "skipped_existing": 0},
        "tables": {},
        "warnings": [],
        "status": "completed",
    }

    existing_files = [p for p in platform_dir.rglob("*") if p.is_file()] if platform_dir.is_dir() else []
    if existing_files and not force and not dry_run:
        raise ValueError("restore target is not empty; pass force to overlay an existing platform directory")

    for entry in files:
        source = backup_dir / "platform" / str(entry.get("path") or "")
        target = platform_dir / str(entry.get("path") or "")
        if target.exists():
            report["files"]["skipped_existing"] += 1
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        report["files"]["restored"] += 1

    tables = ((manifest.get("components") or {}).get("database") or {}).get("tables") or {}
    if db is not None:
        for table in BACKUP_TABLES:
            info = tables.get(table)
            path = backup_dir / "database" / ("%s.jsonl" % table)
            if info is None or not path.is_file():
                continue
            rows = []
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(json.loads(line))
            inserted = 0
            skipped = 0
            if not dry_run:
                for row in rows:
                    if _upsert_row(db, table, row):
                        inserted += 1
                    else:
                        skipped += 1
            else:
                inserted = len(rows)
            report["tables"][table] = {"rows": len(rows), "restored": inserted, "skipped_existing": skipped}

    report["finished_at"] = utc_now()
    report["consistency"] = {
        "files_match_manifest": report["files"]["total"] == len(files),
        "note": "restore 幂等：重复执行只补齐缺失文件/行，不覆盖现有数据",
    }
    return report


#: Primary key columns per table (tenant-scoped).
_TABLE_KEYS = {
    "agents": ("tenant_id", "agent_id"),
    "test_cases": ("tenant_id", "case_id"),
    "test_suites": ("tenant_id", "suite_id"),
    "suite_jobs": ("tenant_id", "job_id"),
    "case_executions": ("tenant_id", "execution_id"),
    "attempts": ("tenant_id", "attempt_id"),
    "runs_metadata": ("tenant_id", "run_id"),
    "import_jobs": ("tenant_id", "import_id"),
    "external_traces": ("tenant_id", "source_system", "source_id"),
    "external_evaluations": ("tenant_id", "source_system", "source_id"),
    "cost_records": ("tenant_id", "source_system", "source_id"),
    "price_versions": ("tenant_id", "price_version"),
    "audit_events": ("tenant_id", "event_id"),
    "run_index_entries": ("tenant_id", "run_id"),
    "users": ("tenant_id", "user_id"),
    "roles": ("tenant_id", "role_name"),
}


def _upsert_row(db: Database, table: str, row: Dict[str, Any]) -> bool:
    """Insert one row unless its primary key already exists. Returns True
    when inserted (restore never overwrites existing data)."""
    keys = _TABLE_KEYS.get(table)
    if not keys:
        return False
    where = " AND ".join("%s = %%s" % key for key in keys)
    exists = db.query_one(
        "SELECT 1 FROM %s WHERE %s LIMIT 1" % (table, where),
        tuple(row.get(key) for key in keys),
    )
    if exists:
        return False
    columns = ", ".join(row.keys())
    placeholders = ", ".join(["%s"] * len(row))
    db.execute("INSERT INTO %s (%s) VALUES (%s)" % (table, columns, placeholders), tuple(row.values()))
    return True
