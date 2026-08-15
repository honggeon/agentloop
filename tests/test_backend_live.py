import json

from fastapi.testclient import TestClient

from agentloop.client import AgentLoopHTTPError
from backend.app.config import Settings
from backend.app.main import app


def _sse_events(response):
    events = []
    current = {"event": "message", "data": ""}
    for line in "".join(response.iter_text()).splitlines():
        if not line:
            if current["data"]:
                events.append((current["event"], json.loads(current["data"])))
            current = {"event": "message", "data": ""}
            continue
        if line.startswith("event:"):
            current["event"] = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            current["data"] += line.split(":", 1)[1].strip()
    return events


def test_live_evaluate_streams_graph_and_persists_run(tmp_path, monkeypatch):
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.live.get_settings", lambda: settings)

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            assert base_url == "http://agent.example"
            assert user_id == "u1"
            assert token == "secret"

        def create_session(self, title="agentloop smoke test", model=""):
            return {"session": {"session_id": "s1"}}

        def start_chat(self, session_id, message, model=None, max_turns=20):
            assert session_id == "s1"
            assert message == "做一个 PPT"
            assert model == "m1"
            assert max_turns == 3
            return {"stream_id": "stream-1"}

        def stream_events(self, stream_id):
            assert stream_id == "stream-1"
            yield "delta", {"content": "thinking"}
            yield "stream_end", {"ok": True}

        def fetch_session(self, session_id):
            assert session_id == "s1"
            return {
                "session": {"session_id": "s1", "title": "live", "model": "m1"},
                "messages": [
                    {"role": "user", "content": "做一个 PPT"},
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "skill_view",
                                    "arguments": json.dumps({"name": "ppt-master"}),
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call-1", "tool_name": "skill_view", "content": "{}"},
                    {"role": "assistant", "content": "完成"},
                ],
            }

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/evaluate",
        json={
            "base_url": "http://agent.example",
            "user_id": "u1",
            "token": "secret",
            "message": "做一个 PPT",
            "acceptance_criteria": "必须调用 ppt-master",
            "model": "m1",
            "max_turns": 3,
        },
    ) as response:
        assert response.status_code == 200
        events = _sse_events(response)

    event_names = [name for name, _payload in events]
    assert "run_started" in event_names
    assert "agent_event" in event_names
    assert "graph_update" in event_names
    assert "trace_update" in event_names
    assert "audit_update" in event_names
    assert "run_finished" in event_names

    finished = [payload for name, payload in events if name == "run_finished"][0]
    run_dir = settings.runs_dir / finished["run"]["name"]
    assert json.loads((run_dir / "expectations.json").read_text(encoding="utf-8")) == {
        "acceptance_criteria": "必须调用 ppt-master"
    }
    assert (run_dir / "audit.json").exists()
    graph_payload = [payload for name, payload in events if name == "graph_update"][-1]
    assert graph_payload["graph"]["nodes"][2]["label"] == "skill_view: ppt-master"
    trace_payload = [payload for name, payload in events if name == "trace_update"][-1]
    assert trace_payload["trace"]["metrics"]["skillCalls"] == 1
    assert trace_payload["trace"]["spans"][2]["name"] == "skill_view: ppt-master"


