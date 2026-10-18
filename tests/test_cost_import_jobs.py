"""Phase 5 cost imports through the unified ImportJob state machine.

Same lifecycle as Langfuse/promptfoo imports: checkpointed batches, pause/
resume/cancel/retry, stale recovery — while preserving cost semantics
(observed priority, source_id idempotency, price-versioned estimation,
tokens never treated as dollars).
"""

import json

import pytest
from fastapi.testclient import TestClient

from auditrail import costs as cost_mod
from auditrail.integrations import import_jobs
from backend.app import import_worker, job_store as job_store_mod
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path, **overrides):
    base = dict(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
        import_checkpoint_records=2,
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _db_settings(tmp_path, **overrides):
    from auditrail.db import Database, apply_migrations

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
    monkeypatch.setattr("backend.app.routes.costs.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.import_worker.get_settings", lambda: settings)
    return TestClient(app)


def _write_costs(settings, rows, name="costs.jsonl"):
    path = settings.platform_dir / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _rows(count, prefix="c", **extra):
    base = {"total_cost": 0.001, "currency": "USD"}
    base.update(extra)
    return [dict(base, callId="%s-%d" % (prefix, i)) for i in range(1, count + 1)]


# ---------------------------------------------------------------------------
# unified state machine
# ---------------------------------------------------------------------------


def test_cost_import_runs_through_import_job(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _write_costs(settings, _rows(3))
    response = client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "imports/costs.jsonl"})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "completed"
    assert report["imported_records"] == 3
    # unified: the job is visible through the ImportJob machinery too
    job = import_jobs.load_import_job(settings.platform_dir, report["import_id"])
    assert job is not None
    assert job["kind"] == "cost_records"
    assert job["status"] == "completed"
    assert job["checkpoint"] is not None  # checkpoint support


def test_cost_import_checkpoint_and_no_duplicate_on_resume(tmp_path):
    settings = _db_settings(tmp_path)
    _write_costs(settings, _rows(5))
    from backend.app import cost_service

    job = cost_service.create_cost_import_job(
        {"source_system": "metrics_proxy", "source_file": "imports/costs.jsonl"}, settings
    )
    import_id = str(job["import_id"])
    finished = import_worker.execute_import_inline(import_id, settings)
    assert finished["status"] == "completed"
    assert finished["checkpoint"]["index"] == 5
    before = cost_mod.load_cost_records(settings.platform_dir)
    assert len(before) == 5

    # simulate crash -> failed with checkpoint -> retry resumes, no dupes
    store = job_store_mod.job_store(settings)
    store.mutate(import_id, lambda record: dict(record, status="failed"))
    import_worker.prepare_retry(import_id, settings)
    again = import_worker.execute_import_inline(import_id, settings)
    assert again["status"] == "completed"
    assert len(cost_mod.load_cost_records(settings.platform_dir)) == 5
    assert again["duplicate_records"] == 5


def test_cost_import_pause_resume_cancel(tmp_path):
    settings = _db_settings(tmp_path)
    _write_costs(settings, _rows(4))
    from backend.app import cost_service

    job = cost_service.create_cost_import_job(
        {"source_system": "metrics_proxy", "source_file": "imports/costs.jsonl"}, settings
    )
    import_id = str(job["import_id"])

    # queued job pauses immediately; resume re-queues; cancel is terminal
    paused = import_worker.request_pause(import_id, settings)
    assert paused["status"] == "paused"
    resumed = import_worker.prepare_resume(import_id, settings)
    assert resumed["status"] == "queued"
    cancelled = import_worker.request_cancel(import_id, settings)
    assert cancelled["status"] == "cancelled"
    # completed/cancelled are not unconditionally retryable
    with pytest.raises(import_jobs.ImportJobError) as excinfo:
        import_worker.prepare_retry(import_id, settings)
    assert excinfo.value.code == "import_invalid_state"
    # cancel never wrote partial data
    assert cost_mod.load_cost_records(settings.platform_dir) == []


