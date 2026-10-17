"""Phase 4 run-index productionization: pause/resume, health, degraded
counters, batched scan, rebuild transparency, shutdown."""

import json
import time

from fastapi.testclient import TestClient

from auditrail import run_index
from auditrail import run_index_watcher as watcher_mod
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


def _client(monkeypatch, settings):
    monkeypatch.setattr("backend.app.routes.admin.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.routes.costs.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.run_index_watch.get_settings", lambda: settings)
    return TestClient(app)


def _write_run(runs_dir, run_id):
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
                "agent_ref": {"id": "agent-1"},
                "test_case_ref": None,
                "suite_ref": None,
                "artifact_manifest": [],
            }
        ),
        encoding="utf-8",
    )


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ---------------------------------------------------------------------------
# pause / resume
# ---------------------------------------------------------------------------


def test_pause_stops_automatic_refresh_and_persists(tmp_path):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    watcher = watcher_mod.RunIndexWatcher(runs_dir, platform_dir, debounce_ms=50, poll_interval_ms=100)
    watcher.start()
    try:
        watcher.pause()
        assert watcher.status()["status"] == "paused"
        _write_run(runs_dir, "run-paused")
        time.sleep(0.5)
        assert watcher.status()["last_refresh_at"] is None  # no auto refresh
        watcher.resume()
        assert watcher.status()["status"] == "running"
        assert _wait(lambda: watcher.status()["last_refresh_at"] is not None)
    finally:
        watcher.stop()


def test_pause_resume_api_and_restart_persistence(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    paused = client.post("/api/admin/run-index/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["paused"] is True
    assert run_index.is_paused(settings.platform_dir)  # persisted, not just in-memory
    health = client.get("/api/admin/run-index/health")
    assert health.status_code == 200
    assert health.json()["paused"] is True
    assert health.json()["authority"] == "local"
    resumed = client.post("/api/admin/run-index/resume")
    assert resumed.json()["paused"] is False
    assert not run_index.is_paused(settings.platform_dir)
    # pause/resume are audited
    events = client.get("/api/audit-events", params={"resource_type": "run_index"})
    actions = [item["action"] for item in events.json()["audit_events"]]
    assert "index_pause" in actions and "index_resume" in actions


def test_pause_requires_admin(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_role="operator")
    client = _client(monkeypatch, settings)
    assert client.post("/api/admin/run-index/pause").status_code == 403


def test_manual_rebuild_allowed_while_paused(tmp_path):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    _write_run(runs_dir, "run-1")
    run_index.set_paused(platform_dir, True)
    status = run_index.rebuild_index(runs_dir, platform_dir)
    assert status["status"] == "ready"
    assert status["indexed_count"] == 1
    assert status["paused"] is True  # rebuild does not unpause
    assert status["refresh_reason"] == "manual"


# ---------------------------------------------------------------------------
# degraded + failure counters
# ---------------------------------------------------------------------------


def test_consecutive_failures_counted_with_redacted_error(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()

    def failing(*args, **kwargs):
        raise RuntimeError("secret path /Users/x/token=abc")

    monkeypatch.setattr(watcher_mod.run_index, "ensure_index", failing)
    watcher = watcher_mod.RunIndexWatcher(runs_dir, platform_dir, debounce_ms=30, poll_interval_ms=50)
    watcher.start()
    try:
        _write_run(runs_dir, "run-1")
        assert _wait(lambda: watcher.status()["failure_count"] >= 1)
        status = watcher.status()
        assert status["status"] == "degraded"
        # redacted: exception type only, never the raw message
        assert status["last_error"] == "RuntimeError"
        assert "token=abc" not in json.dumps(status)
    finally:
        watcher.stop()


def test_watcher_recovers_from_degraded_on_success(tmp_path):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    watcher = watcher_mod.RunIndexWatcher(runs_dir, platform_dir, debounce_ms=50, poll_interval_ms=80)
    watcher.start()
    try:
        watcher._record_error(RuntimeError("x"))
        assert watcher.status()["status"] == "degraded"
        _write_run(runs_dir, "run-ok")
        assert _wait(lambda: watcher.status()["status"] == "running")
        assert watcher.status()["failure_count"] == 0
    finally:
        watcher.stop()


# ---------------------------------------------------------------------------
# batched scan + refresh reasons
# ---------------------------------------------------------------------------


def test_large_tree_scanned_in_batches(tmp_path):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    for index in range(12):
        _write_run(runs_dir, "run-%02d" % index)
    status = run_index.rebuild_index(runs_dir, platform_dir, scan_batch=5)
    assert status["indexed_count"] == 12
    assert status["status"] == "ready"


def test_refresh_reason_recorded_in_meta(tmp_path):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    _write_run(runs_dir, "run-1")
    entries, block = run_index.ensure_index(runs_dir, platform_dir, reason="import_complete")
    assert block["refresh_reason"] == "import_complete"
    status = run_index.index_status(platform_dir)
    assert status["refresh_reason"] == "import_complete"
    assert any(item["reason"] == "import_complete" for item in status["refresh_history"])


def test_corrupt_index_falls_back_and_rebuild_recovers(tmp_path):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    platform_dir.mkdir()
    _write_run(runs_dir, "run-1")
    run_index.rebuild_index(runs_dir, platform_dir)
    (platform_dir / "run_index.json").write_text("{corrupt", encoding="utf-8")
    entries, block = run_index.ensure_index(runs_dir, platform_dir)
    assert entries is None
    assert block["status"] == "unavailable"
    recovered = run_index.rebuild_index(runs_dir, platform_dir)
    assert recovered["status"] == "ready"
    assert recovered["indexed_count"] == 1


def test_rebuild_during_rebuild_returns_409(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _write_run(settings.runs_dir, "run-1")
    # simulate an in-progress rebuild from another request
    meta_path = settings.platform_dir / "run_index_meta.json"
    meta_path.write_text(
        json.dumps({"schema_version": 1, "status": "rebuilding", "refresh_reason": "manual"}),
        encoding="utf-8",
    )
    response = client.post("/api/admin/run-index/rebuild")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "index_rebuilding"


def test_watcher_shutdown_stops_thread(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    watcher = watcher_mod.RunIndexWatcher(runs_dir, tmp_path / "platform", poll_interval_ms=50)
    watcher.start(paused=True)
    assert watcher.status()["status"] == "paused"
    watcher.stop()
    assert watcher._thread is None