def test_live_evaluate_uploads_files_and_starts_skill(tmp_path, monkeypatch):
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.live.get_settings", lambda: settings)

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            assert base_url == "http://agent.example"
            assert user_id == "u1"

        def create_session(self, title="agentloop smoke test", model=""):
            assert title == "ppt job"
            return {"session": {"session_id": "s1"}}

        def upload_file(self, session_id, filename, content):
            assert session_id == "s1"
            assert filename == "paper.pdf"
            assert content == b"%PDF"
            return {"filename": "paper.pdf", "file_id": "file-1", "path": "/remote/paper.pdf", "size": 4}

        def start_chat(self, session_id, message, model=None, max_turns=20, attachments=None, skill=None):
            assert session_id == "s1"
            assert message == "请调用 ppt-master 生成 PPT"
            assert skill == "ppt-master"
            assert attachments == [{"filename": "paper.pdf", "file_id": "file-1", "path": "/remote/paper.pdf", "size": 4}]
            return {"stream_id": "stream-1"}

        def stream_events(self, stream_id):
            yield "stream_end", {"ok": True}

        def fetch_session(self, session_id):
            return {
                "session": {"session_id": "s1", "title": "ppt job", "model": ""},
                "messages": [{"role": "user", "content": "请调用 ppt-master 生成 PPT"}],
            }

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/evaluate-with-files",
        json={
            "base_url": "http://agent.example",
            "user_id": "u1",
            "title": "ppt job",
            "message": "请调用 ppt-master 生成 PPT",
            "skill": "ppt-master",
            "acceptance_criteria": "必须触发 ppt-master",
            "files": [{"filename": "paper.pdf", "content_base64": "JVBERg=="}],
        },
    ) as response:
        assert response.status_code == 200
        events = _sse_events(response)

    assert [payload for name, payload in events if name == "files_uploaded"][0]["files"][0]["file_id"] == "file-1"
    assert "run_finished" in [name for name, _payload in events]


def test_live_evaluate_marks_plain_confirmation_as_interaction(tmp_path, monkeypatch):
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.live.get_settings", lambda: settings)

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            pass

        def create_session(self, title="agentloop smoke test", model=""):
            return {"session": {"session_id": "s1"}}

        def start_chat(self, session_id, message, model=None, max_turns=20):
            return {"stream_id": "stream-1"}

        def stream_events(self, stream_id):
            yield "stream_end", {"ok": True}

        def fetch_session(self, session_id):
            return {
                "session": {"session_id": "s1", "title": "live", "model": ""},
                "messages": [
                    {"role": "user", "content": "生成 PPT"},
                    {"role": "assistant", "content": "请确认以上内容或提出修改。确认后我将继续生成。"},
                ],
            }

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/evaluate",
        json={"base_url": "http://agent.example", "user_id": "u1", "message": "生成 PPT"},
    ) as response:
        events = _sse_events(response)

    interaction = [payload for name, payload in events if name == "needs_interaction"][0]
    assert interaction["session_id"] == "s1"
    assert interaction["recommended_response"] == "确认"
    assert "请确认" in interaction["message"]


def test_live_evaluate_marks_continue_prompt_as_interaction(tmp_path, monkeypatch):
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.live.get_settings", lambda: settings)

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            pass

        def create_session(self, title="agentloop smoke test", model=""):
            return {"session": {"session_id": "s1"}}

        def start_chat(self, session_id, message, model=None, max_turns=20):
            return {"stream_id": "stream-1"}

        def stream_events(self, stream_id):
            yield "stream_end", {"ok": True}

        def fetch_session(self, session_id):
            return {
                "session": {"session_id": "s1", "title": "live", "model": ""},
                "messages": [
                    {"role": "user", "content": "生成 PPT"},
                    {
                        "role": "assistant",
                        "content": "由于系统调用次数限制，尚未执行最终的 PPT 生成指令。回复“继续”或“生成 PPT”即可自动完成最后的导出步骤。",
                    },
                ],
            }

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/evaluate",
        json={"base_url": "http://agent.example", "user_id": "u1", "message": "生成 PPT"},
    ) as response:
        events = _sse_events(response)

    interaction = [payload for name, payload in events if name == "needs_interaction"][0]
    assert interaction["session_id"] == "s1"
    assert interaction["recommended_response"] == "继续，补齐缺失文件并生成最终 PPT。"
    assert "回复“继续”" in interaction["message"]


