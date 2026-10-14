import json

import pytest
import zstandard

from auditrail import cli
from auditrail.dsh_source import (
    build_session_response,
    events_to_messages,
    find_acp_session_file,
    find_session_file,
    list_sessions,
    read_events,
    session_info,
    session_log_path,
    source_meta,
    validate_acp_session_id,
    validate_session_id,
    _model_of,
)

SESSION_ID = "session-11111111-2222-4333-8444-555555555555"


def _events():
    return [
        {
            "type": "session",
            "version": 0,
            "id": SESSION_ID,
            "createdAt": 1787000000000,
            "cwd": "/Users/test/project",
            "agentPreset": "standard",
        },
        {"type": "permission/preset", "seq": 0, "time": 1787000000001, "data": {"preset": "workspace-write"}},
        {
            "type": "user/message",
            "seq": 7,
            "time": 1787000001000,
            "data": {
                "role": "user",
                "id": "u1",
                "content": [{"type": "text", "text": "帮我修复测试"}],
            },
        },
        {"type": "session/title", "seq": 11, "time": 1787000001001, "data": {"title": "修复测试"}},
        {
            "type": "assistant/message",
            "seq": 20,
            "time": 1787000002000,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "assistant",
                    "id": "a1",
                    "content": [
                        {"type": "reasoning", "text": "先读文件"},
                        {"type": "text", "text": "我来读取文件。"},
                        {"type": "tool-call", "id": "call_1", "name": "read_file", "arguments": "{\"path\": \"a.py\"}"},
                    ],
                },
            },
        },
        {
            "type": "tool/call",
            "seq": 21,
            "time": 1787000002001,
            "data": {"turn": 1, "step": 1, "callId": "call_1", "name": "read_file", "arguments": "{\"path\": \"a.py\"}"},
        },
        {
            "type": "tool/result",
            "seq": 22,
            "time": 1787000003000,
            "data": {
                "turn": 1,
                "step": 1,
                "message": {
                    "role": "user",
                    "id": "t1",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "call_1",
                            "content": [{"type": "text", "text": "file content"}],
                            "isError": False,
                        }
                    ],
                },
            },
        },
        {
            "type": "assistant/message",
            "seq": 30,
            "time": 1787000004000,
            "data": {
                "turn": 1,
                "step": 2,
                "message": {
                    "role": "assistant",
                    "id": "a2",
                    "content": [{"type": "text", "text": "已修复，测试通过。"}],
                },
            },
        },
        {"type": "assistant/chunk", "seq": 31, "time": 1787000004001, "data": {"text": "已修复"}},
    ]


def _write_session(dsh_home, workspace_dir="--Users-test-project--", session_id=SESSION_ID, events=None):
    path = dsh_home / "sessions" / workspace_dir / session_id / "session.jsonl.zstd"
    path.parent.mkdir(parents=True)
    payload = "\n".join(json.dumps(event, ensure_ascii=False) for event in (events or _events()))
    path.write_bytes(zstandard.ZstdCompressor().compress(payload.encode("utf-8")))
    return path


def test_read_events_roundtrip(tmp_path):
    path = _write_session(tmp_path)
    events = read_events(path)
    assert events[0]["type"] == "session"
    assert len(events) == 9


def test_session_info_counts(tmp_path):
    path = _write_session(tmp_path)
    info = session_info(read_events(path))
    assert info["session_id"] == SESSION_ID
    assert info["title"] == "修复测试"
    assert info["workspace"] == "/Users/test/project"
    assert info["agent_preset"] == "standard"
    assert info["user_message_count"] == 1
    assert info["assistant_message_count"] == 2
    assert info["tool_call_count"] == 1
    assert info["tool_result_count"] == 1


def test_list_sessions_newest_first(tmp_path):
    older_id = "session-00000000-0000-4000-8000-000000000001"
    older_events = [dict(event) for event in _events()]
    older_events[0] = {**older_events[0], "id": older_id}
    older = _write_session(tmp_path, session_id=older_id, events=older_events)
    newer = _write_session(tmp_path, session_id=SESSION_ID)
    import os

    os.utime(older, (1000, 1000))
    os.utime(newer, (2000, 2000))
    sessions = list_sessions(tmp_path)
    assert [item["session_id"] for item in sessions] == [SESSION_ID, older_id]
    limited = list_sessions(tmp_path, limit=1)
    assert len(limited) == 1


