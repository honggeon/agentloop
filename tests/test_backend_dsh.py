import json

import zstandard
from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import app

SESSION_ID = "session-11111111-2222-4333-8444-555555555555"


def _write_dsh_session(dsh_home, session_id=SESSION_ID):
    events = [
        {
            "type": "session",
            "version": 0,
            "id": session_id,
            "createdAt": 1787000000000,
            "cwd": "/Users/test/project",
            "agentPreset": "standard",
        },
        {
            "type": "user/message",
            "seq": 1,
            "time": 1787000001000,
            "data": {"role": "user", "id": "u1", "content": [{"type": "text", "text": "你好"}]},
        },
        {
            "type": "assistant/message",
            "seq": 2,
            "time": 1787000002000,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {"role": "assistant", "id": "a1", "content": [{"type": "text", "text": "你好！有什么可以帮你？"}]},
            },
        },
    ]
    path = dsh_home / "sessions" / "--Users-test-project--" / session_id / "session.jsonl.zstd"
    path.parent.mkdir(parents=True)
    payload = "\n".join(json.dumps(event, ensure_ascii=False) for event in events)
    path.write_bytes(zstandard.ZstdCompressor().compress(payload.encode("utf-8")))
    return path


def _settings(tmp_path, dsh_home):
    return Settings(runs_dir=tmp_path / "runs", dsh_home=dsh_home)


def test_list_dsh_sessions(tmp_path, monkeypatch):
    dsh_home = tmp_path / "dsh"
    _write_dsh_session(dsh_home)
    monkeypatch.setattr("backend.app.routes.dsh.get_settings", lambda: _settings(tmp_path, dsh_home))
    client = TestClient(app)
    data = client.get("/api/dsh/sessions").json()
    assert data["sessions"][0]["session_id"] == SESSION_ID
    assert data["sessions"][0]["user_message_count"] == 1


def test_import_dsh_session(tmp_path, monkeypatch):
    dsh_home = tmp_path / "dsh"
    _write_dsh_session(dsh_home)
    monkeypatch.setattr("backend.app.routes.dsh.get_settings", lambda: _settings(tmp_path, dsh_home))
    client = TestClient(app)
    response = client.post("/api/dsh/import", json={"session_id": SESSION_ID})
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    run_dir = tmp_path / "runs" / run_id
    assert (run_dir / "audit.json").exists()
    assert (run_dir / "trajectory.jsonl").exists()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["source_type"] == "dsh"


def test_import_dsh_session_rejects_bad_id(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.app.routes.dsh.get_settings", lambda: _settings(tmp_path, tmp_path / "dsh"))
    client = TestClient(app)
    assert client.post("/api/dsh/import", json={}).status_code == 400
    assert client.post("/api/dsh/import", json={"session_id": "../etc"}).status_code == 400
    assert client.post("/api/dsh/import", json={"session_id": "session-99999999-9999-4999-8999-999999999999"}).status_code == 404
