import asyncio
import json
import threading
import time

import pytest
import zstandard
from fastapi.testclient import TestClient

from auditrail.dsh_runtime import DshExecutionResult, DshRunRequest, DshRuntimeError, DshRuntimeEvent
from auditrail.runtime import build_runtime_graph, build_runtime_trace
from backend.app.config import Settings
from backend.app.main import app
from backend.app.routes.dsh_chat import (
    ActiveDshOperationRegistry,
    _chat_event_stream,
    _import_durable_run,
    _project_runtime_event,
    active_operations,
)


SESSION_ID = "session-12345678-1234-4234-8234-123456789abc"


def _settings(tmp_path):
    root = tmp_path / "allowed"
    workspace = root / "project"
    workspace.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        runs_dir=tmp_path / "runs",
        dsh_home=tmp_path / "dsh",
        dsh_runtime_enabled=True,
        dsh_binary=tmp_path / "bin" / "dsh",
        dsh_allowed_profiles=("headless",),
        dsh_allowed_workspace_roots=(root,),
        dsh_default_timeout_seconds=30,
        dsh_max_timeout_seconds=60,
        dsh_max_concurrency=2,
    )
    return settings, workspace.resolve()


def _write_durable_session(settings, workspace, session_id=SESSION_ID):
    events = [
        {
            "type": "session",
            "version": 0,
            "id": session_id,
            "createdAt": 1787000000000,
            "cwd": str(workspace),
            "agentPreset": "headless",
        },
        {
            "type": "user/message",
            "seq": 1,
            "time": 1787000001000,
            "data": {"role": "user", "id": "u1", "content": [{"type": "text", "text": "run tests"}]},
        },
        {
            "type": "tool/call",
            "seq": 2,
            "time": 1787000002000,
            "data": {"callId": "call-1", "name": "bash", "input": {"command": "pytest"}},
        },
        {
            "type": "assistant/message",
            "seq": 3,
            "time": 1787000003000,
            "data": {
                "message": {
                    "role": "assistant",
                    "id": "a1",
                    "content": [
                        {"type": "tool-call", "id": "call-1", "name": "bash", "arguments": {"command": "pytest"}}
                    ],
                }
            },
        },
        {
            "type": "tool/result",
            "seq": 4,
            "time": 1787000004000,
            "data": {
                "message": {
                    "role": "tool",
                    "id": "t1",
                    "content": [
                        {
                            "type": "tool-result",
                            "toolCallId": "call-1",
                            "content": [{"type": "text", "text": "1 passed"}],
                        }
                    ],
                }
            },
        },
        {
            "type": "assistant/message",
            "seq": 5,
            "time": 1787000005000,
            "data": {
                "message": {
                    "role": "assistant",
                    "id": "a2",
                    "content": [{"type": "text", "text": "done"}],
                }
            },
        },
    ]
    path = settings.dsh_home / "sessions" / "--allowed-project--" / session_id / "session.jsonl.zstd"
    path.parent.mkdir(parents=True)
    payload = "\n".join(json.dumps(event) for event in events).encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(payload))


def _sse_events(response):
    events = []
    current = None
    for line in response.text.splitlines():
        if line.startswith("event: "):
            current = line.removeprefix("event: ")
        elif line.startswith("data: ") and current:
            events.append((current, json.loads(line.removeprefix("data: "))))
            current = None
    return events