def test_live_message_respond_continues_existing_session(tmp_path, monkeypatch):
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.live.get_settings", lambda: settings)
    calls = []

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            assert base_url == "http://agent.example"
            assert user_id == "u1"

        def start_chat(self, session_id, message, model=None, max_turns=20):
            calls.append((session_id, message, model, max_turns))
            return {"stream_id": "stream-2"}

        def stream_events(self, stream_id):
            assert stream_id == "stream-2"
            yield "stream_end", {"ok": True}

        def fetch_session(self, session_id):
            return {
                "session": {"session_id": session_id, "title": "live", "model": "m1"},
                "messages": [
                    {"role": "user", "content": "生成 PPT"},
                    {"role": "assistant", "content": "请确认以上内容。"},
                    {"role": "user", "content": "确认"},
                    {"role": "assistant", "content": "已生成 /tmp/demo.pptx"},
                ],
            }

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/message/respond",
        json={
            "base_url": "http://agent.example",
            "user_id": "u1",
            "session_id": "s1",
            "message": "确认",
            "model": "m1",
            "max_turns": 7,
            "acceptance_criteria": "必须生成 PPT",
        },
    ) as response:
        events = _sse_events(response)

    assert calls == [("s1", "确认", "m1", 7)]
    assert "run_started" in [name for name, _payload in events]
    assert "run_finished" in [name for name, _payload in events]
    assert not [payload for name, payload in events if name == "needs_interaction"]
    finished = [payload for name, payload in events if name == "run_finished"][0]
    run_dir = settings.runs_dir / finished["run"]["name"]
    assert json.loads((run_dir / "expectations.json").read_text(encoding="utf-8")) == {
        "acceptance_criteria": "必须生成 PPT"
    }


def test_live_proxy_clarify_and_approval(monkeypatch):
    calls = []

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            assert base_url == "http://agent.example"
            assert user_id == "u1"

        def respond_clarify(self, session_id, response):
            calls.append(("clarify", session_id, response))
            return {"ok": True}

        def respond_approval(self, session_id, choice, approval_id=None):
            calls.append(("approval", session_id, choice, approval_id))
            return {"ok": True}

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    clarify = client.post(
        "/api/live/clarify/respond",
        json={
            "base_url": "http://agent.example",
            "user_id": "u1",
            "session_id": "s1",
            "response": "继续使用推荐方案",
        },
    )
    approval = client.post(
        "/api/live/approval/respond",
        json={
            "base_url": "http://agent.example",
            "user_id": "u1",
            "session_id": "s1",
            "choice": "once",
            "approval_id": "approval-1",
        },
    )

    assert clarify.status_code == 200
    assert approval.status_code == 200
    assert calls == [
        ("clarify", "s1", "继续使用推荐方案"),
        ("approval", "s1", "once", "approval-1"),
    ]


def test_live_evaluate_detects_tool_call_limit_wording(tmp_path, monkeypatch):
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.live.get_settings", lambda: settings)

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            pass

        def create_session(self, title="agentloop smoke test", model=""):
            return {"session": {"session_id": "s1"}}

        def start_chat(self, session_id, message, model=None, max_turns=20):
            return {"stream_id": "stream-1"}

        def stream_events(self, stream_id):
            yield "stream_end", {"ok": True}

        def fetch_session(self, session_id):
            return {
                "session": {"session_id": "s1", "title": "live", "model": ""},
                "messages": [
                    {"role": "user", "content": "生成 PPT"},
                    {
                        "role": "assistant",
                        "content": "当前已达到工具调用次数上限。要继续完成PPT生成，请回复\"**继续**\"或\"**接着做**\"。",
                    },
                ],
            }

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/evaluate",
        json={"base_url": "http://agent.example", "user_id": "u1", "message": "生成 PPT"},
    ) as response:
        events = _sse_events(response)

    interaction = [payload for name, payload in events if name == "needs_interaction"][0]
    assert interaction["reason"] == "agent_paused_after_turn_limit"
    assert interaction["recommended_response"] == "继续，补齐缺失文件并生成最终 PPT。"
    assert not [name for name, _payload in events if name == "auto_interaction"]


