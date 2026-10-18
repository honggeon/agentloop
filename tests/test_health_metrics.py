"""Phase 5 health/readiness/metrics: live vs ready vs degraded, database
outage behavior, worker visibility, and metric content safety (no secrets,
no high-cardinality labels). All fixtures are tmp_path; no network.
"""

import json

import pytest
from fastapi.testclient import TestClient

from auditrail.db import Database, apply_migrations
from backend.app import health as health_mod
from backend.app import job_store as job_store_mod
from backend.app import metrics as metrics_mod
from backend.app.config import Settings
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


def _db_settings(tmp_path, **overrides):
    base = dict(
        storage_mode="database",
        queue_mode="local_worker",
        database_url="sqlite:///%s" % (tmp_path / "platform" / "auditrail.db"),
    )
    base.update(overrides)
    settings = _settings(tmp_path, **base)
    apply_migrations(Database(settings.database_url))
    job_store_mod.reset_cached_stores()
    return settings


def _client(monkeypatch, settings):
    for module in (
        "backend.app.main",
        "backend.app.routes.admin",
        "backend.app.routes.integrations",
    ):
        monkeypatch.setattr("%s.get_settings" % module, lambda: settings)
    return TestClient(app)


# ---------------------------------------------------------------------------
# live / ready / degraded
# ---------------------------------------------------------------------------


def test_liveness_always_ok_even_when_db_down(monkeypatch, tmp_path):
    settings = _db_settings(tmp_path)
    client = _client(monkeypatch, settings)

    def _down(_settings):
        from auditrail.db import StorageUnavailableError

        raise StorageUnavailableError("database is unreachable")

    monkeypatch.setattr("backend.app.job_store.database_for_settings", _down)
    job_store_mod.reset_cached_stores()
    live = client.get("/api/health/live")
    assert live.status_code == 200
    assert live.json()["status"] == "alive"
    # readiness flips to not_ready on a critical dependency failure
    ready = client.get("/api/health/ready")
    assert ready.status_code == 503
    checks = {check["name"]: check for check in ready.json()["checks"]}
    assert checks["database"]["status"] == "fail"