def _patch_settings(monkeypatch, settings):
    monkeypatch.setenv("AUDITRAIL_LLM_PROXY_LOG", str(settings.dsh_home / "missing-proxy.jsonl"))
    monkeypatch.setattr("backend.app.routes.dsh_chat.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)


def test_validation_error_is_sse_and_never_constructs_client(tmp_path, monkeypatch):
    settings, workspace = _settings(tmp_path)
    _patch_settings(monkeypatch, settings)
    constructed = []

    class UnexpectedClient:
        def __init__(self, config):
            constructed.append(config)

    monkeypatch.setattr("backend.app.routes.dsh_chat.DshHeadlessClient", UnexpectedClient)
    response = TestClient(app).post(
        "/api/dsh/chat",
        json={"message": "hello", "profile": "forbidden", "workspace": str(workspace)},
    )

    events = _sse_events(response)
    assert response.status_code == 200
    assert events[0][0] == "error"
    assert events[0][1]["stage"] == "validate"
    assert events[0][1]["code"] == "invalid_request"
    assert constructed == []


def test_non_object_body_is_sse_validation_error():
    response = TestClient(app).post("/api/dsh/chat", json=["not", "an", "object"])

    assert response.status_code == 200
    assert _sse_events(response) == [
        (
            "error",
            {
                "code": "invalid_request",
                "detail": "DSH request body must be an object",
                "stage": "validate",
            },
        )
    ]


def test_malformed_json_is_sse_validation_error():
    response = TestClient(app).post(
        "/api/dsh/chat",
        content="{",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200
    assert _sse_events(response)[0][1]["stage"] == "validate"


def test_runtime_projection_replaces_streamed_text_and_preserves_tool_error_status():
    messages = [{"role": "user", "content": "run"}]
    _project_runtime_event(messages, DshRuntimeEvent("text", {"text": "partial"}))
    _project_runtime_event(messages, DshRuntimeEvent("text", {"text": " output"}))
    _project_runtime_event(messages, DshRuntimeEvent("final", {"text": "complete answer"}))

    assistant_messages = [message for message in messages if message.get("role") == "assistant"]
    assert assistant_messages == [{"role": "assistant", "content": "complete answer"}]

    _project_runtime_event(
        messages,
        DshRuntimeEvent("tool_call", {"call_id": "call-1", "tool": "bash", "input": {"command": "false"}}),
    )
    _project_runtime_event(
        messages,
        DshRuntimeEvent("tool_result", {"call_id": "call-1", "status": "error", "result": "command exited 1"}),
    )
    graph = build_runtime_graph(messages)
    trace = build_runtime_trace(messages)

    assert next(node for node in graph["nodes"] if node["type"] == "tool")["status"] == "error"
    assert next(node for node in graph["nodes"] if node["type"] == "tool_result")["status"] == "error"
    assert any(span["status"] == "failed" for span in trace["spans"])


def test_resume_rejects_different_durable_workspace_without_spawning(tmp_path, monkeypatch):
    settings, workspace = _settings(tmp_path)
    other_workspace = workspace.parent / "other"
    other_workspace.mkdir()
    _write_durable_session(settings, workspace)
    _patch_settings(monkeypatch, settings)
    constructed = []

    class UnexpectedClient:
        def __init__(self, config):
            constructed.append(config)

    monkeypatch.setattr("backend.app.routes.dsh_chat.DshHeadlessClient", UnexpectedClient)
    events = _sse_events(
        TestClient(app).post(
            "/api/dsh/chat",
            json={
                "message": "continue",
                "profile": "headless",
                "workspace": str(other_workspace),
                "session_id": SESSION_ID,
            },
        )
    )

    assert events == [
        (
            "error",
            {
                "code": "invalid_request",
                "detail": "DSH session belongs to a different workspace",
                "stage": "validate",
            },
        )
    ]
    assert constructed == []


def test_success_streams_projections_and_imports_authoritative_run(tmp_path, monkeypatch):
    settings, workspace = _settings(tmp_path)
    _write_durable_session(settings, workspace)
    _patch_settings(monkeypatch, settings)

    class FakeClient:
        def __init__(self, config):
            assert config.allowed_profiles == ("headless",)

        def run(self, request, *, cancel_event=None, on_event=None):
            assert request.workspace == workspace
            events = (
                DshRuntimeEvent("session", {"session_id": SESSION_ID, "cwd": str(workspace)}),
                DshRuntimeEvent("tool_call", {"call_id": "call-1", "tool": "bash", "input": {"command": "pytest"}}),
                DshRuntimeEvent("tool_result", {"call_id": "call-1", "status": "completed", "result": "1 passed"}),
                DshRuntimeEvent("final", {"text": "done"}),
            )
            for event in events:
                on_event(event)
            return DshExecutionResult(
                session_id=SESSION_ID,
                final_text="done",
                turn_end_reason="completed",
                exit_code=0,
                timed_out=False,
                cancelled=False,
                events=events,
            )

    monkeypatch.setattr("backend.app.routes.dsh_chat.DshHeadlessClient", FakeClient)
    response = TestClient(app).post(
        "/api/dsh/chat",
        json={
            "message": "run tests",
            "profile": "headless",
            "workspace": str(workspace),
            "acceptance_criteria": "tests pass",
            "timeout": 45,
        },
    )

    events = _sse_events(response)
    names = [name for name, _ in events]
    assert names[0] == "run_started"
    assert names.count("agent_event") == 4
    assert "graph_update" in names
    assert "trace_update" in names
    assert names[-2:] == ["audit_update", "run_finished"]
    assert any(
        node["type"] == "tool"
        for name, payload in events
        if name == "graph_update"
        for node in payload["graph"]["nodes"]
    )

    finished = events[-1][1]
    assert finished["session_id"] == SESSION_ID
    assert finished["summary"]["session_id"] == SESSION_ID
    run_dir = settings.runs_dir / finished["run_id"]
    expected_files = {
        "session.json",
        "summary.json",
        "trajectory.jsonl",
        "audit.json",
        "tool_audit.md",
        "source.json",
        "expectations.json",
    }
    assert expected_files <= {path.name for path in run_dir.iterdir()}
    assert json.loads((run_dir / "expectations.json").read_text())["acceptance_criteria"] == "tests pass"
    source = json.loads((run_dir / "source.json").read_text())
    assert source["source_type"] == "dsh"
    assert source["interaction_mode"] == "auditrail-chat"
    assert source["dsh_profile"] == "headless"
    assert source["session_role"] == "evaluand"
    trajectory = [json.loads(line) for line in (run_dir / "trajectory.jsonl").read_text().splitlines()]
    assert trajectory[-1]["content"] == "done"
    assert active_operations.get(finished["operation_id"]) is None

    second_response = TestClient(app).post(
        "/api/dsh/chat",
        json={
            "message": "run tests again",
            "profile": "headless",
            "workspace": str(workspace),
            "session_id": SESSION_ID,
            "acceptance_criteria": "tests still pass",
        },
    )
    second_finished = _sse_events(second_response)[-1][1]
    assert second_finished["run_id"] != finished["run_id"]
    assert (settings.runs_dir / finished["run_id"] / "session.json").exists()
    assert (settings.runs_dir / second_finished["run_id"] / "session.json").exists()


def test_import_failure_preserves_session_and_never_finishes(tmp_path, monkeypatch):
    settings, workspace = _settings(tmp_path)
    _patch_settings(monkeypatch, settings)

    class FakeClient:
        def __init__(self, config):
            pass

        def run(self, request, *, cancel_event=None, on_event=None):
            event = DshRuntimeEvent("session", {"session_id": SESSION_ID, "cwd": str(workspace)})
            on_event(event)
            return DshExecutionResult(
                session_id=SESSION_ID,
                final_text="done",
                turn_end_reason="completed",
                exit_code=0,
                timed_out=False,
                cancelled=False,
                events=(event,),
            )

    monkeypatch.setattr("backend.app.routes.dsh_chat.DshHeadlessClient", FakeClient)
    events = _sse_events(
        TestClient(app).post(
            "/api/dsh/chat",
            json={"message": "run", "profile": "headless", "workspace": str(workspace)},
        )
    )

    assert events[-1][0] == "error"
    assert events[-1][1]["stage"] == "import"
    assert events[-1][1]["session_id"] == SESSION_ID
    assert "run_finished" not in [name for name, _ in events]


def test_execution_failure_never_imports_or_finishes(tmp_path, monkeypatch):
    settings, workspace = _settings(tmp_path)
    _patch_settings(monkeypatch, settings)

    class FakeClient:
        def __init__(self, config):
            pass

        def run(self, request, *, cancel_event=None, on_event=None):
            return DshExecutionResult(
                session_id=None,
                final_text=None,
                turn_end_reason=None,
                exit_code=1,
                timed_out=False,
                cancelled=False,
                events=(),
                error=DshRuntimeError("process_failed", "DSH process failed"),
            )

    monkeypatch.setattr("backend.app.routes.dsh_chat.DshHeadlessClient", FakeClient)
    events = _sse_events(
        TestClient(app).post(
            "/api/dsh/chat",
            json={"message": "run", "profile": "headless", "workspace": str(workspace)},
        )
    )

    assert events[-1] == (
        "error",
        {
            "code": "process_failed",
            "detail": "DSH process failed",
            "stage": "execute",
            "operation_id": events[0][1]["operation_id"],
            "cancelled": False,
            "timed_out": False,
        },
    )
    assert not settings.runs_dir.exists()


@pytest.mark.parametrize(
    ("cancelled", "timed_out", "code"),
    [(True, False, "cancelled"), (False, True, "timeout")],
)
def test_cancelled_or_timed_out_execution_never_imports(tmp_path, monkeypatch, cancelled, timed_out, code):
    settings, workspace = _settings(tmp_path)
    _patch_settings(monkeypatch, settings)

    class FakeClient:
        def __init__(self, config):
            pass

        def run(self, request, *, cancel_event=None, on_event=None):
            return DshExecutionResult(
                session_id=SESSION_ID,
                final_text=None,
                turn_end_reason=None,
                exit_code=None,
                timed_out=timed_out,
                cancelled=cancelled,
                events=(),
                error=DshRuntimeError(code, "DSH execution did not complete"),
            )

    monkeypatch.setattr("backend.app.routes.dsh_chat.DshHeadlessClient", FakeClient)
    events = _sse_events(
        TestClient(app).post(
            "/api/dsh/chat",
            json={"message": "run", "profile": "headless", "workspace": str(workspace)},
        )
    )

    assert events[-1][0] == "error"
    assert events[-1][1]["cancelled"] is cancelled
    assert events[-1][1]["timed_out"] is timed_out
    assert "run_finished" not in [name for name, _ in events]
    assert not settings.runs_dir.exists()


def test_durable_import_runs_off_event_loop(tmp_path, monkeypatch):
    settings, workspace = _settings(tmp_path)
    _patch_settings(monkeypatch, settings)
    import_started = threading.Event()
    release_import = threading.Event()

    class FakeClient:
        def __init__(self, config):
            pass

        def run(self, request, *, cancel_event=None, on_event=None):
            return DshExecutionResult(
                session_id=SESSION_ID,
                final_text="done",
                turn_end_reason="completed",
                exit_code=0,
                timed_out=False,
                cancelled=False,
                events=(),
            )

    def blocking_import(*args):
        import_started.set()
        release_import.wait(1)
        return {
            "run_dir": settings.runs_dir / "published-run",
            "graph": {},
            "trace": {},
            "audit": {},
            "summary": {},
            "run": {},
            "batch_summary": {},
        }

    monkeypatch.setattr("backend.app.routes.dsh_chat.DshHeadlessClient", FakeClient)
    monkeypatch.setattr("backend.app.routes.dsh_chat._import_durable_run", blocking_import)

    async def consume():
        stream = _chat_event_stream({"message": "run", "profile": "headless", "workspace": str(workspace)})
        await anext(stream)
        pending = asyncio.create_task(anext(stream))
        deadline = asyncio.get_running_loop().time() + 1
        while not import_started.is_set() and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.001)
        assert import_started.is_set()
        marker = asyncio.Event()
        asyncio.get_running_loop().call_soon(marker.set)
        await asyncio.wait_for(marker.wait(), timeout=0.1)
        release_import.set()
        projected = await pending
        await stream.aclose()
        return projected

    assert asyncio.run(consume()).startswith("event: graph_update")


def test_import_failure_after_writes_leaves_no_visible_run(tmp_path, monkeypatch):
    settings, workspace = _settings(tmp_path)
    _write_durable_session(settings, workspace)
    _patch_settings(monkeypatch, settings)

    def fail_expectations(run_dir, acceptance_criteria):
        raise RuntimeError("expectations failed")

    monkeypatch.setattr("backend.app.routes.dsh_chat.save_expectations", fail_expectations)
    request = DshRunRequest(message="run", profile="headless", workspace=workspace)

    with pytest.raises(RuntimeError, match="expectations failed"):
        _import_durable_run(settings, request, SESSION_ID, "tests pass")

    assert settings.runs_dir.exists()
    assert list(settings.runs_dir.iterdir()) == []


def test_disconnect_cancels_worker_and_cleans_registry(tmp_path, monkeypatch):
    settings, workspace = _settings(tmp_path)
    _patch_settings(monkeypatch, settings)
    stopped = threading.Event()

    class BlockingClient:
        def __init__(self, config):
            pass

        def run(self, request, *, cancel_event=None, on_event=None):
            cancel_event.wait(2)
            stopped.set()
            return DshExecutionResult(
                session_id=None,
                final_text=None,
                turn_end_reason=None,
                exit_code=None,
                timed_out=False,
                cancelled=True,
                events=(),
                error=DshRuntimeError("cancelled", "DSH execution was cancelled"),
            )

    monkeypatch.setattr("backend.app.routes.dsh_chat.DshHeadlessClient", BlockingClient)

    async def consume():
        stream = _chat_event_stream(
            {"message": "run", "profile": "headless", "workspace": str(workspace)},
        )
        first = await anext(stream)
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0.01)
        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled stream continued")
        return _sse_events(type("Response", (), {"text": first})())

    events = asyncio.run(consume())
    assert events[0][0] == "run_started"
    operation_id = events[0][1]["operation_id"]
    assert stopped.is_set()
    assert active_operations.get(operation_id) is None


