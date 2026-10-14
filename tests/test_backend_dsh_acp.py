"""Deterministic backend tests for the ACP runtime manager and its HTTP/SSE surface.

Two layers are covered:

- the manager with injected fakes, for registry, import and permission policy;
- the route with the real ``AcpTransport``/``AcpClient`` over the scripted
  in-process peer from ``test_dsh_acp``, so framing, JSON-RPC correlation,
  permission blocking, SSE projection and durable import are exercised together.

Nothing here spawns DSH, opens a socket, or needs a model credential.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from pathlib import Path

import pytest
import zstandard
from fastapi.testclient import TestClient

from auditrail.dsh_acp import (
    ACP_ALLOW_ONCE_OPTION_ID,
    ACP_REJECT_ONCE_OPTION_ID,
    AcpClient,
    AcpInitializeResult,
    AcpPermissionRequest,
    AcpProcess,
    AcpPromptOutcome,
    AcpSession,
    AcpTransport,
    AgentMethod,
    ClientMethod,
    SessionUpdateKind,
)
from auditrail.dsh_runtime import DshRuntimeConfig, DshRuntimeEvent
from backend.app.config import Settings
from backend.app.main import app
from backend.app.routes import dsh_acp as dsh_acp_route
from backend.app.runtime_manager import AcpManagerError, AcpRuntimeManager
from test_dsh_acp import (
    SESSION_ID,
    SESSION_ID_TWO,
    ScriptedAcpPeer,
    initialize_responder,
    wait_for,
)

WORKSPACE_SLUG = "--private-tmp-acp-workspace--"


# --- durable recording fixture ----------------------------------------------


def _durable_events(session_id, workspace):
    """A small but realistic v4 ACP recording for one session."""
    return [
        {
            "type": "session",
            "version": 4,
            "id": session_id,
            "createdAt": 1787000000000,
            "cwd": str(workspace),
            "agentPreset": "acp",
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


def _write_durable_recording(
    dsh_home,
    workspace,
    session_id=SESSION_ID,
    *,
    header_id=None,
    header_cwd=None,
    filename="session.v4.jsonl.zstd",
):
    """Write one compressed ACP recording; callers may corrupt the header."""
    events = _durable_events(session_id, workspace)
    events[0]["id"] = header_id if header_id is not None else session_id
    events[0]["cwd"] = str(header_cwd if header_cwd is not None else workspace)
    path = Path(dsh_home) / "sessions" / WORKSPACE_SLUG / session_id / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = "\n".join(json.dumps(event) for event in events).encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(payload))
    return path


# --- injected fakes ---------------------------------------------------------


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.dead = False

    def poll(self):
        return 1 if self.dead else None

    def close_stdin(self):
        self.dead = True

    def terminate(self, force=False):
        self.dead = True


class FakeTransport:
    def __init__(self, process):
        self.process = process

    def start(self):
        return self

    def close(self, **kwargs):
        self.process.dead = True


class FakeClient:
    """Records every manager decision without touching a transport."""

    def __init__(self, transport, config, profile="acp", on_permission=None):
        self.transport = transport
        self.on_permission = on_permission
        self.sessions: dict[str, Path] = {}
        self.prompt_calls: list[tuple[str, str]] = []
        self.resume_calls: list[tuple[str, Path]] = []
        self.cancelled: list[str] = []
        self.closed: list[str] = []
        self.cancelled_permissions: list[object] = []
        self.responded_permissions: list[tuple[object, str]] = []
        self.pending: tuple[AcpPermissionRequest, ...] = ()
        self.release_prompt: threading.Event | None = None
        self.initialize_result = AcpInitializeResult(
            protocol_version=1,
            agent_name="deepseek-harness-acp",
            session_capabilities={"close": {}, "list": {}, "resume": {}},
        )

    def initialize(self, timeout_seconds=None):
        return self.initialize_result

    def new_session(self, workspace, timeout_seconds=None):
        session_id = str(uuid.uuid4())
        self.sessions[session_id] = Path(workspace)
        return AcpSession(session_id, Path(workspace))

    def resume_session(self, session_id, workspace, timeout_seconds=None):
        self.resume_calls.append((session_id, Path(workspace)))
        self.sessions[session_id] = Path(workspace)
        return AcpSession(session_id, Path(workspace))

    def prompt(self, session_id, text, timeout_seconds=None, cancel_event=None, on_event=None):
        self.prompt_calls.append((session_id, text))
        if self.release_prompt is not None:
            self.release_prompt.wait(2)
        if cancel_event is not None and cancel_event.is_set():
            outcome = AcpPromptOutcome(
                session_id,
                "cancelled",
                True,
                updates=(DshRuntimeEvent("final", {"text": "", "stop_reason": "cancelled"}),),
            )
        else:
            outcome = AcpPromptOutcome(
                session_id,
                "end_turn",
                False,
                updates=(DshRuntimeEvent("text", {"text": text}),),
                text=text,
            )
        # Mirror the real client: the observer sees every event of the turn.
        if on_event is not None:
            for event in outcome.updates:
                on_event(event)
        return outcome

    def take_notifications(self, timeout=None):
        return ()

    def pending_permissions(self):
        return self.pending

    def cancel(self, session_id):
        self.cancelled.append(session_id)

    def cancel_permission(self, request_id):
        self.cancelled_permissions.append(request_id)
        return True

    def respond_permission(self, request_id, option_id):
        self.responded_permissions.append((request_id, option_id))
        return True

    def close_session(self, session_id, timeout_seconds=None):
        self.closed.append(session_id)
        self.sessions.pop(session_id, None)


def _manager(tmp_path, **overrides):
    """A manager with injected fakes, a runs dir and an isolated DSH home."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    binary = tmp_path / "dsh"
    binary.write_text("fake")
    binary.chmod(0o755)
    config = DshRuntimeConfig(
        binary=binary,
        allowed_profiles=("acp",),
        allowed_workspace_roots=(tmp_path,),
        default_timeout_seconds=2,
        max_timeout_seconds=3,
    )
    options = {
        "process_factory": FakeProcess,
        "transport_factory": FakeTransport,
        "client_factory": FakeClient,
        "run_root": tmp_path / "runs",
        "dsh_home": tmp_path / "dsh-home",
        "max_concurrency": 4,
    }
    options.update(overrides)
    return AcpRuntimeManager(config, **options), workspace.resolve()


