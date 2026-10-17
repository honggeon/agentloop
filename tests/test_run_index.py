"""Phase 2 run index: build, incremental updates, fallback, admin API."""

import json
import os
import time

from fastapi.testclient import TestClient

from auditrail import run_index
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
    for module in ("backend.app.routes.platform", "backend.app.routes.costs"):
        monkeypatch.setattr("%s.get_settings" % module, lambda: settings)
    return TestClient(app), settings


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_run(runs_dir, run_id, *, agent_id="agent-001", status="completed", with_run_json=True):
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if with_run_json:
        _write(
            run_dir / "run.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": status,
                "status_reason": None,
                "started_at": "2026-01-05T10:00:00Z",
                "finished_at": "2026-01-05T10:00:02Z",
                "agent_ref": {"id": agent_id, "name": "Demo Agent", "environment": "staging"} if agent_id else None,
                "test_case_ref": {"id": "case-001", "case_id": "case-001", "version": 1},
                "suite_ref": None,
                "artifact_manifest": [],
            },
        )
    (run_dir / "trajectory.jsonl").write_text('{"index": 0, "role": "user"}\n', encoding="utf-8")
    return run_dir


def test_first_scan_builds_index(tmp_path):
    settings = _settings(tmp_path)
    _make_run(settings.runs_dir, "run-1")
    entries, block = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    assert block["used"] is True
    assert block["status"] == "ready"
    assert block["indexed_count"] == 1
    entry = entries["run-1"]
    assert entry["agent_id"] == "agent-001"
    assert entry["status"] == "completed"
    assert entry["has_trajectory"] is True
    assert entry["cost_source"] == "missing"
    assert entry["total_cost"] is None
    # index stores summaries only — no absolute paths
    assert not str(entry["path"]).startswith("/")


def test_incremental_add_modify_delete(tmp_path):
    settings = _settings(tmp_path)
    _make_run(settings.runs_dir, "run-1")
    entries, _ = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    assert set(entries) == {"run-1"}

    # new run is picked up incrementally
    _make_run(settings.runs_dir, "run-2", agent_id="agent-002")
    entries, block = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    assert set(entries) == {"run-1", "run-2"}
    assert entries["run-2"]["agent_id"] == "agent-002"

    # modified run is re-read (status flip)
    _make_run(settings.runs_dir, "run-1", status="failed")
    entries, _ = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    assert entries["run-1"]["status"] == "failed"

    # deleted run stops being served
    for path in (settings.runs_dir / "run-2").iterdir():
        path.unlink()
    (settings.runs_dir / "run-2").rmdir()
    entries, _ = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    assert set(entries) == {"run-1"}


def test_tmp_files_never_indexed(tmp_path):
    settings = _settings(tmp_path)
    _make_run(settings.runs_dir, "run-1")
    staging = settings.runs_dir / ".run.json.123.tmp"
    staging.write_text("{}", encoding="utf-8")
    hidden = settings.runs_dir / ".hidden"
    hidden.mkdir()
    entries, _ = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    assert set(entries) == {"run-1"}


def test_single_bad_run_does_not_block_index(tmp_path):
    settings = _settings(tmp_path)
    _make_run(settings.runs_dir, "run-good")
    bad = settings.runs_dir / "run-bad"
    bad.mkdir(parents=True)

    def boom(run_dir, **kwargs):
        if run_dir.name == "run-bad":
            raise RuntimeError("corrupt")
        return run_index.build_entry.__wrapped__(run_dir, **kwargs) if hasattr(run_index.build_entry, "__wrapped__") else _real(run_dir, **kwargs)

    _real = run_index.build_entry
    run_index.build_entry = boom
    try:
        entries, block = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    finally:
        run_index.build_entry = _real
    assert "run-good" in entries
    meta = run_index.load_meta(settings.platform_dir)
    assert meta["error_count"] == 1
    assert meta["errors"][0]["run_id"] == "run-bad"


def test_corrupt_index_falls_back_then_rebuilds(tmp_path):
    settings = _settings(tmp_path)
    _make_run(settings.runs_dir, "run-1")
    run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    (settings.platform_dir / "run_index.json").write_text("{corrupt", encoding="utf-8")

    entries, block = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    assert entries is None
    assert block["status"] == "unavailable"

    status = run_index.rebuild_index(settings.runs_dir, settings.platform_dir)
    assert status["status"] == "ready"
    entries, block = run_index.ensure_index(settings.runs_dir, settings.platform_dir)
    assert entries is not None and "run-1" in entries


def test_admin_index_api(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _make_run(settings.runs_dir, "run-1")
    status = client.get("/api/admin/run-index/status")
    assert status.status_code == 200
    assert status.json()["authority"] == "local"
    rebuild = client.post("/api/admin/run-index/rebuild")
    assert rebuild.status_code == 200
    body = rebuild.json()
    assert body["status"] == "ready"
    assert body["indexed_count"] == 1
    assert body["authority"] == "local"


def test_admin_index_disabled(monkeypatch, tmp_path):
    settings = Settings(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
        local_admin_enabled=False,
    )
    monkeypatch.setattr("backend.app.routes.costs.get_settings", lambda: settings)
    client = TestClient(app)
    assert client.get("/api/admin/run-index/status").status_code == 403
    assert client.post("/api/admin/run-index/rebuild").status_code == 403


def test_overview_uses_index_and_reports_block(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post(
        "/api/agents",
        json={
            "agent_id": "agent-001",
            "name": "Demo Agent",
            "adapter": "http_session",
            "endpoint": "http://agent.internal",
            "capabilities": ["session"],
        },
    )
    _make_run(settings.runs_dir, "run-1")
    body = client.get("/api/agents/agent-001/overview?range=all").json()
    assert body["index"]["used"] is True
    assert body["index"]["status"] == "ready"
    assert body["index"]["indexed_count"] == 1
    assert body["kpis"]["run_count"] == 1
    assert body["cost"]["source"] == "missing"
    assert body["cost"]["total"] is None


def test_overview_falls_back_when_index_corrupt(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post(
        "/api/agents",
        json={
            "agent_id": "agent-001",
            "name": "Demo Agent",
            "adapter": "http_session",
            "endpoint": "http://agent.internal",
            "capabilities": ["session"],
        },
    )
    _make_run(settings.runs_dir, "run-1")
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    (settings.platform_dir / "run_index.json").write_text("{corrupt", encoding="utf-8")
    body = client.get("/api/agents/agent-001/overview?range=all").json()
    assert body["index"]["used"] is False
    assert body["index"]["status"] == "unavailable"
    # safe scan still serves the data
    assert body["kpis"]["run_count"] == 1
    runs = client.get("/api/agents/agent-001/runs?range=all").json()
    assert runs["index"]["used"] is False
    assert runs["total"] == 1


def test_agent_list_uses_index_stats(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post(
        "/api/agents",
        json={
            "agent_id": "agent-001",
            "name": "Demo Agent",
            "adapter": "http_session",
            "endpoint": "http://agent.internal",
            "capabilities": ["session"],
        },
    )
    _make_run(settings.runs_dir, "run-1")
    body = client.get("/api/agents").json()
    agent = body["agents"][0]
    assert agent["stats"]["run_count"] == 1
    assert agent["stats"]["last_run_status"] == "completed"
    assert body["index"]["used"] is True
