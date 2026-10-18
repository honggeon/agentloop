"""Phase 5 file -> database platform migration and backup/restore.

All fixtures are tmp_path JSON files; nothing touches real ``platform/``,
``runs/`` or the network.
"""

import json

import pytest

from auditrail import platform_backup, platform_migrate
from auditrail.db import Database, apply_migrations


def _db(tmp_path, name="platform.db") -> Database:
    db = Database("sqlite:///%s" % (tmp_path / name))
    apply_migrations(db)
    return db


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload, encoding="utf-8")


def _seed_platform(root):
    platform = root / "platform"
    _write(platform / "agents.json", {"items": [
        {"agent_id": "agent-1", "name": "A1", "endpoint": "http://agent.internal", "capabilities": ["session"],
         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z"},
        {"agent_id": "agent-2", "name": "A2", "endpoint": "http://agent.internal", "capabilities": ["session"]},
    ]})
    _write(platform / "test_suites.json", {"items": [
        {"suite_id": "suite-1", "name": "S1", "case_ids": ["c-1"], "version": 1},
    ]})
    _write(platform / "suite_jobs" / "job-1.json", {"job_id": "job-1", "suite_id": "suite-1", "status": "completed"})
    _write(platform / "external" / "imports" / "import-1.json", {
        "record_type": "import_job", "import_id": "import-1", "source_system": "langfuse",
        "status": "completed", "created_at": "2026-01-02T00:00:00Z",
    })
    _write(platform / "external" / "traces.jsonl",
           '{"source_system": "langfuse", "source_id": "t-1", "run_id": "run-1"}\n'
           '{"source_system": "langfuse", "source_id": "t-2"}\n')
    _write(platform / "external" / "evaluations.jsonl",
           '{"source_system": "langfuse", "source_id": "e-1", "run_id": "run-1"}\n')
    _write(platform / "cost_records.jsonl",
           '{"source_system": "metrics_proxy", "source_id": "c-1", "record_id": "cost-1", "total_cost": 0.01, "currency": "USD"}\n'
           '{"source_system": "metrics_proxy"}\n')  # invalid: no source_id
    _write(platform / "price_versions" / "v1.json", {"price_version": "v1", "status": "active"})
    _write(platform / "audit" / "audit_events.jsonl",
           '{"event_id": "audit-1", "actor": "local-admin", "role": "admin", "action": "agent_create", "timestamp": "2026-01-01T00:00:00Z"}\n')
    _write(platform / "run_index.json", {"schema_version": 1, "entries": {"run-1": {"agent_id": "agent-1"}}})
    # a run evidence directory that must never be touched by migration
    run_dir = root / "runs" / "run-1"
    _write(run_dir / "run.json", {"run_id": "run-1", "status": "completed"})
    return platform


class TestMigratePlatform:
    def test_dry_run_writes_nothing(self, tmp_path):
        platform = _seed_platform(tmp_path)
        db = _db(tmp_path)
        report = platform_migrate.migrate_platform(platform, db, dry_run=True)
        assert report["dry_run"] is True
        agents = report["entities"]["agents"]
        assert agents["total"] == 2 and agents["imported"] == 2
        assert report["entities"]["cost_records"]["invalid"] == 1  # missing source_id
        # nothing written
        assert db.query_one("SELECT agent_id FROM agents") is None
        assert db.query_one("SELECT migration_id FROM migration_runs") is None

    def test_real_migration_and_idempotency(self, tmp_path):
        platform = _seed_platform(tmp_path)
        db = _db(tmp_path)
        first = platform_migrate.migrate_platform(platform, db, dry_run=False)
        assert first["entities"]["agents"]["imported"] == 2
        assert first["entities"]["external_traces"]["imported"] == 2
        # one bad cost record did not block the migration
        assert first["entities"]["cost_records"]["imported"] == 1
        assert first["entities"]["cost_records"]["invalid"] == 1
        # migrated rows carry source_ref + migration_id
        row = db.query_one("SELECT source_ref, migration_id FROM agents WHERE agent_id=%s", ("agent-1",))
        assert row["source_ref"] == "file:agents.json#agent-1"
        assert row["migration_id"] == first["migration_id"]
        # re-run: everything is a duplicate, no double inserts
        second = platform_migrate.migrate_platform(platform, db, dry_run=False)
        assert second["entities"]["agents"]["duplicate"] == 2
        assert second["entities"]["agents"]["imported"] == 0
        count = db.query_one("SELECT COUNT(*) AS c FROM agents")["c"]
        assert count == 2
        # source files untouched
        assert (platform / "agents.json").is_file()
        assert json.loads((tmp_path / "runs" / "run-1" / "run.json").read_text())["status"] == "completed"

    def test_verify_counts_match(self, tmp_path):
        platform = _seed_platform(tmp_path)
        db = _db(tmp_path)
        # before migration: inconsistent (file has data, db does not)
        pre = platform_migrate.verify_platform_migration(platform, db)
        assert pre["consistent"] is False
        assert pre["entities"]["agents"] == {"file": 2, "database": 0, "consistent": False}
        platform_migrate.migrate_platform(platform, db, dry_run=False)
        post = platform_migrate.verify_platform_migration(platform, db)
        assert post["consistent"] is True
        assert post["entities"]["agents"]["database"] == 2
        assert post["entities"]["import_jobs"]["database"] == 1
        assert post["entities"]["audit_events"]["database"] == 1
        assert post["entities"]["price_versions"]["database"] == 1

    def test_rollback_deletes_only_marked_rows(self, tmp_path):
        platform = _seed_platform(tmp_path)
        db = _db(tmp_path)
        report = platform_migrate.migrate_platform(platform, db, dry_run=False)
        # an unrelated database-native row must survive rollback
        db.execute(
            "INSERT INTO agents (tenant_id, agent_id, name, created_at) VALUES (%s,%s,%s,%s)",
            ("default", "agent-db-native", "Native", "2026-01-03T00:00:00Z"),
        )
        dry = platform_migrate.rollback_platform_migration(db, report["migration_id"], dry_run=True)
        assert dry["tables"]["agents"]["matched"] == 2
        assert dry["tables"]["agents"]["deleted"] == 0
        assert db.query_one("SELECT COUNT(*) AS c FROM agents")["c"] == 3  # nothing deleted

        real = platform_migrate.rollback_platform_migration(db, report["migration_id"], dry_run=False)
        assert real["tables"]["agents"]["deleted"] == 2
        assert db.query_one("SELECT COUNT(*) AS c FROM agents")["c"] == 1
        survivor = db.query_one("SELECT agent_id FROM agents")
        assert survivor["agent_id"] == "agent-db-native"
        # files and run evidence untouched
        assert (platform / "agents.json").is_file()
        assert (tmp_path / "runs" / "run-1" / "run.json").is_file()
        # migration run marked rolled back
        row = db.query_one("SELECT status FROM migration_runs WHERE migration_id=%s", (report["migration_id"],))
        assert row["status"] == "rolled_back"


class TestBackupRestore:
    def test_backup_verify_restore_roundtrip(self, tmp_path):
        platform = _seed_platform(tmp_path)
        db = _db(tmp_path)
        platform_migrate.migrate_platform(platform, db, dry_run=False)

        backup_root = tmp_path / "backups"
        manifest = platform_backup.backup_platform(platform, backup_root, db=db)
        backup_dir = backup_root / manifest["backup_id"]
        assert manifest["contains_secrets"] is False
        assert manifest["components"]["platform_files"]["file_count"] > 0
        assert manifest["components"]["database"]["table_count"] > 0

        verified = platform_backup.verify_platform_backup(backup_dir)
        assert verified["valid"] is True, verified["errors"]

        # restore to a fresh directory: dry-run first
        target = tmp_path / "restored"
        db2 = _db(tmp_path, name="restored.db")
        dry = platform_backup.restore_platform(backup_dir, target, db=db2, dry_run=True)
        assert dry["dry_run"] is True
        assert not target.exists() or not list(target.rglob("*.json"))
        assert db2.query_one("SELECT COUNT(*) AS c FROM agents")["c"] == 0

        real = platform_backup.restore_platform(backup_dir, target, db=db2, dry_run=False)
        assert real["files"]["restored"] > 0
        assert (target / "agents.json").is_file()
        assert db2.query_one("SELECT COUNT(*) AS c FROM agents")["c"] == 2
        # idempotent: second restore skips existing files/rows
        again = platform_backup.restore_platform(backup_dir, target, db=db2, dry_run=False, force=True)
        assert again["files"]["skipped_existing"] > 0
        assert db2.query_one("SELECT COUNT(*) AS c FROM agents")["c"] == 2

    def test_backup_excludes_secrets(self, tmp_path):
        platform = _seed_platform(tmp_path)
        _write(platform / ".env", "OPENAI_API_KEY=sk-secret")
        _write(platform / "keys.pem", "PRIVATE KEY")
        manifest = platform_backup.backup_platform(platform, tmp_path / "backups")
        excluded = manifest["components"]["platform_files"]["excluded"]
        assert ".env" in excluded and "keys.pem" in excluded
        backup_dir = tmp_path / "backups" / manifest["backup_id"]
        assert not (backup_dir / "platform" / ".env").exists()
        blob = json.dumps(manifest)
        assert "sk-secret" not in blob and "PRIVATE KEY" not in blob

    def test_restore_refuses_nonempty_target_without_force(self, tmp_path):
        platform = _seed_platform(tmp_path)
        manifest = platform_backup.backup_platform(platform, tmp_path / "backups")
        target = tmp_path / "occupied"
        _write(target / "agents.json", {"items": []})
        with pytest.raises(ValueError):
            platform_backup.restore_platform(tmp_path / "backups" / manifest["backup_id"], target, dry_run=False)

    def test_verify_detects_corruption(self, tmp_path):
        platform = _seed_platform(tmp_path)
        manifest = platform_backup.backup_platform(platform, tmp_path / "backups")
        backup_dir = tmp_path / "backups" / manifest["backup_id"]
        (backup_dir / "platform" / "agents.json").write_text("tampered", encoding="utf-8")
        result = platform_backup.verify_platform_backup(backup_dir)
        assert result["valid"] is False
        assert any("checksum mismatch" in error for error in result["errors"])