def test_registry_enforces_capacity_session_ownership_and_exact_cancel(tmp_path):
    registry = ActiveDshOperationRegistry()
    first = registry.register(workspace=tmp_path, session_id=SESSION_ID, max_concurrency=2)
    second = registry.register(workspace=tmp_path, session_id="session-other", max_concurrency=2)

    try:
        registry.register(workspace=tmp_path, session_id=SESSION_ID, max_concurrency=3)
    except RuntimeError as exc:
        assert getattr(exc, "code") == "session_busy"
    else:
        raise AssertionError("busy session was accepted")

    try:
        registry.register(workspace=tmp_path, session_id="session-third", max_concurrency=2)
    except RuntimeError as exc:
        assert getattr(exc, "code") == "runtime_busy"
    else:
        raise AssertionError("registry capacity was exceeded")

    assert registry.cancel(first.operation_id) is first
    assert first.cancel_event.is_set()
    assert not second.cancel_event.is_set()
    registry.finish(first.operation_id)
    replacement = registry.register(workspace=tmp_path, session_id=SESSION_ID, max_concurrency=2)
    assert replacement.session_id == SESSION_ID
    registry.cancel_all()
    assert registry.get(second.operation_id) is None
    assert second.cancel_event.is_set()


def test_cancel_route_targets_registered_operation(tmp_path):
    operation = active_operations.register(workspace=tmp_path, session_id=SESSION_ID, max_concurrency=2)
    try:
        response = TestClient(app).post("/api/dsh/chat/%s/cancel" % operation.operation_id)
        assert response.status_code == 200
        assert response.json()["operation_id"] == operation.operation_id
        assert operation.cancel_event.is_set()
        assert TestClient(app).post("/api/dsh/chat/missing/cancel").status_code == 404
    finally:
        active_operations.finish(operation.operation_id)