def test_validate_session_id():
    assert validate_session_id(SESSION_ID) == SESSION_ID
    for bad in ("", "../etc", "session-x", "foo/bar", None):
        with pytest.raises(ValueError):
            validate_session_id(bad)


def test_find_session_file(tmp_path):
    path = _write_session(tmp_path)
    assert find_session_file(tmp_path, SESSION_ID) == path
    assert find_session_file(tmp_path, SESSION_ID, "--Users-test-project--") == path
    with pytest.raises(FileNotFoundError):
        find_session_file(tmp_path, "session-99999999-9999-4999-8999-999999999999")
    with pytest.raises(ValueError):
        find_session_file(tmp_path, "..", None)
    with pytest.raises(ValueError):
        find_session_file(tmp_path, SESSION_ID, "../escape")


def _write_generation(dsh_home, session_id, filename, workspace_dir="--Users-test-project--"):
    path = dsh_home / "sessions" / workspace_dir / session_id / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(event, ensure_ascii=False) for event in _events())
    path.write_bytes(zstandard.ZstdCompressor().compress(payload.encode("utf-8")))
    return path


def test_session_log_path_finds_every_format_generation(tmp_path):
    zero = _write_generation(tmp_path, SESSION_ID, "session.jsonl.zstd")
    assert session_log_path(zero.parent) == zero

    four = _write_generation(tmp_path, SESSION_ID, "session.v4.jsonl.zstd")
    assert session_log_path(four.parent) == four

    # A directory that still holds both generations resolves to the newest one.
    assert session_log_path(zero.parent) == four
    corrupted = zero.parent / "session.jsonl.zstd.corrupt-bak-20260906"
    corrupted.write_bytes(b"")
    assert session_log_path(zero.parent) == four
    assert session_log_path(zero.parent / "missing") is None


def test_find_session_file_reads_the_current_format_generation(tmp_path):
    path = _write_generation(tmp_path, SESSION_ID, "session.v4.jsonl.zstd")

    assert find_session_file(tmp_path, SESSION_ID) == path
    assert find_session_file(tmp_path, SESSION_ID, "--Users-test-project--") == path
    assert [item["session_id"] for item in list_sessions(tmp_path)] == [SESSION_ID]


def test_validate_acp_session_id():
    acp_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    assert validate_acp_session_id(acp_id) == acp_id
    # Headless root ids are a different shape and must not be accepted here.
    for bad in ("", "../etc", "session-%s" % acp_id, "foo/bar", SESSION_ID, None):
        with pytest.raises(ValueError):
            validate_acp_session_id(bad)


def test_find_acp_session_file_uses_the_opaque_uuid(tmp_path):
    acp_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    path = _write_generation(tmp_path, acp_id, "session.v4.jsonl.zstd")

    assert find_acp_session_file(tmp_path, acp_id) == path
    assert find_acp_session_file(tmp_path, acp_id, "--Users-test-project--") == path
    with pytest.raises(FileNotFoundError):
        find_acp_session_file(tmp_path, "11111111-2222-4333-8444-555555555555")
    with pytest.raises(ValueError):
        find_acp_session_file(tmp_path, SESSION_ID)
    with pytest.raises(ValueError):
        find_acp_session_file(tmp_path, "..")
    with pytest.raises(ValueError):
        find_acp_session_file(tmp_path, acp_id, "../escape")


def test_find_acp_session_file_rejects_ambiguous_workspaces(tmp_path):
    acp_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    _write_generation(tmp_path, acp_id, "session.v4.jsonl.zstd", workspace_dir="--Users-first--")
    _write_generation(tmp_path, acp_id, "session.jsonl.zstd", workspace_dir="--Users-second--")

    with pytest.raises(FileNotFoundError):
        find_acp_session_file(tmp_path, acp_id)


def test_events_to_messages_mapping():
    messages = events_to_messages(_events())
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "assistant"]

    assistant = messages[1]
    assert assistant["content"] == "我来读取文件。"
    assert assistant["reasoning"] == "先读文件"
    assert assistant["tool_calls"] == [{"id": "call_1", "name": "read_file", "arguments": "{\"path\": \"a.py\"}"}]

    tool = messages[2]
    assert tool["tool_call_id"] == "call_1"
    assert tool["tool_name"] == "read_file"
    assert tool["content"] == "file content"

    final = messages[3]
    assert final["content"] == "已修复，测试通过。"
    assert "tool_calls" not in final


