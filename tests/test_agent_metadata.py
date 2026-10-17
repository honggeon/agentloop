"""Phase 2 Agent metadata: create/update/delete, validation, compatibility."""

import json

from fastapi.testclient import TestClient

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
    monkeypatch.setattr("backend.app.routes.platform.get_settings", lambda: settings)
    return TestClient(app), settings


def _payload(**overrides):
    payload = {
        "agent_id": "agent-001",
        "name": "Demo Agent",
        "adapter": "http_session",
        "endpoint": "http://agent.internal",
        "capabilities": ["session"],
    }
    payload.update(overrides)
    return payload


def _make_run(runs_dir, run_id, agent_id="agent-001"):
    run_dir = runs_dir / run_id
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
                "agent_ref": {"id": agent_id, "name": "Demo Agent", "environment": "staging"},
                "test_case_ref": {"id": "case-001", "version": 1},
                "suite_ref": None,
                "artifact_manifest": ["summary.json"],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "summary.json").write_text("{}", encoding="utf-8")
    return run_dir


def test_create_writes_formal_metadata(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    response = client.post(
        "/api/agents",
        json=_payload(environment="staging", description="回归测试", tags=["regression", "tool-use"]),
    )
    assert response.status_code == 201, response.text
    agent = response.json()["agent"]
    assert agent["environment"] == "staging"
    assert agent["description"] == "回归测试"
    assert agent["tags"] == ["regression", "tool-use"]
    assert agent["enabled"] is True
    assert agent["source_host"] == "agent.internal"
    assert agent["created_at"] is not None
    assert agent["updated_at"] is not None


def test_create_defaults_environment_null_not_production(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    agent = client.post("/api/agents", json=_payload()).json()["agent"]
    assert agent["environment"] is None
    assert agent["tags"] == []
    assert agent["description"] == ""


def test_custom_environment_preserved_verbatim(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    agent = client.post("/api/agents", json=_payload(environment="qa-blue")).json()["agent"]
    assert agent["environment"] == "qa-blue"


def test_update_preserves_created_at_and_bumps_updated_at(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    created = client.post("/api/agents", json=_payload(environment="staging")).json()["agent"]
    response = client.put("/api/agents/agent-001", json={"environment": "production", "tags": ["live"]})
    assert response.status_code == 200, response.text
    updated = response.json()["agent"]
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] >= created["updated_at"]
    assert updated["environment"] == "production"
    assert updated["tags"] == ["live"]


def test_update_unknown_agent_404(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.put("/api/agents/ghost", json={"name": "x"}).status_code == 404


def test_legacy_config_without_new_fields_reads_compat(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    legacy = {
        "agent_id": "agent-old",
        "name": "Old Agent",
        "version": "",
        "adapter": "http_session",
        "endpoint": "http://agent.internal",
        "capabilities": ["session"],
        "metadata": {},
        "config_hash": "h",
    }
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    (settings.platform_dir / "agents.json").write_text(
        json.dumps({"items": [legacy]}, ensure_ascii=False), encoding="utf-8"
    )
    body = client.get("/api/agents/agent-old").json()
    # no fabricated business time from the config file
    assert body["agent"]["created_at"] is None
    assert body["agent"]["environment"] is None
    assert body["agent"]["tags"] == []
    assert body["agent"]["config_status"] == "registered"
    # update backfills updated_at but keeps created_at null
    updated = client.put("/api/agents/agent-old", json={"description": "d"}).json()["agent"]
    assert updated["created_at"] is None
    assert updated["updated_at"] is not None
    assert updated["description"] == "d"


def test_environment_tags_validation(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.post("/api/agents", json=_payload(environment=123)).status_code == 400
    assert client.post("/api/agents", json=_payload(tags="not-a-list")).status_code == 400
    assert client.post("/api/agents", json=_payload(tags=["x" * 65])).status_code == 400
    assert client.post("/api/agents", json=_payload(tags=[""])).status_code == 400
    assert client.post("/api/agents", json=_payload(tags=["t"] * 33)).status_code == 400


def test_metadata_rejects_secret_keys_and_values(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.post("/api/agents", json=_payload(metadata={"api_key": "abc"})).status_code == 400
    assert client.post("/api/agents", json=_payload(metadata={"Authorization": "Bearer x"})).status_code == 400
    assert client.post("/api/agents", json=_payload(metadata={"note": "sk-123456"})).status_code == 400
    assert client.post("/api/agents", json=_payload(metadata={"system_prompt": "..."})).status_code == 400


def test_endpoint_with_embedded_credentials_rejected(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    response = client.post("/api/agents", json=_payload(endpoint="http://user:pass@agent.internal"))
    assert response.status_code == 400
    assert "credential" in response.text.lower() or "endpoint" in response.text.lower()


def test_update_does_not_rewrite_historical_run_agent_ref(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post("/api/agents", json=_payload())
    run_dir = _make_run(settings.runs_dir, "run-hist")
    before = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    client.put("/api/agents/agent-001", json={"name": "Renamed", "environment": "production"})
    after = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert after == before


def test_delete_keeps_runs_and_marks_missing_configuration(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post("/api/agents", json=_payload())
    run_dir = _make_run(settings.runs_dir, "run-keep")
    response = client.delete("/api/agents/agent-001")
    assert response.status_code == 200, response.text
    # the run directory and its agent_ref snapshot survive
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["agent_ref"]["id"] == "agent-001"
    # the agent is still visible, derived from run data, explicitly marked
    body = client.get("/api/agents/agent-001").json()
    assert body["agent"]["derived"] is True
    assert body["agent"]["config_status"] == "missing_configuration"
    assert body["agent"]["created_at"] is None
    assert client.delete("/api/agents/agent-001").status_code == 404
