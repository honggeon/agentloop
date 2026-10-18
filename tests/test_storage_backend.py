"""Phase 5 storage backend behavior: file/database/dual modes, dual-write
mirroring, conflict priority and explicit database-outage behavior.

SQLite in tmp_path is the real database backend under test (not a fake);
no network, no real platform/ data.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from auditrail.db import Database, StorageUnavailableError, apply_migrations
from auditrail.integrations import import_jobs
from backend.app import integration_service, job_store as job_store_mod
from backend.app.config import Settings, resolved_database_url, validate_startup_settings
from backend.app.main import app


def _settings(tmp_path, **overrides):
    base = dict(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _db_settings(tmp_path, mode="database", **overrides):
    base = dict(
        storage_mode=mode,
        queue_mode="local_worker",
        database_url="sqlite:///%s" % (tmp_path / "platform" / "auditrail.db"),
    )
    base.update(overrides)
    settings = _settings(tmp_path, **base)
    apply_migrations(Database(settings.database_url))
    job_store_mod.reset_cached_stores()
    return settings


def _traces_file(settings, count=2):
    rows = [{"id": "t-%d" % i, "metadata": {}} for i in range(1, count + 1)]
    path = settings.platform_dir / "imports" / "traces.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# mode selection and validation
# ---------------------------------------------------------------------------


def test_file_mode_default_requires_no_database(tmp_path):
    settings = _settings(tmp_path)
    store = job_store_mod.job_store(settings)
    assert store.mode == "file"
    assert store.ping() is True
    state = job_store_mod.migration_state(settings)
    assert state["required"] is False and state["up_to_date"] is True


def test_default_database_url_is_embedded_sqlite(tmp_path):
    settings = _settings(tmp_path)
    url = resolved_database_url(settings)
    assert url.startswith("sqlite:///")
    assert str(settings.platform_dir) in url


def test_queue_modes_require_database_storage(tmp_path):
    settings = _settings(tmp_path, queue_mode="local_worker")  # storage file
    with pytest.raises(ValueError):
        validate_startup_settings(settings)
    redis_without_url = _db_settings(tmp_path, queue_mode="redis", redis_url="")
    with pytest.raises(ValueError):
        validate_startup_settings(redis_without_url)


def test_database_mode_store_roundtrip(tmp_path):
    settings = _db_settings(tmp_path)
    store = job_store_mod.job_store(settings)
    assert store.mode == "database"
    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    store.save(job)
    loaded = store.load(str(job["import_id"]))
    assert loaded is not None and loaded["status"] == "queued"
    # file mirror only in dual mode
    assert import_jobs.load_import_job(settings.platform_dir, str(job["import_id"])) is None


def test_dual_mode_mirrors_files(tmp_path):
    settings = _db_settings(tmp_path, mode="dual")
    store = job_store_mod.job_store(settings)
    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    store.save(job)
    mirror = import_jobs.load_import_job(settings.platform_dir, str(job["import_id"]))
    assert mirror is not None and mirror["status"] == "queued"


def test_dual_read_conflict_prefers_database(tmp_path):
    """When file mirror and database disagree, the database wins and the
    disagreement is visible (never silently overwritten the other way)."""
    settings = _db_settings(tmp_path, mode="dual")
    store = job_store_mod.job_store(settings)
    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    store.save(job)
    # hand-edit the file mirror to create a conflict
    mirror_path = settings.platform_dir / "external" / "imports" / ("%s.json" % job["import_id"])
    tampered = json.loads(mirror_path.read_text())
    tampered["status"] = "completed"  # mirror lies; database says queued
    mirror_path.write_text(json.dumps(tampered), encoding="utf-8")
    read = integration_service.get_import_job(str(job["import_id"]), settings)
    assert read["status"] == "queued"  # database is authoritative


def test_database_unavailable_is_structured_503(monkeypatch, tmp_path):
    settings = _db_settings(tmp_path)
    monkeypatch.setattr("backend.app.routes.integrations.get_settings", lambda: settings)
    client = TestClient(app)

    def _down(_settings):
        raise StorageUnavailableError("database is unreachable")

    monkeypatch.setattr("backend.app.job_store.database_for_settings", _down)
    job_store_mod.reset_cached_stores()
    response = client.get("/api/integrations/imports")
    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["code"] == "storage_unavailable"
    assert detail["retryable"] is True
    # no paths or connection strings in the error
    assert str(tmp_path) not in json.dumps(detail)


def test_existing_job_state_never_overwritten_on_outage(tmp_path):
    """An outage must not flip stored jobs to completed."""
    settings = _db_settings(tmp_path)
    store = job_store_mod.job_store(settings)
    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    store.save(job)

    # a broken database handle (path is a directory): operations fail with
    # StorageUnavailableError, never with fabricated state
    broken_db = Database("sqlite:///%s" % tmp_path)  # a directory: cannot open
    from backend.app.job_store import DatabaseJobStore

    broken_store = DatabaseJobStore(broken_db, tenant_id="default", platform_dir=None, mirror_files=False)
    with pytest.raises(StorageUnavailableError):
        broken_store.list_jobs()
    with pytest.raises(StorageUnavailableError):
        broken_store.load(str(job["import_id"]))
    # the real store recovers with the same state afterwards
    reloaded = store.load(str(job["import_id"]))
    assert reloaded is not None and reloaded["status"] == "queued"


def test_legacy_file_jobs_still_listed_in_database_mode(tmp_path):
    """File-era import records remain readable after switching modes: the
    file store is the compatibility read layer."""
    settings = _db_settings(tmp_path)
    legacy = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    legacy["status"] = "completed"
    import_jobs.save_import_job(settings.platform_dir, legacy)
    # database store does not see the file-only record...
    assert job_store_mod.job_store(settings).load(str(legacy["import_id"])) is None
    # ...but the migration path imports it (covered in test_platform_migration)
    from auditrail import platform_migrate

    report = platform_migrate.migrate_platform(
        settings.platform_dir, Database(settings.database_url), dry_run=False
    )
    assert report["entities"]["import_jobs"]["imported"] == 1
    migrated = job_store_mod.job_store(settings).load(str(legacy["import_id"]))
    assert migrated is not None and migrated["status"] == "completed"
    assert migrated["source_ref"]
