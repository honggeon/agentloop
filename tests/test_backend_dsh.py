import json

import zstandard
from fastapi.testclient import TestClient

from auditrail.dsh_source import list_evaluand_sessions
from backend.app.config import Settings
from backend.app.main import app

SESSION_ID = "session-11111111-2222-4333-8444-555555555555"
JUDGE_SESSION_ID = "session-aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
JUDGE_PROFILE = "agentloop-judge"


def _write_dsh_session(dsh_home, session_id=SESSION_ID, agent_preset="standard", cwd="/Users/test/project"):
    events = [
        {
            "type": "session",
            "version": 0,
            "id": session_id,
            "createdAt": 1787000000000,
            "cwd": cwd,
            "agentPreset": agent_preset,
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


def _settings(tmp_path, dsh_home, **overrides):
    values = dict(runs_dir=tmp_path / "runs", dsh_home=dsh_home)
    values.update(overrides)
    return Settings(**values)


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


def test_list_evaluand_sessions_hides_judge_profile_sessions(tmp_path):
    dsh_home = tmp_path / "dsh"
    _write_dsh_session(dsh_home)
    _write_dsh_session(dsh_home, session_id=JUDGE_SESSION_ID, agent_preset=JUDGE_PROFILE)

    sessions = list_evaluand_sessions(dsh_home, judge_profiles=(JUDGE_PROFILE,))

    assert [item["session_id"] for item in sessions] == [SESSION_ID]


def test_list_evaluand_sessions_hides_judge_workspace_sessions(tmp_path):
    dsh_home = tmp_path / "dsh"
    judge_workspace = tmp_path / "judge-workspace"
    _write_dsh_session(dsh_home)
    _write_dsh_session(dsh_home, session_id=JUDGE_SESSION_ID, agent_preset="standard", cwd=str(judge_workspace))

    sessions = list_evaluand_sessions(dsh_home, judge_workspaces=(judge_workspace,))

    assert [item["session_id"] for item in sessions] == [SESSION_ID]


def test_dsh_sessions_route_hides_judge_sessions(tmp_path, monkeypatch):
    dsh_home = tmp_path / "dsh"
    judge_workspace = tmp_path / "judge-workspace"
    judge_workspace.mkdir()
    _write_dsh_session(dsh_home)
    _write_dsh_session(dsh_home, session_id=JUDGE_SESSION_ID, agent_preset=JUDGE_PROFILE, cwd=str(judge_workspace))
    settings = _settings(
        tmp_path,
        dsh_home,
        dsh_judge_profile=JUDGE_PROFILE,
        dsh_judge_workspace=judge_workspace,
    )
    monkeypatch.setattr("backend.app.routes.dsh.get_settings", lambda: settings)
    client = TestClient(app)

    data = client.get("/api/dsh/sessions").json()

    assert [item["session_id"] for item in data["sessions"]] == [SESSION_ID]
