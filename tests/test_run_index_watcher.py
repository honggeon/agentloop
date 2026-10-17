"""Phase 3 run-index watcher: opt-in polling, debounce, degraded, API block."""

import json
import time

import pytest
from fastapi.testclient import TestClient

from auditrail import run_index
from auditrail import run_index_watcher as watcher_mod
from backend.app.config import Settings
from backend.app.main import app


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
    return run_dir


def _wait_for(predicate, timeout=3.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# disabled by default
# ---------------------------------------------------------------------------


def test_watcher_disabled_by_default(tmp_path):
    assert watcher_mod.watcher_from_env(tmp_path / "runs", tmp_path / "platform", enabled=False) is None
    status = watcher_mod.disabled_status()
    assert status["enabled"] is False
    assert status["status"] == "disabled"
    assert status["mode"] == "polling"
    assert status["error_count"] == 0


# ---------------------------------------------------------------------------
# refresh on new run
# ---------------------------------------------------------------------------


def test_new_run_triggers_refresh(tmp_path):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    watcher = watcher_mod.RunIndexWatcher(runs_dir, platform_dir, debounce_ms=50, poll_interval_ms=100)
    watcher.start()
    try:
        assert watcher.status()["status"] == "running"
        _write_run(runs_dir, "run-new")
        assert _wait_for(lambda: watcher.status()["last_refresh_at"] is not None)
        index = run_index.load_index(platform_dir)
        assert index is not None
        assert "run-new" in index["entries"]
    finally:
        watcher.stop()
    assert watcher.status()["last_refresh_at"] is not None


def test_event_burst_is_debounced(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    calls = []
    real_ensure = run_index.ensure_index

    def counting_ensure(*args, **kwargs):
        calls.append(1)
        return real_ensure(*args, **kwargs)

    monkeypatch.setattr(watcher_mod.run_index, "ensure_index", counting_ensure)
    watcher = watcher_mod.RunIndexWatcher(runs_dir, platform_dir, debounce_ms=100, poll_interval_ms=100)
    watcher.start()
    try:
        for index in range(5):
            _write_run(runs_dir, "run-%d" % index)
        assert _wait_for(lambda: watcher.status()["last_refresh_at"] is not None)
        time.sleep(0.4)  # let any extra debounce windows elapse
        # a burst collapses into a bounded number of refreshes, never one per file
        assert 1 <= len(calls) <= 3
    finally:
        watcher.stop()


# ---------------------------------------------------------------------------
# degraded on failure
# ---------------------------------------------------------------------------


def test_refresh_failure_degrades_watcher_not_api(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()

    def failing_ensure(*args, **kwargs):
        raise RuntimeError("index exploded")

    monkeypatch.setattr(watcher_mod.run_index, "ensure_index", failing_ensure)
    watcher = watcher_mod.RunIndexWatcher(runs_dir, platform_dir, debounce_ms=50, poll_interval_ms=100)
    watcher.start()
    try:
        _write_run(runs_dir, "run-1")
        assert _wait_for(lambda: watcher.status()["status"] == "degraded")
        status = watcher.status()
        assert status["error_count"] > 0
        # the worker thread survives the failure and status stays callable
        assert watcher._thread is not None and watcher._thread.is_alive()
        assert status["mode"] == "polling"
    finally:
        watcher.stop()


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


def test_stop_releases_thread(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    watcher = watcher_mod.RunIndexWatcher(runs_dir, tmp_path / "platform", poll_interval_ms=100)
    watcher.start()
    assert watcher._thread is not None and watcher._thread.is_alive()
    watcher.stop()
    assert watcher._thread is None
    assert watcher.status()["status"] == "stopped"
    # stop is idempotent
    watcher.stop()
    assert watcher._thread is None


def test_start_is_idempotent(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    watcher = watcher_mod.RunIndexWatcher(runs_dir, tmp_path / "platform", poll_interval_ms=100)
    watcher.start()
    first = watcher._thread
    watcher.start()
    assert watcher._thread is first
    watcher.stop()


# ---------------------------------------------------------------------------
# fingerprint ignores staging/hidden entries
# ---------------------------------------------------------------------------


def test_tmp_and_hidden_dirs_do_not_change_fingerprint(tmp_path):
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_run(runs_dir, "run-1")
    before = watcher_mod._cheap_fingerprint(runs_dir)
    (runs_dir / "staging.json.tmp").mkdir()
    (runs_dir / ".hidden").mkdir()
    (runs_dir / "note.txt").write_text("x", encoding="utf-8")
    after = watcher_mod._cheap_fingerprint(runs_dir)
    assert before == after
    assert [entry[0] for entry in after] == ["run-1"]


# ---------------------------------------------------------------------------
# manual index operations never depend on the watcher
# ---------------------------------------------------------------------------


def test_manual_rebuild_and_fallback_scan_work_without_watcher(tmp_path):
    runs_dir = tmp_path / "runs"
    platform_dir = tmp_path / "platform"
    runs_dir.mkdir()
    _write_run(runs_dir, "run-1")
    status = run_index.rebuild_index(runs_dir, platform_dir)
    assert status["status"] == "ready"
    assert status["indexed_count"] == 1
    entries, block = run_index.ensure_index(runs_dir, platform_dir)
    assert block["used"] is True
    assert "run-1" in entries


# ---------------------------------------------------------------------------
# admin status API exposes the watcher block
# ---------------------------------------------------------------------------


def test_run_index_status_api_includes_watcher_block(monkeypatch, tmp_path):
    settings = Settings(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
    )
    settings.runs_dir.mkdir(parents=True)
    monkeypatch.setattr("backend.app.routes.costs.get_settings", lambda: settings)
    client = TestClient(app)
    response = client.get("/api/admin/run-index/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["authority"] == "local"
    watcher = body["watcher"]
    assert watcher["enabled"] is False
    assert watcher["status"] == "disabled"
    assert watcher["mode"] == "polling"