def _settings(tmp_path, **overrides):
    values = {
        "runs_dir": tmp_path / "runs",
        "dsh_home": tmp_path / "dsh-home",
        "dsh_runtime_enabled": True,
        "dsh_binary": tmp_path / "dsh",
        "dsh_allowed_profiles": ("acp",),
        "dsh_allowed_workspace_roots": (tmp_path,),
        "dsh_default_timeout_seconds": 5,
        "dsh_max_timeout_seconds": 5,
        "dsh_max_concurrency": 2,
        # ACP is opt-in, so the ACP tests must opt in explicitly; leaving this
        # out of the defaults would silently test the disabled path.
        "dsh_acp_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)


def _pending_permission(session_id=SESSION_ID, request_id="perm-1"):
    return AcpPermissionRequest(
        request_id=request_id,
        session_id=session_id,
        tool_call_id="call-1",
        options=(
            {"optionId": ACP_ALLOW_ONCE_OPTION_ID, "name": "Allow once", "kind": "allow_once"},
            {"optionId": ACP_REJECT_ONCE_OPTION_ID, "name": "Reject", "kind": "reject_once"},
        ),
    )


@pytest.fixture
def injected_manager():
    """Install a manager for the route module and always clear it again."""
    yield dsh_acp_route.set_acp_runtime_manager
    dsh_acp_route.set_acp_runtime_manager(None)


@pytest.fixture(autouse=True)
def _no_configured_proxy_log(monkeypatch, tmp_path):
    """Keep run writes offline: the real ``.env`` can point at a remote log."""
    monkeypatch.setenv("AUDITRAIL_LLM_PROXY_LOG", str(tmp_path / "missing-proxy-llm-calls.jsonl"))


# --- manager: settlement, durable import ------------------------------------


def test_acp_manager_completes_and_imports_durable_run(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)

    operation = manager.prompt(session.session_id, "run tests", acceptance_criteria="tests pass")
    assert operation.done.wait(5)

    events = [operation.queue.get(timeout=1)["event"] for _ in range(4)]
    assert events == ["started", "runtime_event", "completed", "run_imported"]
    assert operation.state == "completed"
    assert operation.error is None
    assert operation.run_id is not None

    run_dir = tmp_path / "runs" / operation.run_id
    assert {
        "session.json",
        "summary.json",
        "trajectory.jsonl",
        "audit.json",
        "tool_audit.md",
        "source.json",
        "expectations.json",
    } <= {path.name for path in run_dir.iterdir()}
    source = json.loads((run_dir / "source.json").read_text())
    assert source["source_type"] == "dsh"
    assert source["interaction_mode"] == "auditrail-chat"
    assert source["dsh_profile"] == "acp"
    assert source["session_role"] == "evaluand"
    assert source["dsh_session_id"] == session.session_id
    assert json.loads((run_dir / "expectations.json").read_text())["acceptance_criteria"] == "tests pass"
    trajectory = [json.loads(line) for line in (run_dir / "trajectory.jsonl").read_text().splitlines()]
    assert trajectory[-1]["content"] == "done"
    assert [path.name for path in (tmp_path / "runs").iterdir()] == [operation.run_id]


def test_acp_manager_reads_the_version_zero_generation_too(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(
        tmp_path / "dsh-home",
        workspace,
        session.session_id,
        filename="session.jsonl.zstd",
    )

    operation = manager.prompt(session.session_id, "run tests")
    assert operation.done.wait(5)

    assert operation.state == "completed"
    assert operation.run_id is not None


@pytest.mark.parametrize(
    "corruption",
    ["missing", "session_id_mismatch", "workspace_mismatch", "unreadable"],
)
def test_acp_manager_reports_import_failure_without_claiming_success(tmp_path, corruption):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    dsh_home = tmp_path / "dsh-home"
    if corruption == "session_id_mismatch":
        _write_durable_recording(dsh_home, workspace, session.session_id, header_id=SESSION_ID_TWO)
    elif corruption == "workspace_mismatch":
        _write_durable_recording(dsh_home, workspace, session.session_id, header_cwd=tmp_path)
    elif corruption == "unreadable":
        path = _write_durable_recording(dsh_home, workspace, session.session_id)
        path.write_bytes(b"not zstandard")

    operation = manager.prompt(session.session_id, "run tests")
    assert operation.done.wait(5)

    assert operation.state == "failed"
    assert operation.error is not None
    assert operation.error["code"] == "import_failed"
    assert operation.error["stage"] == "import"
    assert operation.error["session_id"] == session.session_id
    assert operation.run_id is None
    kinds = []
    while not operation.queue.empty():
        kinds.append(operation.queue.get(timeout=1)["event"])
    assert kinds == ["started", "runtime_event", "completed", "error"]
    assert "run_imported" not in kinds
    assert not (tmp_path / "runs").exists() or list((tmp_path / "runs").iterdir()) == []


def test_acp_manager_keeps_runs_from_repeated_prompts_on_one_session(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)

    first = manager.prompt(session.session_id, "first")
    assert first.done.wait(5)
    second = manager.prompt(session.session_id, "second")
    assert second.done.wait(5)

    assert first.run_id != second.run_id
    published = sorted(path.name for path in (tmp_path / "runs").iterdir())
    assert published == sorted([first.run_id, second.run_id])


# --- manager: concurrency, registry bounds ----------------------------------


def test_acp_manager_rejects_second_prompt_until_first_settles(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    original = FakeClient.prompt
    started = threading.Event()

    def slow_prompt(self, *args, **kwargs):
        started.set()
        return original(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(FakeClient, "prompt", slow_prompt)
        first = None
        try:
            first = manager.prompt(session.session_id, "one")
            with pytest.raises(AcpManagerError) as exc_info:
                manager.prompt(session.session_id, "two")
            assert exc_info.value.code == "prompt_in_flight"
        finally:
            if first is not None:
                assert first.done.wait(5)
    assert started.is_set()


def test_acp_manager_bounds_settled_operations(tmp_path, monkeypatch):
    import backend.app.runtime_manager as runtime_manager

    retention = 2
    monkeypatch.setattr(runtime_manager, "MAX_RETAINED_OPERATIONS", retention)
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)

    # Pruning happens when the next prompt is registered, so it takes one extra
    # settled operation past the bound before the oldest one is dropped.
    operation_ids = []
    for _ in range(retention + 2):
        operation = manager.prompt(session.session_id, "turn")
        assert operation.done.wait(5)
        operation_ids.append(operation.operation_id)

    with pytest.raises(AcpManagerError):
        manager.get_operation(operation_ids[0])
    for operation_id in operation_ids[1:]:
        assert manager.get_operation(operation_id).done.is_set()


def test_operation_queue_applies_backpressure_but_keeps_terminal_events(monkeypatch):
    """A stalled SSE reader must not grow the queue without limit.

    Progress events are dropped once the buffer is nearly full, while the
    low-volume events the SSE contract depends on are always queued.
    """
    import backend.app.runtime_manager as runtime_manager

    monkeypatch.setattr(runtime_manager, "MAX_OPERATION_QUEUE", 8)
    monkeypatch.setattr(runtime_manager, "_TERMINAL_QUEUE_RESERVE", 2)
    operation = runtime_manager.AcpOperation("op-1", SESSION_ID, "prompt")

    for index in range(50):
        operation.emit("runtime_event", {"index": index})
    assert operation.dropped_events > 0
    assert operation.queue.qsize() == 6

    # The reserved headroom means the terminal event still lands.
    operation.emit("completed", {"stop_reason": "end_turn"})
    drained = []
    while not operation.queue.empty():
        drained.append(operation.queue.get_nowait()["event"])
    assert drained[-1] == "completed"
    assert drained.count("runtime_event") == 6


def test_operation_queue_never_drops_contract_events(monkeypatch):
    import backend.app.runtime_manager as runtime_manager

    monkeypatch.setattr(runtime_manager, "MAX_OPERATION_QUEUE", 4)
    monkeypatch.setattr(runtime_manager, "_TERMINAL_QUEUE_RESERVE", 1)
    operation = runtime_manager.AcpOperation("op-2", SESSION_ID, "prompt")

    for _ in range(3):
        operation.emit("started", {"operation_id": "op-2"})
        operation.emit("permission_requested", {"request_id": "p"})
        operation.emit("error", {"code": "boom"})
        operation.emit("run_imported", {"run_id": "run"})
    assert operation.dropped_events == 0
    assert operation.queue.qsize() == 12


def test_acp_manager_completed_event_reports_dropped_progress(tmp_path):
    """The settled event always reports how much progress was shed."""
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)

    operation = manager.prompt(session.session_id, "turn")
    assert operation.done.wait(5)

    events = []
    while not operation.queue.empty():
        events.append(operation.queue.get_nowait())
    completed = [item for item in events if item["event"] == "completed"]
    assert len(completed) == 1
    # The field is always present so a client can distinguish "0 dropped" from
    # "this server does not report drops".
    assert completed[0]["data"]["dropped_events"] == 0


def test_acp_manager_capabilities_are_negotiated_not_guessed(tmp_path):
    manager, workspace = _manager(tmp_path)
    manager.create_session(workspace)

    capabilities = manager.capabilities()

    assert capabilities["protocol_version"] == 1
    assert capabilities["supports_images"] is False
    assert capabilities["supports_session_close"] is True
    assert capabilities["permission_options"] == [ACP_ALLOW_ONCE_OPTION_ID, ACP_REJECT_ONCE_OPTION_ID]


def test_acp_manager_restarts_process_and_resumes_sessions(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    first_process = manager.process
    first_process.dead = True

    operation = manager.prompt(session.session_id, "after crash")
    assert operation.done.wait(5)

    assert operation.state == "completed"
    assert manager.process is not first_process
    assert session.state == "ready"
    assert session.client.resume_calls == [(session.session_id, workspace)]


def test_acp_manager_reports_resume_failure_instead_of_running_the_turn(tmp_path, monkeypatch):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    manager.process.dead = True

    def refuse_resume(self, session_id, workspace, timeout_seconds=None):
        raise AcpError("unknown_session", "ACP session is not open on this connection")

    monkeypatch.setattr(FakeClient, "resume_session", refuse_resume)
    operation = manager.prompt(session.session_id, "after crash")
    assert operation.done.wait(5)

    assert operation.state == "failed"
    assert operation.error == {
        "code": "resume_failed",
        "detail": "ACP session could not be resumed",
        "session_id": session.session_id,
    }
    assert session.state == "error"
    assert not (tmp_path / "runs").exists() or list((tmp_path / "runs").iterdir()) == []


def test_acp_manager_rejects_workspace_outside_policy(tmp_path):
    manager, workspace = _manager(tmp_path)

    with pytest.raises(AcpManagerError) as exc_info:
        manager.create_session(tmp_path.parent)

    assert exc_info.value.code == "invalid_workspace"


def test_acp_manager_rejects_an_oversize_message_precisely(tmp_path):
    manager, peer, workspace = _real_transport_manager(tmp_path, initialize_responder())
    try:
        session = manager.create_session(workspace)
        operation = manager.prompt(session.session_id, "x" * (manager.config.max_message_bytes + 1))
        assert operation.done.wait(5)
    finally:
        peer.close()

    assert operation.state == "failed"
    assert operation.error == {
        "code": "invalid_message",
        "detail": "DSH message exceeds the configured limit",
        "session_id": session.session_id,
    }
    assert not (tmp_path / "runs").exists() or list((tmp_path / "runs").iterdir()) == []


# --- manager: permissions ---------------------------------------------------


def test_acp_manager_pushes_permission_requests_to_the_running_operation(tmp_path):
    manager, workspace = _manager(tmp_path)
    first = manager.create_session(workspace)
    second = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, first.session_id)
    first.client.release_prompt = threading.Event()
    operation = manager.prompt(first.session_id, "go")
    try:
        assert wait_for(lambda: first.client.prompt_calls)
        while not operation.queue.empty():
            operation.queue.get(timeout=1)

        # The owning session's request reaches the stream...
        first.client.on_permission(_pending_permission(first.session_id, request_id="perm-1"))
        pushed = operation.queue.get(timeout=2)
        assert pushed["event"] == "permission_requested"
        assert pushed["data"]["operation_id"] == operation.operation_id
        assert pushed["data"]["request_id"] == "perm-1"

        # ...while a request for a session with no prompt in flight is dropped.
        first.client.on_permission(_pending_permission(second.session_id, request_id="perm-2"))
        assert operation.queue.qsize() == 0
    finally:
        first.client.release_prompt.set()
        assert operation.done.wait(5)

    assert operation.state == "completed"
    while not operation.queue.empty():
        operation.queue.get(timeout=1)
    first.client.on_permission(_pending_permission(first.session_id, request_id="perm-3"))
    assert operation.queue.qsize() == 0


def test_acp_manager_answers_permission_for_the_owning_session_only(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    operation = manager.prompt(session.session_id, "go")
    assert operation.done.wait(5)

    session.client.pending = (_pending_permission(session.session_id),)
    manager.respond_permission(operation.operation_id, "perm-1", ACP_ALLOW_ONCE_OPTION_ID)
    assert session.client.responded_permissions == [("perm-1", ACP_ALLOW_ONCE_OPTION_ID)]

    session.client.pending = (_pending_permission(SESSION_ID_TWO, request_id="perm-2"),)
    with pytest.raises(AcpManagerError) as exc_info:
        manager.respond_permission(operation.operation_id, "perm-2", ACP_ALLOW_ONCE_OPTION_ID)
    assert exc_info.value.code == "unknown_permission_request"

    session.client.pending = ()
    with pytest.raises(AcpManagerError) as exc_info:
        manager.respond_permission(operation.operation_id, "perm-3", ACP_ALLOW_ONCE_OPTION_ID)
    assert exc_info.value.code == "unknown_permission_request"

    with pytest.raises(AcpManagerError) as exc_info:
        manager.respond_permission(operation.operation_id, ["not", "hashable"], ACP_ALLOW_ONCE_OPTION_ID)
    assert exc_info.value.code == "unknown_permission_request"


def test_acp_manager_rejects_unsupported_permission_option(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    operation = manager.prompt(session.session_id, "go")
    assert operation.done.wait(5)
    session.client.pending = (_pending_permission(session.session_id),)

    with pytest.raises(AcpManagerError) as exc_info:
        manager.respond_permission(operation.operation_id, "perm-1", "allow-always")

    assert exc_info.value.code == "invalid_permission_option"
    assert session.client.responded_permissions == []


def test_acp_manager_permission_response_never_restarts_the_process(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    operation = manager.prompt(session.session_id, "go")
    assert operation.done.wait(5)
    dead_process = manager.process
    dead_process.dead = True

    with pytest.raises(AcpManagerError):
        manager.respond_permission(operation.operation_id, "perm-1", ACP_ALLOW_ONCE_OPTION_ID)

    assert manager.process is dead_process


# --- manager: cancel and close ----------------------------------------------


def test_acp_manager_cancel_does_not_flip_a_settled_operation(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    operation = manager.prompt(session.session_id, "go")
    assert operation.done.wait(5)
    published = operation.run_id
    session.client.cancelled.clear()

    manager.cancel(operation.operation_id)

    assert operation.state == "completed"
    assert operation.run_id == published
    assert session.client.cancelled == []


def test_acp_manager_cancel_unblocks_permissions_and_the_session(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    session.client.release_prompt = threading.Event()
    operation = manager.prompt(session.session_id, "slow")
    assert wait_for(lambda: session.client.prompt_calls)

    session.client.pending = (
        _pending_permission(session.session_id, request_id="perm-1"),
        _pending_permission(SESSION_ID_TWO, request_id="perm-2"),
    )
    manager.cancel(operation.operation_id)

    assert operation.cancel_event.is_set()
    assert session.client.cancelled_permissions == ["perm-1"]
    assert session.client.cancelled == [session.session_id]
    session.client.release_prompt.set()
    assert operation.done.wait(5)
    assert operation.state == "cancelled"
    assert session.active_operation is None


def test_acp_manager_close_session_leaves_the_others_alone(tmp_path):
    manager, workspace = _manager(tmp_path)
    first = manager.create_session(workspace)
    second = manager.create_session(workspace)
    assert first.client is second.client

    manager.close_session(first.session_id)

    assert first.client.closed == [first.session_id]
    assert second.session_id not in first.client.closed
    assert [item["session_id"] for item in manager.list_sessions()] == [second.session_id]
    with pytest.raises(AcpManagerError):
        manager.get_session(first.session_id)


def test_acp_manager_shutdown_settles_operations_and_closes_the_process(tmp_path):
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    session.client.release_prompt = threading.Event()
    operation = manager.prompt(session.session_id, "slow")
    assert wait_for(lambda: session.client.prompt_calls)
    process = manager.process

    session.client.release_prompt.set()
    manager.shutdown()

    assert operation.done.wait(5)
    assert process.dead is True
    assert manager.list_sessions() == []


def test_acp_manager_shutdown_bounds_a_worker_that_never_settles(tmp_path):
    """Shutdown must not hang on a worker that ignores cancellation."""
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    # The fake peer blocks indefinitely: no release event is ever set.
    session.client.release_prompt = threading.Event()
    operation = manager.prompt(session.session_id, "stuck")
    assert wait_for(lambda: session.client.prompt_calls)
    process = manager.process

    started = time.monotonic()
    manager.shutdown()
    elapsed = time.monotonic() - started

    # Shutdown is bounded by its join timeout, and the process is force-closed
    # even though the worker never settled on its own.
    assert elapsed < 10, "shutdown blocked on a stuck worker"
    assert process.dead is True
    assert manager.list_sessions() == []
    # A cancelled operation must never look successful.
    assert operation.state != "completed"


def test_route_shutdown_releases_the_manager_and_is_idempotent(injected_manager, tmp_path):
    """The FastAPI shutdown hook must close the process and be safe to repeat."""
    manager, workspace = _manager(tmp_path)
    manager.create_session(workspace)
    process = manager.process
    injected_manager(manager)

    dsh_acp_route.shutdown_acp_runtime()
    assert process.dead is True
    # A second call with no live manager must not raise.
    dsh_acp_route.shutdown_acp_runtime()
    assert dsh_acp_route._manager is None


def test_manager_closed_is_reported_before_any_work_starts(tmp_path):
    manager, workspace = _manager(tmp_path)
    manager.shutdown()
    with pytest.raises(AcpManagerError) as excinfo:
        manager.create_session(workspace)
    assert excinfo.value.code == "manager_closed"


def test_max_concurrency_must_be_positive(tmp_path):
    with pytest.raises(ValueError):
        _manager(tmp_path, max_concurrency=0)


# --- route: contract --------------------------------------------------------


def _client_settings(monkeypatch, settings):
    monkeypatch.setattr("backend.app.routes.dsh_acp.get_settings", lambda: settings)


def test_acp_options_and_routes_report_disabled_runtime(monkeypatch, tmp_path):
    _client_settings(monkeypatch, _settings(tmp_path, dsh_runtime_enabled=False))
    client = TestClient(app)

    options = client.get("/api/dsh/acp/options")
    assert options.status_code == 200
    assert options.json() == {
        "enabled": False,
        "disabled_reason": "ACP runtime is disabled",
        "capabilities": {},
    }
    assert client.get("/api/dsh/acp/sessions").status_code == 503
    assert client.post("/api/dsh/acp/session/new", json={"workspace": str(tmp_path)}).status_code == 503


def test_acp_routes_stay_disabled_when_only_the_master_switch_is_on(monkeypatch, tmp_path):
    """Master on + ACP off must not expose ACP, even with a valid allowlist."""
    settings = _settings(tmp_path, dsh_acp_enabled=False)
    _client_settings(monkeypatch, settings)
    # Guard against the test passing for the wrong reason: the config is valid.
    assert settings.dsh_runtime_enabled is True
    assert settings.dsh_acp_enabled is False
    client = TestClient(app)

    options = client.get("/api/dsh/acp/options")
    assert options.status_code == 200
    assert options.json() == {
        "enabled": False,
        "disabled_reason": "ACP runtime is disabled",
        "capabilities": {},
    }
    assert client.get("/api/dsh/acp/sessions").status_code == 503
    assert client.post("/api/dsh/acp/session/new", json={"workspace": str(tmp_path)}).status_code == 503
    assert client.get("/api/dsh/acp/operation/missing").status_code == 503
    assert client.post("/api/dsh/acp/cancel", json={"operation_id": "missing"}).status_code == 503


def test_acp_route_validation_and_error_mapping(monkeypatch, tmp_path, injected_manager):
    _client_settings(monkeypatch, _settings(tmp_path))
    manager, workspace = _manager(tmp_path)
    injected_manager(manager)
    client = TestClient(app)

    assert client.post("/api/dsh/acp/session/new", json={}).status_code == 400
    outside = client.post("/api/dsh/acp/session/new", json={"workspace": str(tmp_path.parent)})
    assert outside.status_code == 400
    assert outside.json()["detail"]["code"] == "invalid_workspace"
    assert client.post("/api/dsh/acp/session/resume", json={"session_id": "x"}).status_code == 400
    assert client.post("/api/dsh/acp/prompt", json={"session_id": "x"}).status_code == 400
    assert client.post("/api/dsh/acp/prompt", json={"session_id": "x", "message": "  "}).status_code == 400
    assert client.post(
        "/api/dsh/acp/prompt",
        json={"session_id": "x", "message": "hi", "acceptance_criteria": 5},
    ).status_code == 400
    unknown = client.post("/api/dsh/acp/prompt", json={"session_id": "missing", "message": "hi"})
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "unknown_session"
    assert client.get("/api/dsh/acp/operation/missing").status_code == 404
    assert client.post("/api/dsh/acp/cancel", json={}).status_code == 400
    assert client.post("/api/dsh/acp/operation/missing/cancel").status_code == 404
    assert client.post("/api/dsh/acp/permission/respond", json={}).status_code == 400
    assert client.post(
        "/api/dsh/acp/permission/respond",
        json={"operation_id": "missing", "request_id": "perm-1", "option_id": ACP_ALLOW_ONCE_OPTION_ID},
    ).status_code == 404
    assert client.delete("/api/dsh/acp/session/missing").status_code == 404


def test_acp_options_route_reports_live_capabilities(monkeypatch, tmp_path, injected_manager):
    _client_settings(monkeypatch, _settings(tmp_path))
    manager, workspace = _manager(tmp_path)
    manager.create_session(workspace)
    injected_manager(manager)
    client = TestClient(app)

    payload = client.get("/api/dsh/acp/options").json()

    assert payload["enabled"] is True
    assert payload["disabled_reason"] is None
    assert payload["profile"] == "acp"
    assert payload["capabilities"]["protocol_version"] == 1
    assert payload["permission_policy"] == {
        "allow": [ACP_ALLOW_ONCE_OPTION_ID],
        "reject": [ACP_REJECT_ONCE_OPTION_ID],
        "allow_always": False,
    }
    assert "supports_images" not in payload


def test_acp_session_new_list_operation_and_close_routes(monkeypatch, tmp_path, injected_manager):
    _client_settings(monkeypatch, _settings(tmp_path))
    manager, workspace = _manager(tmp_path)
    injected_manager(manager)
    client = TestClient(app)

    created = client.post("/api/dsh/acp/session/new", json={"workspace": str(workspace)})
    assert created.status_code == 200
    session_id = created.json()["session_id"]
    assert created.json()["capabilities"]["supports_session_close"] is True

    listed = client.get("/api/dsh/acp/sessions").json()["sessions"]
    assert [item["session_id"] for item in listed] == [session_id]
    assert listed[0]["state"] == "ready"

    _write_durable_recording(tmp_path / "dsh-home", workspace, session_id)
    streamed = client.post(
        "/api/dsh/acp/session/%s/prompt" % session_id,
        json={"message": "go", "acceptance_criteria": "done"},
    )
    assert streamed.status_code == 200
    names = [name for name, _ in _sse_events(streamed.text)]
    assert names[-1] == "run_imported"

    operation = _sse_events(streamed.text)[0][1]["operation_id"]
    status = client.get("/api/dsh/acp/operation/%s" % operation).json()
    assert status["state"] == "completed"
    assert status["done"] is True
    assert status["run_id"]
    assert status["error"] is None

    closed = client.post("/api/dsh/acp/session/%s/close" % session_id)
    assert closed.json() == {"ok": True, "session_id": session_id, "state": "closed"}
    assert client.get("/api/dsh/acp/sessions").json()["sessions"] == []


def test_acp_permission_route_rejects_unsupported_option(monkeypatch, tmp_path, injected_manager):
    _client_settings(monkeypatch, _settings(tmp_path))
    manager, workspace = _manager(tmp_path)
    session = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    operation = manager.prompt(session.session_id, "go")
    assert operation.done.wait(5)
    session.client.pending = (_pending_permission(session.session_id),)
    injected_manager(manager)
    client = TestClient(app)

    rejected = client.post(
        "/api/dsh/acp/permission/respond",
        json={
            "operation_id": operation.operation_id,
            "request_id": "perm-1",
            "option_id": "allow-always",
        },
    )
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "invalid_permission_option"

    accepted = client.post(
        "/api/dsh/acp/operation/%s/permission" % operation.operation_id,
        json={"request_id": "perm-1", "option_id": ACP_REJECT_ONCE_OPTION_ID},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {
        "ok": True,
        "operation_id": operation.operation_id,
        "request_id": "perm-1",
        "option_id": ACP_REJECT_ONCE_OPTION_ID,
    }
    assert session.client.responded_permissions == [("perm-1", ACP_REJECT_ONCE_OPTION_ID)]


# --- route: real transport, cross layer -------------------------------------


def _sse_events(text):
    events = []
    current = None
    for line in text.splitlines():
        if line.startswith("event: "):
            current = line.removeprefix("event: ")
        elif line.startswith("data: ") and current:
            events.append((current, json.loads(line.removeprefix("data: "))))
            current = None
    return events


def _real_transport_manager(tmp_path, responder):
    """A manager whose client is the real ACP client over a scripted peer."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    binary = tmp_path / "dsh"
    binary.write_text("fake")
    binary.chmod(0o755)
    config = DshRuntimeConfig(
        binary=binary,
        allowed_profiles=("acp",),
        allowed_workspace_roots=(tmp_path,),
        default_timeout_seconds=5,
        max_timeout_seconds=10,
    )
    peer = ScriptedAcpPeer(responder)

    def process_factory(argv, cwd=None):
        return AcpProcess(argv, peer, owns_process=False)

    manager = AcpRuntimeManager(
        config,
        profile="acp",
        process_factory=process_factory,
        transport_factory=AcpTransport,
        client_factory=AcpClient,
        run_root=tmp_path / "runs",
        dsh_home=tmp_path / "dsh-home",
        max_concurrency=2,
    )
    return manager, peer, workspace.resolve()


def _cross_layer_responder(session_id):
    """Stream updates, block on a permission request, then settle the turn."""

    def after(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": session_id, "configOptions": []})
            return
        if method != AgentMethod.SESSION_PROMPT:
            peer.send_result(request_id, {})
            return
        peer.session_update(
            session_id,
            {
                "sessionUpdate": SessionUpdateKind.TOOL_CALL,
                "toolCallId": "call-1",
                "title": "bash",
                "kind": "other",
                "status": "in_progress",
                "rawInput": {"command": "pytest"},
            },
        )
        peer.send(
            {
                "jsonrpc": "2.0",
                "id": "perm-1",
                "method": ClientMethod.SESSION_REQUEST_PERMISSION,
                "params": {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": "call-1"},
                    "options": [
                        {"optionId": ACP_ALLOW_ONCE_OPTION_ID, "name": "Allow once", "kind": "allow_once"},
                        {"optionId": ACP_REJECT_ONCE_OPTION_ID, "name": "Reject", "kind": "reject_once"},
                    ],
                },
            }
        )
        answer = peer.wait_for_response("perm-1", timeout=10)
        assert answer is not None, "the backend never answered the permission request"
        assert answer["result"]["outcome"] == {"outcome": "selected", "optionId": ACP_ALLOW_ONCE_OPTION_ID}
        peer.session_update(
            session_id,
            {
                "sessionUpdate": SessionUpdateKind.TOOL_CALL_UPDATE,
                "toolCallId": "call-1",
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "1 passed"}}],
            },
        )
        peer.session_update(
            session_id,
            {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": {"type": "text", "text": "tests pass"},
                "messageId": "m1",
            },
        )
        peer.send_result(request_id, {"stopReason": "end_turn", "usage": {"totalTokens": 7}})

    return after


def test_acp_prompt_route_streams_real_transport_and_imports(
    monkeypatch,
    tmp_path,
    injected_manager,
):
    """Cross-layer: HTTP -> SSE -> manager -> real ACP transport -> peer -> durable run.

    The prompt request runs on its own thread: the peer blocks the turn until the
    permission is answered through the HTTP route, which is what proves the
    transport keeps serving inbound requests while ``session/prompt`` is blocked.
    """
    settings = _settings(tmp_path)
    _client_settings(monkeypatch, settings)
    manager, peer, workspace = _real_transport_manager(
        tmp_path, initialize_responder(_cross_layer_responder(SESSION_ID))
    )
    injected_manager(manager)
    client = TestClient(app)

    created = client.post("/api/dsh/acp/session/new", json={"workspace": str(workspace)})
    assert created.status_code == 200
    session_id = created.json()["session_id"]
    assert session_id == SESSION_ID
    _write_durable_recording(settings.dsh_home, workspace, session_id)

    responses = []

    def start_prompt():
        streaming = TestClient(app)
        responses.append(
            streaming.post(
                "/api/dsh/acp/prompt",
                json={
                    "session_id": session_id,
                    "message": "run the tests",
                    "acceptance_criteria": "tests pass",
                },
            )
        )

    worker = threading.Thread(target=start_prompt, name="acp-route-prompt", daemon=True)
    try:
        worker.start()
        session = manager.get_session(session_id)
        assert wait_for(lambda: session.active_operation is not None, timeout=5)
        assert wait_for(lambda: session.client.pending_permissions(), timeout=10)

        permission = session.client.pending_permissions()[0]
        assert permission.tool_call_id == "call-1"
        assert permission.option_ids == (ACP_ALLOW_ONCE_OPTION_ID, ACP_REJECT_ONCE_OPTION_ID)
        assert session.active_operation is not None, "the turn must still be in flight"
        answered = client.post(
            "/api/dsh/acp/permission/respond",
            json={
                "operation_id": session.active_operation,
                "request_id": permission.request_id,
                "option_id": ACP_ALLOW_ONCE_OPTION_ID,
            },
        )
        assert answered.status_code == 200
        worker.join(15)
    finally:
        peer.close()

    assert not worker.is_alive()
    response = responses[0]
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)

    names = [name for name, _ in events]
    assert names[0] == "started"
    assert names[-1] == "run_imported"
    assert names.count("permission_requested") == 1
    assert names.index("permission_requested") < names.index("completed")
    assert names.index("completed") < names.index("run_imported")
    assert names.count("runtime_event") == 4
    assert [payload["type"] for name, payload in events if name == "runtime_event"] == [
        "tool_call",
        "tool_result",
        "text",
        "final",
    ]

    started = events[0][1]
    permission_event = next(payload for name, payload in events if name == "permission_requested")
    assert permission_event["session_id"] == session_id
    assert permission_event["tool_call_id"] == "call-1"
    assert [option["optionId"] for option in permission_event["options"]] == [
        ACP_ALLOW_ONCE_OPTION_ID,
        ACP_REJECT_ONCE_OPTION_ID,
    ]
    completed = next(payload for name, payload in events if name == "completed")
    assert completed == {
        "session_id": session_id,
        "stop_reason": "end_turn",
        "text": "tests pass",
        "cancelled": False,
        "dropped_events": 0,
    }
    imported = events[-1][1]
    assert imported["session_id"] == session_id
    assert imported["run_id"]

    run_dir = settings.runs_dir / imported["run_id"]
    assert json.loads((run_dir / "source.json").read_text())["dsh_profile"] == "acp"
    assert json.loads((run_dir / "expectations.json").read_text())["acceptance_criteria"] == "tests pass"
    assert [json.loads(line) for line in (run_dir / "trajectory.jsonl").read_text().splitlines()][-1][
        "content"
    ] == "done"

    status = client.get("/api/dsh/acp/operation/%s" % started["operation_id"]).json()
    assert status["state"] == "completed"
    assert status["run_id"] == imported["run_id"]
    assert client.get("/api/dsh/acp/sessions").json()["sessions"][0]["active_operation"] is None


class _FakeRequest:
    def __init__(self, disconnected=False):
        self.disconnected = disconnected

    async def is_disconnected(self):
        return self.disconnected


def _disconnecting_responder(cancelled_notification):
    """Block the turn until the client cancels it."""

    def responder(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": SESSION_ID, "configOptions": []})
            return
        if method != AgentMethod.SESSION_PROMPT:
            peer.send_result(request_id, {})
            return
        peer.session_update(
            SESSION_ID,
            {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": {"type": "text", "text": "working"},
            },
        )
        if wait_for(
            lambda: any(
                item["method"] == AgentMethod.SESSION_CANCEL for item in peer.captured_notifications()
            ),
            timeout=10,
        ):
            cancelled_notification.set()
        peer.send_result(request_id, {"stopReason": "cancelled"})

    return responder


def test_acp_operation_stream_releases_the_prompt_on_disconnect(tmp_path):
    """Both disconnect paths must cancel: the explicit check and task cancellation.

    Starlette closes the response generator with a cancellation at the current
    ``yield`` when the client goes away, so the explicit ``is_disconnected``
    branch alone is not enough.
    """
    settings = _settings(tmp_path)
    cancelled_notification = threading.Event()
    manager, peer, workspace = _real_transport_manager(
        tmp_path, initialize_responder(_disconnecting_responder(cancelled_notification))
    )
    session = manager.create_session(workspace)
    try:
        operation = manager.prompt(session.session_id, "long task")

        async def cancel_pending_read():
            stream = dsh_acp_route._operation_stream(
                _FakeRequest(),
                manager,
                operation,
            )
            first = await anext(stream)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.05)
            pending.cancel()
            try:
                await pending
            except asyncio.CancelledError:
                pass
            await stream.aclose()
            return first

        first = asyncio.run(cancel_pending_read())
        assert first.startswith("event: started")
        assert operation.cancel_event.is_set()
        assert cancelled_notification.wait(10)
        assert operation.done.wait(10)
        assert operation.state == "cancelled"
        assert operation.run_id is None
        assert not (tmp_path / "runs").exists() or list((tmp_path / "runs").iterdir()) == []
        assert session.active_operation is None
    finally:
        peer.close()


def test_acp_operation_stream_reports_explicit_client_disconnect(tmp_path):
    settings = _settings(tmp_path)
    cancelled_notification = threading.Event()
    manager, peer, workspace = _real_transport_manager(
        tmp_path, initialize_responder(_disconnecting_responder(cancelled_notification))
    )
    session = manager.create_session(workspace)
    try:
        operation = manager.prompt(session.session_id, "long task")

        async def read_all():
            stream = dsh_acp_route._operation_stream(_FakeRequest(disconnected=True), manager, operation)
            return [chunk async for chunk in stream]

        chunks = asyncio.run(read_all())
        assert len(chunks) == 1
        assert chunks[0].startswith("event: error")
        assert json.loads(chunks[0].split("data: ", 1)[1]) == {
            "code": "client_disconnected",
            "detail": "ACP client disconnected",
            "operation_id": operation.operation_id,
            "session_id": session.session_id,
        }
        assert cancelled_notification.wait(10)
        assert operation.done.wait(10)
        assert operation.state == "cancelled"
    finally:
        peer.close()


def test_acp_prompt_route_reports_queue_conflicts_before_streaming(monkeypatch, tmp_path, injected_manager):
    _client_settings(monkeypatch, _settings(tmp_path))
    manager, workspace = _manager(tmp_path, max_concurrency=1)
    session = manager.create_session(workspace)
    second = manager.create_session(workspace)
    _write_durable_recording(tmp_path / "dsh-home", workspace, session.session_id)
    _write_durable_recording(tmp_path / "dsh-home", workspace, second.session_id)
    session.client.release_prompt = threading.Event()
    injected_manager(manager)
    client = TestClient(app)

    first = manager.prompt(session.session_id, "one")
    assert wait_for(lambda: session.client.prompt_calls)
    busy = client.post(
        "/api/dsh/acp/prompt",
        json={"session_id": second.session_id, "message": "two"},
    )
    assert busy.status_code == 409
    assert busy.json()["detail"]["code"] == "runtime_busy"
    session.client.release_prompt.set()
    assert first.done.wait(5)


# --- manager: complete turn projection --------------------------------------


def _streaming_chunk_responder(turn_id, chunk_count):
    """Answer ``session/prompt`` with a burst of text chunks at full speed.

    The whole burst is usually already on the wire when the response arrives,
    which is exactly the shape that used to split a turn between the manager's
    monitor thread and the client's own inbox drain.
    """

    def responder(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": turn_id, "configOptions": []})
            return
        if method != AgentMethod.SESSION_PROMPT:
            peer.send_result(request_id, {})
            return
        for index in range(chunk_count):
            peer.session_update(
                turn_id,
                {
                    "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "c%d;" % index},
                    "messageId": "m1",
                },
            )
        peer.send_result(request_id, {"stopReason": "end_turn"})

    return responder


def test_acp_operation_streams_every_chunk_of_a_burst(tmp_path):
    """The projection must deliver the whole turn, not a scheduling slice."""
    chunk_count = 60
    settings = _settings(tmp_path)
    manager, peer, workspace = _real_transport_manager(
        tmp_path, initialize_responder(_streaming_chunk_responder(SESSION_ID, chunk_count))
    )
    session = manager.create_session(workspace)
    try:
        operation = manager.prompt(session.session_id, "burst")
        assert operation.done.wait(20)

        items = []
        while not operation.queue.empty():
            items.append(operation.queue.get(timeout=1))
    finally:
        peer.close()

    streamed = "".join(
        str(item["data"].get("data", {}).get("text") or "")
        for item in items
        if item["event"] == "runtime_event" and item["data"].get("type") == "text"
    )
    assert streamed == "".join("c%d;" % index for index in range(chunk_count))
    completed = next(item for item in items if item["event"] == "completed")
    assert completed["data"]["text"] == streamed