def test_build_session_response_filters_chunks():
    response = build_session_response(_events())
    session = response["session"]
    assert session["session_id"] == SESSION_ID
    assert session["title"] == "修复测试"
    assert session["workspace"] == "/Users/test/project"
    assert len(session["messages"]) == 4
    event_types = [event["type"] for event in response["dsh_events"]]
    assert "assistant/chunk" not in event_types
    assert "tool/call" in event_types
    meta = source_meta(response)
    assert meta["source_type"] == "dsh"
    assert meta["server_host"] == "deepseek-harness"
    assert meta["dsh_session_id"] == SESSION_ID


def test_model_of_reads_nested_request_header_config():
    events = _events() + [
        {
            "type": "request/header",
            "seq": 13,
            "time": 1787000000500,
            "data": {"header": {"config": {"provider": "kimi-coding", "model": "k3"}}, "reason": "initial"},
        }
    ]
    assert _model_of(events) == "k3"
    response = build_session_response(events)
    assert response["session"]["model"] == "k3"


def test_model_of_last_event_wins_and_legacy_flat_shape():
    events = [
        {"type": "request/header", "data": {"model": "legacy-model"}},
        {"type": "request/header", "data": {"header": {"config": {"model": "k3"}}}},
    ]
    assert _model_of(events) == "k3"
    assert _model_of(events[:1]) == "legacy-model"
    assert _model_of(_events()) is None


def test_cli_render_run_recovers_dsh_model(tmp_path, capsys):
    events = _events() + [
        {
            "type": "request/header",
            "seq": 13,
            "time": 1787000000500,
            "data": {"header": {"config": {"model": "k3"}}},
        }
    ]
    # Simulate a run imported before model extraction worked: session.model is
    # already null, but the raw dsh_events are still in session.json.
    response = build_session_response(events)
    response["session"]["model"] = None
    run = tmp_path / "runs" / "20260901-000000-dsh-old"
    cli._write_run_outputs(run, response)
    assert json.loads((run / "summary.json").read_text(encoding="utf-8"))["model"] is None

    code = cli.main(["render-run", "--run", str(run)])
    assert code == 0
    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["model"] == "k3"


def test_cli_import_dsh_session_writes_run_contract(tmp_path, capsys):
    dsh_home = tmp_path / "dsh"
    _write_session(dsh_home)
    out = tmp_path / "runs"
    code = cli.main(
        ["import-dsh-session", "--dsh-home", str(dsh_home), "--session-id", SESSION_ID, "--out", str(out)]
    )
    assert code == 0
    run_dirs = list(out.iterdir())
    assert len(run_dirs) == 1
    run = run_dirs[0]
    assert run.name.startswith("dsh-") or "-dsh-" in run.name
    for filename in ("session.json", "summary.json", "trajectory.jsonl", "audit.json", "tool_audit.md", "source.json"):
        assert (run / filename).exists(), filename

    audit = json.loads((run / "audit.json").read_text(encoding="utf-8"))
    assert audit["tool_call_count"] == 1
    assert audit["missing_tool_results"] == []
    assert audit["final_answer_chars"] > 0

    summary = json.loads((run / "summary.json").read_text(encoding="utf-8"))
    assert summary["session_id"] == SESSION_ID
    assert summary["source_type"] == "dsh"

    trajectory = (run / "trajectory.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(trajectory) == 4


def test_cli_import_dsh_session_missing(tmp_path, capsys):
    code = cli.main(
        ["import-dsh-session", "--dsh-home", str(tmp_path), "--session-id", SESSION_ID, "--out", str(tmp_path / "runs")]
    )
    assert code == 2
    assert "no DSH session recording" in capsys.readouterr().err


def test_cli_list_dsh_sessions_json(tmp_path, capsys):
    _write_session(tmp_path)
    code = cli.main(["list-dsh-sessions", "--dsh-home", str(tmp_path), "--json"])
    assert code == 0
    data = json.loads(capsys.readouterr().out)
    assert data["sessions"][0]["session_id"] == SESSION_ID
