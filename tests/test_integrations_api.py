"""Phase 3 integration API: imports, idempotency, path safety, evidence reads."""

import json

from fastapi.testclient import TestClient

from auditrail.integrations import external_models as ext
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path):
    return Settings(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
    )


def _client(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr("backend.app.routes.integrations.get_settings", lambda: settings)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return TestClient(app), settings


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_run(settings, run_id, *, session_id=None, agent_id="agent-1"):
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        run_dir / "run.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": "completed",
            "status_reason": None,
            "started_at": "2026-01-05T10:00:00Z",
            "finished_at": "2026-01-05T10:00:02Z",
            "agent_ref": {"id": agent_id},
            "test_case_ref": None,
            "suite_ref": None,
            "artifact_manifest": [],
        },
    )
    if session_id:
        _write_json(run_dir / "source.json", {"session_id": session_id})
    return run_dir


def _traces_fixture(settings, rows=None):
    rows = rows if rows is not None else [{"id": "t-1", "metadata": {"run_id": "run-1", "agent_id": "agent-1"}}]
    path = settings.platform_dir / "imports" / "traces.jsonl"
    _write_jsonl(path, rows)
    return path


# ---------------------------------------------------------------------------
# import endpoint
# ---------------------------------------------------------------------------


def test_langfuse_file_import_success(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _make_run(settings, "run-1")
    _traces_fixture(settings)
    response = client.post("/api/integrations/langfuse/import", json={"source_file": "imports/traces.jsonl"})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["source_system"] == "langfuse"
    assert report["status"] == "imported"
    assert report["imported_records"] == 1
    assert report["import_id"].startswith("import-")
    stored = ext.load_external_traces(settings.platform_dir)
    assert len(stored) == 1
    assert stored[0]["run_id"] == "run-1"
    assert stored[0]["link"]["confidence"] == "exact"


def test_langfuse_import_dry_run_does_not_persist(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _traces_fixture(settings)
    response = client.post(
        "/api/integrations/langfuse/import",
        json={"source_file": "imports/traces.jsonl", "dry_run": True},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "dry_run"
    assert not (settings.platform_dir / "external" / "traces.jsonl").exists()


def test_langfuse_import_scores_via_api(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _make_run(settings, "run-1")
    _traces_fixture(settings)
    client.post("/api/integrations/langfuse/import", json={"source_file": "imports/traces.jsonl"})
    _write_jsonl(
        settings.platform_dir / "imports" / "scores.jsonl",
        [{"id": "s-1", "traceId": "t-1", "name": "quality", "value": 0.7}],
    )
    response = client.post(
        "/api/integrations/langfuse/import",
        json={"source_file": "imports/scores.jsonl", "kind": "scores"},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["kind"] == "scores"
    assert report["imported_records"] == 1
    stored = ext.load_external_evaluations(settings.platform_dir)
    assert stored[0]["run_id"] == "run-1"
    assert stored[0]["provenance"]["confidence"] == "strong"


def test_import_idempotency_key_replay_and_conflict(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _traces_fixture(settings)
    payload = {"source_file": "imports/traces.jsonl"}
    first = client.post("/api/integrations/langfuse/import", json=payload, headers={"Idempotency-Key": "key-1"})
    assert first.status_code == 200
    second = client.post("/api/integrations/langfuse/import", json=payload, headers={"Idempotency-Key": "key-1"})
    assert second.status_code == 200
    assert second.json()["import_id"] == first.json()["import_id"]
    assert second.json()["idempotent_replay"] is True
    # the replay never re-executes the import: still one stored record
    assert len(ext.load_external_traces(settings.platform_dir)) == 1

    _write_jsonl(settings.platform_dir / "imports" / "other.jsonl", [{"id": "t-2"}])
    conflict = client.post(
        "/api/integrations/langfuse/import",
        json={"source_file": "imports/other.jsonl"},
        headers={"Idempotency-Key": "key-1"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_import_source_file_path_safety(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    outside = tmp_path / "outside.jsonl"
    _write_jsonl(outside, [{"id": "t-1"}])
    staging = settings.platform_dir / "imports" / "staging.jsonl.tmp"
    _write_jsonl(staging, [{"id": "t-1"}])
    for candidate in (str(outside), "../outside.jsonl", "imports/staging.jsonl.tmp"):
        response = client.post("/api/integrations/langfuse/import", json={"source_file": candidate})
        assert response.status_code == 400, candidate
        assert response.json()["detail"]["code"] == "invalid_import"


def test_import_unknown_source_404(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    response = client.post("/api/integrations/bogus/import", json={})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_integration"
    response = client.post("/api/integrations/bogus/validate", json={})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "unknown_integration"


# ---------------------------------------------------------------------------
# import reports
# ---------------------------------------------------------------------------


def test_list_and_get_import_reports(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _traces_fixture(settings)
    report = client.post("/api/integrations/langfuse/import", json={"source_file": "imports/traces.jsonl"}).json()

    listing = client.get("/api/integrations/imports")
    assert listing.status_code == 200
    body = listing.json()
    assert body["count"] == 1
    assert body["imports"][0]["import_id"] == report["import_id"]
    assert body["imports"][0]["source_system"] == "langfuse"

    fetched = client.get("/api/integrations/imports/%s" % report["import_id"])
    assert fetched.status_code == 200
    assert fetched.json()["import_id"] == report["import_id"]

    missing = client.get("/api/integrations/imports/import-nope")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "import_not_found"


# ---------------------------------------------------------------------------
# external evidence reads
# ---------------------------------------------------------------------------


def _import_linked_evidence(client, settings):
    _make_run(settings, "run-1", agent_id="agent-1")
    _traces_fixture(settings)
    client.post("/api/integrations/langfuse/import", json={"source_file": "imports/traces.jsonl"})
    _write_jsonl(
        settings.platform_dir / "imports" / "scores.jsonl",
        [{"id": "s-1", "traceId": "t-1", "name": "quality", "value": 0.7}],
    )
    client.post(
        "/api/integrations/langfuse/import",
        json={"source_file": "imports/scores.jsonl", "kind": "scores"},
    )


def test_run_external_evaluations_and_traces(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _import_linked_evidence(client, settings)

    evaluations = client.get("/api/runs/run-1/external-evaluations")
    assert evaluations.status_code == 200
    body = evaluations.json()
    assert body["run_id"] == "run-1"
    assert body["count"] == 1
    assert body["external_evaluations"][0]["source_id"] == "s-1"

    traces = client.get("/api/runs/run-1/external-traces")
    assert traces.status_code == 200
    body = traces.json()
    assert body["count"] == 1
    assert body["external_traces"][0]["source_id"] == "t-1"
    assert body["external_traces"][0]["run_id"] == "run-1"


def test_run_external_evidence_unknown_run_404(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    for path in ("/api/runs/run-ghost/external-evaluations", "/api/runs/run-ghost/external-traces"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["detail"]["code"] == "run_not_found"


def test_agent_external_evaluations_with_summary(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _import_linked_evidence(client, settings)
    response = client.get("/api/agents/agent-1/external-evaluations")
    assert response.status_code == 200
    body = response.json()
    assert body["agent_id"] == "agent-1"
    assert "external_summary" in body
    summary = body["external_summary"]
    assert summary["trace_count"] == 1
    assert summary["linked_count"] == 1
    assert "langfuse" in summary["source_systems"]


def test_agent_external_evaluations_invalid_id(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    response = client.get("/api/agents/bad%2Fid/external-evaluations")
    assert response.status_code in (400, 404)


# ---------------------------------------------------------------------------
# promptfoo export
# ---------------------------------------------------------------------------


def test_promptfoo_export_case_without_secrets(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _write_json(
        settings.platform_dir / "test_cases.json",
        {
            "items": [
                {
                    "case_id": "case-001",
                    "version": 1,
                    "title": "问候用例",
                    "adapter": "http_session",
                    "input": {"user_id": "user-1", "message": "你好"},
                    "expectations": [{"id": "exp-1", "type": "trajectory_contains", "required": True}],
                    "adapter_config": {"provider_id": "xuanjian-agent", "token": "export-secret-token"},
                }
            ]
        },
    )
    response = client.get("/api/integrations/promptfoo/export/case-001")
    assert response.status_code == 200, response.text
    assert "export-secret-token" not in response.text
    body = response.json()
    assert body["metadata"]["case_id"] == "case-001"
    assert body["metadata"]["platform_schema_version"] == 1
    assert body["promptfoo_compatibility"]

    missing = client.get("/api/integrations/promptfoo/export/case-ghost")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "case_not_found"


# ---------------------------------------------------------------------------
# validate endpoint
# ---------------------------------------------------------------------------


def test_validate_langfuse_invalid_config(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    response = client.post(
        "/api/integrations/langfuse/validate",
        json={"config": {"enabled": True, "base_url": "http://127.0.0.1"}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert body["errors"]
    assert body["normalized_config"] is None


def test_validate_langfuse_file_source(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _traces_fixture(settings)
    response = client.post("/api/integrations/langfuse/validate", json={"source_file": "imports/traces.jsonl"})
    assert response.status_code == 200
    assert response.json()["valid"] is True
    missing = client.post("/api/integrations/langfuse/validate", json={"source_file": "imports/nope.jsonl"})
    assert missing.json()["valid"] is False


def test_validate_promptfoo_valid_file(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _write_json(
        settings.platform_dir / "imports" / "promptfoo.json",
        {"providers": ["openai:gpt-4o-mini"], "tests": [{"description": "ok", "assert": []}]},
    )
    response = client.post(
        "/api/integrations/promptfoo/validate", json={"source_file": "imports/promptfoo.json"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is True
    assert body["total_records"] == 1
    # validate is a dry run: nothing was persisted
    assert not (settings.platform_dir / "external" / "promptfoo_drafts.json").exists()


def test_promptfoo_file_import_via_api(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _write_json(
        settings.platform_dir / "imports" / "promptfoo.json",
        {
            "providers": ["openai:gpt-4o-mini"],
            "tests": [{"description": "ok", "assert": [{"type": "icontains", "value": "hi"}]}],
        },
    )
    response = client.post(
        "/api/integrations/promptfoo/import", json={"source_file": "imports/promptfoo.json"}
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "imported"
    assert report["draft_count"] == 1
    assert (settings.platform_dir / "external" / "promptfoo_drafts.json").is_file()