def test_cost_import_api_lifecycle_endpoints(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _write_costs(settings, _rows(2))
    created = client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "imports/costs.jsonl"})
    import_id = created.json()["import_id"]
    fetched = client.get("/api/costs/imports/%s" % import_id)
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "completed"
    # cancel on a terminal job is a structured 409, not a crash
    cancel = client.post("/api/costs/imports/%s/cancel" % import_id)
    assert cancel.status_code == 409
    # retry on completed is rejected
    retry = client.post("/api/costs/imports/%s/retry" % import_id)
    assert retry.status_code == 409
    # unknown import 404s
    assert client.get("/api/costs/imports/import-ghost").status_code == 404


# ---------------------------------------------------------------------------
# cost semantics preserved
# ---------------------------------------------------------------------------


def test_import_never_overwrites_observed(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    run_dir = settings.runs_dir / "run-1"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "llm_calls.json").write_text(
        json.dumps({"llm_calls": [{"callId": "call-1", "cost": 9.99, "currency": "USD"}]}), encoding="utf-8"
    )
    _write_costs(settings, [{"callId": "call-1", "run_id": "run-1", "total_cost": 0.01, "currency": "USD"}])
    client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "imports/costs.jsonl"})
    from backend.app import cost_service

    payload = cost_service.run_costs("run-1", settings)
    summary = payload["summary"]
    # observed wins the total for the shared source_id; the import is still
    # recorded alongside (conflict tracked, both records retained).
    assert summary["total"] == 9.99
    assert summary["record_count"] == 2
    assert summary["conflict_count"] == 1
    sources = {record["source"] for record in payload["records"]}
    assert sources == {"observed", "imported"}


def test_estimation_keeps_price_version(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    (settings.platform_dir / "price_table.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "price_version": "v2026-01",
                "models": {"gpt-test": {"input_cost_per_1m_tokens": 1.0, "output_cost_per_1m_tokens": 2.0, "currency": "USD"}},
            }
        ),
        encoding="utf-8",
    )
    _write_costs(settings, [{"callId": "tok-1", "model": "gpt-test", "tokenUsage": {"inputTokens": 1000, "outputTokens": 500}}])
    client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "imports/costs.jsonl"})
    stored = cost_mod.load_cost_records(settings.platform_dir)
    assert len(stored) == 1
    record = stored[0]
    assert record["source"] == "estimated"
    assert record["price_version"] == "v2026-01"
    assert record["total_cost"] is not None


def test_missing_cost_stays_null_not_zero(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _write_costs(settings, [{"callId": "tok-2", "tokenUsage": {"totalTokens": 42}}])
    client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "imports/costs.jsonl"})
    stored = cost_mod.load_cost_records(settings.platform_dir)
    assert stored[0]["total_cost"] is None  # tokens are never dollars


def test_legacy_cost_report_still_readable(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    # a Phase 2-era report file remains readable through the same endpoint
    legacy = {
        "schema_version": 1,
        "import_id": "import-legacy1",
        "status": "completed",
        "source_system": "metrics_proxy",
        "source_file": "old.jsonl",
        "dry_run": False,
        "total_records": 1,
        "imported_records": 1,
        "duplicate_records": 0,
        "invalid_records": 0,
        "errors": [],
        "warnings": [],
        "imported_at": "2026-01-01T00:00:00Z",
    }
    cost_mod.save_import_report(settings.platform_dir, legacy)
    response = client.get("/api/costs/imports/import-legacy1")
    assert response.status_code == 200
    assert response.json()["import_id"] == "import-legacy1"


def test_cost_import_async_mode_returns_202(tmp_path, monkeypatch):
    import time

    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _write_costs(settings, _rows(2))
    response = client.post(
        "/api/costs/import",
        json={"source_system": "metrics_proxy", "source_file": "imports/costs.jsonl", "async": True},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["mode"] == "async"
    assert body["queue_degraded"] is False
    import_id = body["import_id"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = import_jobs.load_import_job(settings.platform_dir, import_id)
        if job and job.get("status") == "completed":
            break
        time.sleep(0.02)
    assert job["status"] == "completed"
    assert job["imported_records"] == 2
    assert len(cost_mod.load_cost_records(settings.platform_dir)) == 2