def test_readiness_ready_in_file_mode(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["database"]["status"] == "skipped"  # file mode needs no DB
    assert checks["platform_storage"]["status"] == "ok"
    assert checks["runs_root"]["status"] == "ok"
    assert checks["audit_log"]["status"] == "ok"


def test_readiness_ready_in_database_mode(monkeypatch, tmp_path):
    settings = _db_settings(tmp_path)
    client = _client(monkeypatch, settings)
    body = client.get("/api/health/ready").json()
    assert body["status"] == "ready"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["database"]["status"] == "ok"


def test_readiness_fails_when_migrations_pending(monkeypatch, tmp_path):
    settings = _settings(
        tmp_path,
        storage_mode="database",
        queue_mode="local_worker",
        database_url="sqlite:///%s" % (tmp_path / "platform" / "auditrail.db"),
    )
    # deliberately NOT applying migrations
    job_store_mod.reset_cached_stores()
    client = _client(monkeypatch, settings)
    response = client.get("/api/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["database"]["status"] == "fail"
    assert "migrations_pending" in checks["database"]["detail"]


def test_readiness_degraded_with_stale_jobs(monkeypatch, tmp_path):
    settings = _db_settings(tmp_path)
    store = job_store_mod.job_store(settings)
    from auditrail.integrations import import_jobs

    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    store.save(job)
    store.database.execute(
        "UPDATE import_jobs SET status=%s WHERE import_id=%s", ("stale", job["import_id"])
    )
    client = _client(monkeypatch, settings)
    body = client.get("/api/health/ready").json()
    assert body["status"] == "degraded"
    assert "stale_import_jobs" in body["degraded_reasons"]
    assert body["operational"]["stale_import_jobs"] == 1


def test_health_endpoints_never_leak_paths_or_secrets(monkeypatch, tmp_path):
    settings = _db_settings(tmp_path)
    client = _client(monkeypatch, settings)
    for path in ("/api/health", "/api/health/live", "/api/health/ready"):
        body = client.get(path).text
        assert str(tmp_path) not in body
        assert "sqlite:///" not in body
        assert "password" not in body.lower()


def test_readiness_degraded_when_redis_down(monkeypatch, tmp_path):
    """queue_mode=redis with an unreachable broker: degraded (DB poll
    fallback), never healthy, never not_ready."""
    settings = _db_settings(tmp_path, queue_mode="redis", redis_url="redis://127.0.0.1:6390/0")
    monkeypatch.setattr("backend.app.queue_adapter.queue_ping", lambda _s=None: False)
    client = _client(monkeypatch, settings)
    response = client.get("/api/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    checks = {check["name"]: check for check in body["checks"]}
    assert checks["queue"]["status"] == "fail"
    assert "queue_degraded" in body["degraded_reasons"]


def test_admin_health_and_workers(monkeypatch, tmp_path):
    settings = _db_settings(tmp_path)
    client = _client(monkeypatch, settings)
    health = client.get("/api/admin/health")
    assert health.status_code == 200
    assert health.json()["storage_mode"] == "database"
    workers = client.get("/api/admin/workers")
    assert workers.status_code == 200
    assert workers.json()["registry_available"] is True
    migrations = client.get("/api/admin/migrations")
    assert migrations.json()["up_to_date"] is True


def test_admin_migrations_apply_is_explicit(monkeypatch, tmp_path):
    settings = _settings(
        tmp_path,
        storage_mode="database",
        queue_mode="local_worker",
        database_url="sqlite:///%s" % (tmp_path / "platform" / "auditrail.db"),
    )
    job_store_mod.reset_cached_stores()
    client = _client(monkeypatch, settings)
    pending = client.get("/api/admin/migrations").json()
    assert pending["up_to_date"] is False
    applied = client.post("/api/admin/migrations/apply")
    assert applied.status_code == 200
    assert applied.json()["up_to_date"] is True
    # audited
    events = client.get("/api/audit-events", params={"action": "migration_run"})
    assert events.json()["total"] == 1
    # idempotent second apply
    again = client.post("/api/admin/migrations/apply")
    assert again.json()["applied_now"] == []


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_metrics_endpoint_shape_and_safety(monkeypatch, tmp_path):
    metrics_mod.reset_registry()
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    client.get("/api/agents")  # generate some traffic
    response = client.get("/api/metrics")
    assert response.status_code == 200
    text = response.text
    assert "auditrail_http_requests_total" in text
    assert "auditrail_http_request_duration_seconds" in text
    # no high-cardinality identifier labels and no secret-shaped values
    lowered = text.lower()
    for forbidden in ("run_id=", "trace_id=", "user_id=", "import_id=", "job_id=", "bearer ey", "sk-"):
        assert forbidden not in lowered
    assert str(tmp_path) not in text


def test_metric_label_guard_rejects_high_cardinality():
    with pytest.raises(ValueError):
        metrics_mod.counter_inc("auditrail_test_counter", run_id="run-1")
    with pytest.raises(ValueError):
        metrics_mod.counter_inc("auditrail_test_counter", user_id="u-1")
    # bounded labels fine
    metrics_mod.counter_inc("auditrail_test_counter_ok", mode="local", status="2xx")


def test_import_job_metrics_recorded(tmp_path, monkeypatch):
    """Import lifecycle increments counters (labels: source_system/status)."""
    metrics_mod.reset_registry()
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    rows = [{"id": "t-%d" % i, "metadata": {}} for i in range(1, 3)]
    path = settings.platform_dir / "imports" / "traces.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    response = client.post("/api/integrations/langfuse/import", json={"source_file": "imports/traces.jsonl"})
    assert response.status_code == 200
    text = client.get("/api/metrics").text
    assert 'auditrail_import_jobs_total{kind="traces",source_system="langfuse",status="completed"} 1' in text
    assert 'auditrail_external_records_total{kind="traces",source_system="langfuse"} 2' in text
    assert "auditrail_import_job_duration_seconds" in text
    # never labelled with per-job identifiers
    assert "import-" not in text