def test_live_evaluate_auto_continues_confirmation(tmp_path, monkeypatch):
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.live.get_settings", lambda: settings)
    chats = []
    clarifications = []
    approvals = []

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            pass

        def create_session(self, title="agentloop smoke test", model=""):
            return {"session": {"session_id": "s1"}}

        def start_chat(self, session_id, message, model=None, max_turns=20):
            chats.append((session_id, message, max_turns))
            return {"stream_id": "stream-%s" % len(chats)}

        def stream_events(self, stream_id):
            if stream_id == "stream-1":
                yield "clarify", {"question": "请确认推荐方案。"}
                yield "approval", {"approval_id": "a1", "description": "允许写文件"}
                yield "stream_end", {"ok": True}
                return
            yield "stream_end", {"ok": True}

        def respond_clarify(self, session_id, response):
            clarifications.append((session_id, response))
            return {"ok": True}

        def respond_approval(self, session_id, choice, approval_id=None):
            approvals.append((session_id, choice, approval_id))
            return {"ok": True}

        def fetch_session(self, session_id):
            if len(chats) == 1:
                return {
                    "session": {"session_id": "s1", "title": "live", "model": ""},
                    "messages": [
                        {"role": "user", "content": "生成 PPT"},
                        {"role": "assistant", "content": "请确认以上内容或提出修改。确认后我将继续生成。"},
                    ],
                }
            return {
                "session": {"session_id": "s1", "title": "live", "model": ""},
                "messages": [
                    {"role": "user", "content": "生成 PPT"},
                    {"role": "assistant", "content": "请确认以上内容或提出修改。确认后我将继续生成。"},
                    {"role": "user", "content": "确认"},
                    {"role": "assistant", "content": "已生成 /tmp/demo.pptx"},
                ],
            }

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/evaluate",
        json={
            "base_url": "http://agent.example",
            "user_id": "u1",
            "message": "生成 PPT",
            "auto_continue": True,
            "auto_continue_limit": 3,
            "max_turns": 9,
        },
    ) as response:
        events = _sse_events(response)

    event_names = [name for name, _payload in events]
    assert chats == [
        ("s1", "生成 PPT", 9),
        ("s1", "确认", 9),
    ]
    assert clarifications == [("s1", "继续使用当前推荐方案。")]
    assert approvals == [("s1", "session", "a1")]
    assert event_names.count("auto_interaction") == 3
    assert "run_finished" in event_names
    assert not [payload for name, payload in events if name == "needs_interaction"]


def test_live_evaluate_auto_continue_stops_at_limit(tmp_path, monkeypatch):
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.live.get_settings", lambda: settings)
    chats = []

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            pass

        def create_session(self, title="agentloop smoke test", model=""):
            return {"session": {"session_id": "s1"}}

        def start_chat(self, session_id, message, model=None, max_turns=20):
            chats.append(message)
            return {"stream_id": "stream-%s" % len(chats)}

        def stream_events(self, stream_id):
            yield "stream_end", {"ok": True}

        def fetch_session(self, session_id):
            return {
                "session": {"session_id": "s1", "title": "live", "model": ""},
                "messages": [
                    {"role": "user", "content": "生成 PPT"},
                    {
                        "role": "assistant",
                        "content": "当前已达到工具调用次数上限。请回复\"**继续**\"。round-%s" % len(chats),
                    },
                ],
            }

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/evaluate",
        json={
            "base_url": "http://agent.example",
            "user_id": "u1",
            "message": "生成 PPT",
            "auto_continue": True,
            "auto_continue_limit": 1,
        },
    ) as response:
        events = _sse_events(response)

    assert chats == ["生成 PPT", "继续，补齐缺失文件并生成最终 PPT。"]
    interaction = [payload for name, payload in events if name == "needs_interaction"][0]
    assert interaction["recommended_response"] == "继续，补齐缺失文件并生成最终 PPT。"
    assert [payload for name, payload in events if name == "auto_interaction"][0]["count"] == 1
    assert "run_finished" in [name for name, _payload in events]


def test_live_evaluate_reports_failed_remote_stage(monkeypatch):
    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            pass

        def create_session(self, title="agentloop smoke test", model=""):
            return {"session": {"session_id": "s1"}}

        def start_chat(self, session_id, message, model=None, max_turns=20):
            raise AgentLoopHTTPError(502, "upstream unavailable")

    monkeypatch.setattr("backend.app.routes.live.AgentClient", FakeAgentClient)
    client = TestClient(app)

    with client.stream(
        "POST",
        "/api/live/evaluate",
        json={"base_url": "http://agent.example", "user_id": "u1", "message": "hi"},
    ) as response:
        events = _sse_events(response)

    error = [payload for name, payload in events if name == "error"][0]
    assert error["stage"] == "start_chat"
    assert "HTTP 502" in error["detail"]
