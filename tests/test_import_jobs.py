"""Phase 4 ImportJob: state machine, checkpoints, cancel/retry/resume, stale.

All tests are deterministic: no network (HTTP imports use injected fake
fetchers), no real tokens, tmp_path fixtures only.
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from auditrail.integrations import external_models as ext
from auditrail.integrations import import_jobs
from auditrail.integrations import langfuse
from backend.app import import_worker, integration_service
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path, **overrides):
    base = dict(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
        import_checkpoint_records=2,
        import_lease_seconds=60,
        import_max_retries=3,
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _client(monkeypatch, settings):
    monkeypatch.setattr("backend.app.routes.integrations.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.routes.admin.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.import_worker.get_settings", lambda: settings)
    return TestClient(app)


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    return path


def _make_run(settings, run_id):
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "completed",
                "status_reason": None,
                "started_at": "2026-01-05T10:00:00Z",
                "finished_at": "2026-01-05T10:00:02Z",
                "agent_ref": {"id": "agent-1"},
                "test_case_ref": None,
                "suite_ref": None,
                "artifact_manifest": [],
            }
        ),
        encoding="utf-8",
    )


def _traces_file(settings, count=5, prefix="t"):
    rows = [{"id": "%s-%d" % (prefix, index), "metadata": {"run_id": "run-1"}} for index in range(1, count + 1)]
    return _write_jsonl(settings.platform_dir / "imports" / "traces.jsonl", rows)


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ---------------------------------------------------------------------------
# state machine and persistence (core)
# ---------------------------------------------------------------------------


def test_job_state_machine_transitions():
    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    assert job["status"] == "queued"
    assert job["phase"] == "validating"
    job = import_jobs.transition(job, "validating")
    job = import_jobs.transition(job, "running", phase="reading")
    assert job["started_at"]
    job = import_jobs.transition(job, "completed", phase="finalizing")
    assert job["finished_at"]
    with pytest.raises(import_jobs.ImportJobError):
        import_jobs.transition(job, "running")


def test_terminal_states_cannot_be_cancelled():
    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    job = import_jobs.transition(job, "validating")
    job = import_jobs.transition(job, "running")
    job = import_jobs.transition(job, "completed")
    with pytest.raises(import_jobs.ImportJobError):
        import_jobs.request_cancel(job)


def test_options_redaction_drops_secret_keys():
    job = import_jobs.new_import_job(
        source_system="langfuse",
        source_type="http",
        kind="traces",
        source_fingerprint="fp",
        options={"config": {"token_env": "LF_TOKEN", "authorization": "Bearer x"}, "limit": 10},
    )
    assert "authorization" not in json.dumps(job["options"])
    assert job["options"]["limit"] == 10


# ---------------------------------------------------------------------------
# sync inline execution keeps the Phase 3 contract
# ---------------------------------------------------------------------------


def test_inline_import_completes_with_report(tmp_path):
    settings = _settings(tmp_path)
    _make_run(settings, "run-1")
    _traces_file(settings, count=3)
    job = integration_service.create_import_job(
        "langfuse", {"source_file": "imports/traces.jsonl"}, settings, actor="local-admin"
    )
    assert job["status"] == "queued"
    finished = import_worker.execute_import_inline(str(job["import_id"]), settings)
    assert finished["status"] == "completed"
    assert finished["report"]["status"] == "imported"
    assert finished["report"]["imported_records"] == 3
    assert finished["imported_records"] == 3
    stored = ext.load_external_traces(settings.platform_dir)
    assert len(stored) == 3
    assert all(record["run_id"] == "run-1" for record in stored)


def test_checkpoint_written_per_batch(tmp_path):
    settings = _settings(tmp_path)  # checkpoint every 2 records
    _make_run(settings, "run-1")
    _traces_file(settings, count=5)
    job = integration_service.create_import_job(
        "langfuse", {"source_file": "imports/traces.jsonl"}, settings
    )
    finished = import_worker.execute_import_inline(str(job["import_id"]), settings)
    checkpoint = finished["checkpoint"]
    assert checkpoint is not None
    assert checkpoint["index"] == 5  # final flush covers the whole file
    assert checkpoint["byte_offset"] > 0
    assert finished["checkpoint_at"]


def test_resume_from_checkpoint_never_duplicates(tmp_path):
    """A failed job resumed from its checkpoint must not duplicate records."""
    settings = _settings(tmp_path)
    _make_run(settings, "run-1")
    _traces_file(settings, count=5)
    job = integration_service.create_import_job(
        "langfuse", {"source_file": "imports/traces.jsonl"}, settings
    )
    import_id = str(job["import_id"])
    import_worker.execute_import_inline(import_id, settings)
    before = ext.load_external_traces(settings.platform_dir)
    assert len(before) == 5

    # Simulate a crashed retry: mark failed, keep the checkpoint, resume.
    stored = import_jobs.load_import_job(settings.platform_dir, import_id)
    stored = dict(stored)
    stored["status"] = "failed"
    import_jobs.save_import_job(settings.platform_dir, stored)
    import_worker.prepare_retry(import_id, settings)
    finished = import_worker.execute_import_inline(import_id, settings)
    assert finished["status"] == "completed"
    after = ext.load_external_traces(settings.platform_dir)
    assert len(after) == 5  # no duplicated external records
    assert finished["retry_count"] == 1


def test_interrupted_job_never_claims_completed(tmp_path):
    """A job left ``running`` by a dead process is stale after the lease, never completed."""
    settings = _settings(tmp_path)
    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    job = import_jobs.acquire_lease(job, "worker-dead", lease_seconds=1)
    job = import_jobs.transition(job, "validating")
    job = import_jobs.transition(job, "running", phase="reading")
    import_jobs.save_import_job(settings.platform_dir, job)
    loaded = import_jobs.load_import_job(settings.platform_dir, str(job["import_id"]))
    assert loaded["status"] == "running"  # lease valid: still owned, not completed

    # Expire the lease and refresh: stale, resumable, never completed.
    job = dict(job)
    job["lease_expires_at"] = "2020-01-01T00:00:00Z"
    import_jobs.save_import_job(settings.platform_dir, job)
    stale = import_jobs.refresh_stale_jobs(settings.platform_dir)
    assert stale == [str(job["import_id"])]
    reloaded = import_jobs.load_import_job(settings.platform_dir, str(job["import_id"]))
    assert reloaded["status"] == "stale"
    assert import_jobs.job_is_stale(reloaded)


def test_old_worker_cannot_overwrite_new_worker(tmp_path):
    settings = _settings(tmp_path)
    job = import_jobs.new_import_job(
        source_system="langfuse", source_type="file", kind="traces", source_fingerprint="fp"
    )
    job = import_jobs.acquire_lease(job, "worker-new", lease_seconds=60)
    import_jobs.save_import_job(settings.platform_dir, job)
    stale_copy = dict(job)
    stale_copy["status"] = "completed"  # an old worker trying to clobber state
    with pytest.raises(import_jobs.StaleWorkerError):
        import_jobs.save_import_job(settings.platform_dir, stale_copy, worker_id="worker-old")
    on_disk = import_jobs.load_import_job(settings.platform_dir, str(job["import_id"]))
    assert on_disk["status"] == "queued"


# ---------------------------------------------------------------------------
# HTTP pagination / retry classification (fake fetchers only)
# ---------------------------------------------------------------------------


def _http_config():
    return {
        "enabled": True,
        "base_url": "https://langfuse.example",
        "token_env": "AUDITRAIL_TEST_LF_TOKEN",
        "allowed_hosts": ["langfuse.example"],
    }


def _http_job(settings, kind="traces"):
    return integration_service.create_import_job(
        "langfuse", {"kind": kind, "config": _http_config()}, settings
    )


def test_http_cursor_pagination_resume(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDITRAIL_TEST_LF_TOKEN", "lf-token")
    settings = _settings(tmp_path)
    _make_run(settings, "run-1")
    calls = []

    def fake_fetch(url, token):
        calls.append(url)
        if "cursor=" not in url:
            return {
                "data": [{"id": "t-1", "metadata": {"run_id": "run-1"}}],
                "meta": {"cursor": "cursor-page-2"},
            }
        assert "cursor=cursor-page-2" in url
        return {"data": [{"id": "t-2", "metadata": {"run_id": "run-1"}}], "meta": {}}

    job = _http_job(settings)
    finished = import_worker.execute_import_inline(str(job["import_id"]), settings, fetcher=fake_fetch)
    assert finished["status"] == "completed"
    assert len(calls) == 2
    stored = ext.load_external_traces(settings.platform_dir)
    assert {record["source_id"] for record in stored} == {"t-1", "t-2"}
    # the checkpoint tracked the cursor between pages
    assert finished["checkpoint"] is not None


def test_http_429_is_retryable_with_bounded_retries(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDITRAIL_TEST_LF_TOKEN", "lf-token")
    settings = _settings(tmp_path, import_max_retries=2)

    def rate_limited(url, token):
        raise langfuse.LangfuseSourceError("source_rate_limited", "langfuse HTTP 429", retryable=True, status=429)

    job = _http_job(settings)
    finished = import_worker.execute_import_inline(
        str(job["import_id"]), settings, fetcher=rate_limited, sleeper=lambda _s: None
    )
    assert finished["status"] == "failed"
    assert finished["error"]["code"] == "source_rate_limited"
    assert finished["error"]["retryable"] is True
    assert finished["retry_count"] == 2  # bounded, no infinite retry
    assert "lf-token" not in json.dumps(finished)


def test_http_401_is_non_retryable(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDITRAIL_TEST_LF_TOKEN", "lf-token")
    settings = _settings(tmp_path)

    def unauthorized(url, token):
        raise langfuse.LangfuseSourceError("source_unauthorized", "langfuse HTTP 401", retryable=False, status=401)

    job = _http_job(settings)
    finished = import_worker.execute_import_inline(str(job["import_id"]), settings, fetcher=unauthorized)
    assert finished["status"] == "failed"
    assert finished["error"]["code"] == "source_unauthorized"
    assert finished["error"]["retryable"] is False
    assert finished["retry_count"] == 0  # never retried


def test_cancel_between_http_pages_keeps_partial_evidence(monkeypatch, tmp_path):
    monkeypatch.setenv("AUDITRAIL_TEST_LF_TOKEN", "lf-token")
    settings = _settings(tmp_path)
    _make_run(settings, "run-1")
    job = _http_job(settings)
    import_id = str(job["import_id"])

    calls = []

    def fake_fetch(url, token):
        calls.append(url)
        if len(calls) >= 2:
            # page 1 was flushed; cancel lands before page 2 is processed
            import_worker.request_cancel(import_id, settings)
        return {"data": [{"id": "t-%d" % len(calls), "metadata": {"run_id": "run-1"}}], "meta": {"totalPages": 3}}

    finished = import_worker.execute_import_inline(import_id, settings, fetcher=fake_fetch)
    assert finished["status"] == "cancelled"
    assert finished["cancel_requested"] is True
    # the first page was persisted; unprocessed pages are not faked as invalid
    stored = ext.load_external_traces(settings.platform_dir)
    assert len(stored) == 1
    assert finished["invalid_records"] == 0


def test_retry_only_for_resumable_states(tmp_path):
    settings = _settings(tmp_path)
    _traces_file(settings, count=1)
    _make_run(settings, "run-1")
    job = integration_service.create_import_job(
        "langfuse", {"source_file": "imports/traces.jsonl"}, settings
    )
    import_id = str(job["import_id"])
    finished = import_worker.execute_import_inline(import_id, settings)
    assert finished["status"] == "completed"
    with pytest.raises(import_jobs.ImportJobError) as excinfo:
        import_worker.prepare_retry(import_id, settings)
    assert excinfo.value.code == "import_invalid_state"


# ---------------------------------------------------------------------------
# API level: 202 async, idempotency, cancel/retry endpoints
# ---------------------------------------------------------------------------


def test_api_async_import_returns_202_and_completes(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _make_run(settings, "run-1")
    _traces_file(settings, count=2)
    response = client.post(
        "/api/integrations/langfuse/import",
        json={"source_file": "imports/traces.jsonl", "async": True},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["mode"] == "async"
    assert body["status_url"].endswith("/status")
    import_id = body["import_id"]
    assert _wait(lambda: (import_jobs.load_import_job(settings.platform_dir, import_id) or {}).get("status") == "completed")
    status = client.get("/api/integrations/imports/%s/status" % import_id)
    assert status.status_code == 200
    assert status.json()["imported_records"] == 2
    assert status.json()["stale"] is False


def test_api_idempotency_same_key_same_fingerprint(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _traces_file(settings, count=1)
    payload = {"source_file": "imports/traces.jsonl"}
    first = client.post("/api/integrations/langfuse/import", json=payload, headers={"Idempotency-Key": "k-1"})
    assert first.status_code == 200
    second = client.post("/api/integrations/langfuse/import", json=payload, headers={"Idempotency-Key": "k-1"})
    assert second.status_code == 200
    assert second.json()["import_id"] == first.json()["import_id"]
    assert second.json()["idempotent_replay"] is True
    assert len(ext.load_external_traces(settings.platform_dir)) == 1


def test_api_idempotency_conflict_409(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _traces_file(settings, count=1)
    _write_jsonl(settings.platform_dir / "imports" / "other.jsonl", [{"id": "t-x"}])
    client.post(
        "/api/integrations/langfuse/import",
        json={"source_file": "imports/traces.jsonl"},
        headers={"Idempotency-Key": "k-2"},
    )
    conflict = client.post(
        "/api/integrations/langfuse/import",
        json={"source_file": "imports/other.jsonl"},
        headers={"Idempotency-Key": "k-2"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_api_cancel_queued_job_is_terminal_and_idempotent(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _traces_file(settings, count=1)
    # create a job without executing it (direct store write, status queued)
    job = integration_service.create_import_job(
        "langfuse", {"source_file": "imports/traces.jsonl"}, settings
    )
    import_id = str(job["import_id"])
    first = client.post("/api/integrations/imports/%s/cancel" % import_id)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "cancelled"
    second = client.post("/api/integrations/imports/%s/cancel" % import_id)
    assert second.status_code == 200
    assert second.json()["status"] == "cancelled"
    # cancelled is terminal: resume is rejected
    retry = client.post("/api/integrations/imports/%s/retry" % import_id)
    assert retry.status_code == 409
    assert retry.json()["detail"]["code"] == "import_invalid_state"


def test_api_import_not_found_and_structured_errors(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    for path in (
        "/api/integrations/imports/import-ghost",
        "/api/integrations/imports/import-ghost/status",
    ):
        response = client.get(path)
        assert response.status_code == 404, path
        detail = response.json()["detail"]
        assert detail["code"] == "import_not_found"
        assert detail["request_id"]
        assert detail["retryable"] is False
    response = client.post("/api/integrations/imports/import-ghost/cancel")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "import_not_found"


def test_api_import_list_pagination(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _traces_file(settings, count=1)
    _write_jsonl(settings.platform_dir / "imports" / "b.jsonl", [{"id": "t-b"}])
    client.post("/api/integrations/langfuse/import", json={"source_file": "imports/traces.jsonl"})
    client.post("/api/integrations/langfuse/import", json={"source_file": "imports/b.jsonl"})
    page1 = client.get("/api/integrations/imports?limit=1&offset=0")
    assert page1.status_code == 200
    body = page1.json()
    assert body["total"] == 2
    assert body["has_more"] is True
    assert len(body["imports"]) == 1
    page2 = client.get("/api/integrations/imports?limit=1&offset=1")
    assert page2.json()["has_more"] is False
    # limit is capped server-side
    huge = client.get("/api/integrations/imports?limit=100000")
    assert huge.json()["limit"] == 200