def test_shutdown_retains_ownership_until_slow_worker_settles(tmp_path):
    registry = ActiveDshOperationRegistry()
    operation = registry.register(workspace=tmp_path, session_id=SESSION_ID, max_concurrency=2)
    release = threading.Event()

    worker = threading.Thread(target=lambda: release.wait(1))
    registry.attach_worker(operation.operation_id, worker)
    worker.start()
    registry.cancel_all(join_timeout=0.01)

    assert registry.get(operation.operation_id) is operation
    with pytest.raises(RuntimeError) as exc_info:
        registry.register(workspace=tmp_path, session_id=SESSION_ID, max_concurrency=2)
    assert getattr(exc_info.value, "code") == "session_busy"

    release.set()
    deadline = time.monotonic() + 1
    while registry.get(operation.operation_id) is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert registry.get(operation.operation_id) is None


def test_shutdown_cancels_and_joins_owned_worker(tmp_path):
    registry = ActiveDshOperationRegistry()
    operation = registry.register(workspace=tmp_path, session_id=None, max_concurrency=1)
    stopped = threading.Event()

    def run_until_cancelled():
        operation.cancel_event.wait(1)
        stopped.set()

    worker = threading.Thread(target=run_until_cancelled)
    registry.attach_worker(operation.operation_id, worker)
    worker.start()
    registry.cancel_all(join_timeout=1)

    assert stopped.is_set()
    assert not worker.is_alive()
    assert registry.get(operation.operation_id) is None
