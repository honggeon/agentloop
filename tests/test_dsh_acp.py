"""Deterministic tests for the ACP transport.

Every test drives a fake in-process peer over real OS pipes, so the framing,
threading, and JSON-RPC correlation paths are exercised without spawning DSH and
without any model call or network access.
"""

import io
import json
import os
import threading
import time
from pathlib import Path

import pytest

from auditrail.dsh_acp import (
    ACP_ALLOW_ONCE_OPTION_ID,
    ACP_MAX_SUBSCRIBED_UPDATES,
    ACP_PROTOCOL_VERSION,
    ACP_REJECT_ONCE_OPTION_ID,
    AcpClient,
    AcpError,
    AcpFrameReader,
    AcpMessage,
    AcpProcess,
    AcpRemoteError,
    AcpStopReason,
    AcpTransport,
    AgentMethod,
    ClientMethod,
    JSONRPC_METHOD_NOT_FOUND,
    ProtocolMethod,
    SessionUpdateKind,
    build_acp_argv,
    decode_frame,
    encode_frame,
    normalize_session_update,
    read_frame_bytes,
    spawn_acp_process,
)
from auditrail.dsh_runtime import DshRuntimeConfig, DshRuntimeEvent, DshValidationError


SESSION_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
SESSION_ID_TWO = "1a2b3c4d-5e6f-4a1b-8c2d-3e4f50617283"


# --- fixtures ---------------------------------------------------------------


def make_config(tmp_path, **overrides):
    root = tmp_path / "root"
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    values = {
        "binary": Path("/opt/dsh/bin/dsh"),
        "allowed_profiles": ("acp",),
        "allowed_workspace_roots": (root,),
        "default_timeout_seconds": 5,
        "max_timeout_seconds": 30,
    }
    values.update(overrides)
    return DshRuntimeConfig(**values), workspace


class ScriptedAcpPeer:
    """A scripted ACP server over real pipes.

    ``responder(method, params, request_id, peer)`` runs on its own thread and
    may block (for example, waiting for a permission answer) without blocking the
    client's reader.
    """

    def __init__(self, responder=None, *, on_frame=None):
        self._responder = responder
        self._on_frame = on_frame
        self._lock = threading.Lock()
        self._responses: dict[object, dict] = {}
        self._conditions: dict[object, threading.Condition] = {}
        self._notifications: list[dict] = []
        self._server_to_client_r, self._server_to_client_w = os.pipe()
        self._client_to_server_r, self._client_to_server_w = os.pipe()
        self.stdout = os.fdopen(self._server_to_client_r, "rb", 0)
        self.stdin = os.fdopen(self._client_to_server_w, "wb", 0)
        self._server_stdout = os.fdopen(self._server_to_client_w, "wb", 0)
        self._server_stdin = os.fdopen(self._client_to_server_r, "rb", 0)
        self._closed = False
        self._shutdown = threading.Event()
        self._reader = threading.Thread(target=self._read_loop, name="fake-acp-peer", daemon=True)
        self._reader.start()

    # -- process surface used by the transport --

    @property
    def stderr(self):
        return io.BytesIO(b"")

    @property
    def pid(self):
        return os.getpid()

    def poll(self):
        return 0 if self._closed else None

    def wait(self, timeout=None):
        if not self._closed:
            raise TypeError("fake peer has not exited")
        return 0

    # -- peer behaviour --

    def send(self, payload):
        """Write one frame; a teardown race is a silent no-op, not a crash."""
        with self._lock:
            if self._shutdown.is_set():
                return
            try:
                self._server_stdout.write(encode_frame(payload))
                self._server_stdout.flush()
            except (OSError, ValueError):
                return

    def send_result(self, request_id, result):
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def send_error(self, request_id, code, message, data=None):
        error = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self.send({"jsonrpc": "2.0", "id": request_id, "error": error})

    def notify(self, method, params):
        self.send({"jsonrpc": "2.0", "method": method, "params": params})

    def session_update(self, session_id, update):
        self.notify(
            ClientMethod.SESSION_UPDATE,
            {"sessionId": session_id, "update": update},
        )

    def captured_notifications(self):
        with self._lock:
            return list(self._notifications)

    def wait_for_response(self, request_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        with self._lock:
            condition = self._conditions.setdefault(request_id, threading.Condition(self._lock))
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                condition.wait(timeout=remaining)
            return self._responses[request_id]

    def close(self):
        self._shutdown.set()
        self._closed = True
        for stream in (self._server_stdout, self._server_stdin, self.stdout, self.stdin):
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _read_loop(self):
        try:
            while True:
                line = self._server_stdin.readline()
                if not line:
                    return
                try:
                    payload = json.loads(line.decode("utf-8").strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if self._on_frame is not None:
                    self._on_frame(payload)
                if "method" in payload and "id" in payload:
                    self._handle_request(payload)
                elif "method" in payload:
                    with self._lock:
                        self._notifications.append(payload)
                elif "id" in payload:
                    with self._lock:
                        self._responses[payload["id"]] = payload
                        condition = self._conditions.setdefault(
                            payload["id"], threading.Condition(self._lock)
                        )
                        condition.notify_all()
        except (OSError, ValueError):
            return

    def _handle_request(self, payload):
        responder = self._responder
        if responder is None:
            self.send_error(payload["id"], JSONRPC_METHOD_NOT_FOUND, "unscripted")
            return

        def run():
            try:
                responder(payload.get("method"), payload.get("params"), payload["id"], self)
            except Exception as exc:  # pragma: no cover - surfacing test bugs
                if not self._shutdown.is_set():
                    self.send_error(payload["id"], -32603, "responder failed: %r" % (exc,))

        threading.Thread(target=run, name="fake-acp-responder", daemon=True).start()


@pytest.fixture
def acp_env(tmp_path):
    created: list = []

    def build(responder=None, *, on_frame=None, on_permission=None, **config_overrides):
        config, workspace = make_config(tmp_path, **config_overrides)
        peer = ScriptedAcpPeer(responder, on_frame=on_frame)
        created.append(peer)
        process = AcpProcess(("dsh", "--profile", "acp"), peer, owns_process=False)
        transport = AcpTransport(process, max_frame_bytes=config.max_event_bytes).start()
        client = AcpClient(transport, config, profile="acp", on_permission=on_permission)
        return client, transport, peer, workspace

    yield build
    for peer in created:
        peer.close()


def default_session_responder(session_id=SESSION_ID):
    """Answer ``session/new``/``session/resume``/``session/close`` plausibly."""

    def responder(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": session_id, "configOptions": []})
            return True
        if method == AgentMethod.SESSION_RESUME:
            # DSH deliberately does not echo the session id on resume.
            peer.send_result(request_id, {"configOptions": []})
            return True
        return False

    return responder


def initialize_responder(next_responder=None, *, version=ACP_PROTOCOL_VERSION, capabilities=None):
    """Respond to initialize, then delegate to ``next_responder``.

    Without an explicit responder, unscripted methods get a plausible empty
    result so a test only has to describe the behaviour it asserts.
    """
    session_responder = default_session_responder()

    def responder(method, params, request_id, peer):
        if method == AgentMethod.INITIALIZE:
            peer.send_result(
                request_id,
                {
                    "protocolVersion": version,
                    "agentInfo": {"name": "deepseek-harness-acp", "version": "0.0.1"},
                    "agentCapabilities": capabilities
                    if capabilities is not None
                    else default_capabilities(),
                    "authMethods": [],
                },
            )
            return
        if next_responder is not None:
            next_responder(method, params, request_id, peer)
            return
        if session_responder(method, params, request_id, peer):
            return
        peer.send_result(request_id, {})

    return responder


def default_capabilities(*, image=False):
    return {
        "mcpCapabilities": {"http": True},
        "promptCapabilities": {"image": image, "audio": False, "embeddedContext": False},
        "sessionCapabilities": {"close": {}, "list": {}, "resume": {}},
    }


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- framing ----------------------------------------------------------------


def test_encode_frame_is_ndjson_with_trailing_newline():
    assert encode_frame({"jsonrpc": "2.0", "id": 1}) == b'{"jsonrpc": "2.0", "id": 1}\n'


def test_encode_frame_preserves_non_ascii():
    assert encode_frame({"text": "思考"}).endswith(b"\n")
    assert "思考".encode("utf-8") in encode_frame({"text": "思考"})


def test_decode_frame_classifies_each_envelope():
    request = decode_frame(b'{"jsonrpc":"2.0","id":7,"method":"initialize","params":{}}')
    assert request.is_request and not request.is_notification and not request.is_response
    assert request.method == "initialize" and request.request_id == 7

    notification = decode_frame(b'{"jsonrpc":"2.0","method":"session/update","params":{}}')
    assert notification.is_notification and request.is_request
    assert not notification.has_id

    response = decode_frame(b'{"jsonrpc":"2.0","id":7,"result":{"ok":true}}')
    assert response.is_response and not response.is_error
    assert response.result == {"ok": True}

    failure = decode_frame(b'{"jsonrpc":"2.0","id":7,"error":{"code":-32601,"message":"nope"}}')
    assert failure.is_error
    assert failure.peer_error.code == -32601


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[1,2,3]",
        b'{"id":1,"method":"initialize"}',
        b'{"jsonrpc":"1.0","id":1,"method":"initialize"}',
        b"   ",
    ],
)
def test_decode_frame_rejects_malformed_frames(payload):
    with pytest.raises(AcpError) as excinfo:
        decode_frame(payload)
    assert excinfo.value.code == "malformed_frame"


def test_decode_frame_rejects_invalid_utf8():
    with pytest.raises(AcpError) as excinfo:
        decode_frame(b"\xff\xfe")
    assert excinfo.value.code == "malformed_frame"


def test_frame_reader_returns_each_line_and_tolerates_missing_final_newline():
    reader = AcpFrameReader(io.BytesIO(b'{"a":1}\n{"b":2}\n{"c":3}'), max_bytes=1024)
    assert reader.read().frame == b'{"a":1}'
    assert reader.read().frame == b'{"b":2}'
    assert reader.read().frame == b'{"c":3}'
    assert reader.read().end_of_stream


def test_frame_reader_reports_oversize_and_stays_framed():
    oversized = b"x" * 4096
    reader = AcpFrameReader(io.BytesIO(oversized + b"\n" + b'{"ok":true}\n'), max_bytes=64)
    first = reader.read()
    assert first.too_large and first.discarded_bytes == len(oversized)
    assert reader.read().frame == b'{"ok":true}'


def test_frame_reader_requires_positive_limits():
    with pytest.raises(DshValidationError):
        AcpFrameReader(io.BytesIO(b""), max_bytes=0)
    with pytest.raises(DshValidationError):
        AcpFrameReader(io.BytesIO(b""), max_bytes=8, chunk_size=0)


def test_read_frame_bytes_is_a_convenience_over_a_fresh_reader():
    assert read_frame_bytes(io.BytesIO(b'{"a":1}\n'), max_bytes=64).frame == b'{"a":1}'


def test_frame_reader_buffers_partial_frames_across_calls():
    """A pipe may deliver a frame in pieces; nothing may be lost or merged."""
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb", 0) as stream:
        reader = AcpFrameReader(stream, max_bytes=1024)
        os.write(write_fd, b'{"partial":')
        os.write(write_fd, b'true}\n{"second"')
        assert reader.read().frame == b'{"partial":true}'
        os.write(write_fd, b':1}\n')
        assert reader.read().frame == b'{"second":1}'
        os.close(write_fd)
        assert reader.read().end_of_stream


# --- argv -------------------------------------------------------------------


def test_build_acp_argv_is_the_only_supported_invocation(tmp_path):
    config, _ = make_config(tmp_path)
    assert build_acp_argv(config, "acp") == ("/opt/dsh/bin/dsh", "--profile", "acp")


def test_build_acp_argv_rejects_disallowed_profile_and_judge_profile(tmp_path):
    config, _ = make_config(tmp_path, judge_profile="agentloop-judge")
    with pytest.raises(DshValidationError):
        build_acp_argv(config, "web")
    with pytest.raises(DshValidationError):
        build_acp_argv(config, "agentloop-judge")


def test_spawn_acp_process_never_uses_a_shell_and_merges_extra_env(monkeypatch, tmp_path):
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.stdin = None
            self.stdout = None
            self.stderr = None
            self.pid = 4242

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr("auditrail.dsh_acp.subprocess.Popen", FakePopen)
    config, workspace = make_config(tmp_path)
    argv = build_acp_argv(config, "acp")
    process = spawn_acp_process(
        argv,
        cwd=workspace,
        extra_env={"DSH_TELEMETRY_MODE": "DISABLED"},
    )

    assert captured["argv"] == ["/opt/dsh/bin/dsh", "--profile", "acp"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(workspace)
    assert captured["kwargs"]["start_new_session"] is True
    assert captured["kwargs"]["env"]["DSH_TELEMETRY_MODE"] == "DISABLED"
    # No caller-supplied flag or task text can reach the ACP invocation.
    assert argv[1:] == ("--profile", "acp")
    assert process.argv == tuple(argv)
    assert process.pid == 4242


def test_acp_process_without_ownership_only_closes_stdin():
    peer = ScriptedAcpPeer()
    try:
        process = AcpProcess(("dsh", "--profile", "acp"), peer, owns_process=False)
        process.close_stdin()
        assert process.owns_process is False
        process.terminate()
        assert peer.poll() is None  # an unowned process is never signalled
    finally:
        peer.close()


# --- transport --------------------------------------------------------------


def test_transport_correlates_out_of_order_responses(acp_env):
    def responder(method, params, request_id, peer):
        if method == AgentMethod.INITIALIZE:
            peer.send_result(
                request_id,
                {"protocolVersion": ACP_PROTOCOL_VERSION, "agentCapabilities": default_capabilities()},
            )
            return
        peer.send_result(request_id, {"method": method})


    client, transport, peer, _ = acp_env(responder)
    client.initialize(timeout_seconds=5)
    results = {}

    def call(name):
        results[name] = transport.send_request("probe/%s" % name, {}, timeout_seconds=5)

    threads = [threading.Thread(target=call, args=(name,)) for name in ("a", "b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert results == {"a": {"method": "probe/a"}, "b": {"method": "probe/b"}, "c": {"method": "probe/c"}}


def test_transport_ids_are_unique_and_monotonic(acp_env):
    seen = []

    def on_frame(payload):
        if "method" in payload and "id" in payload:
            seen.append(payload["id"])

    def responder(method, params, request_id, peer):
        if method == AgentMethod.INITIALIZE:
            peer.send_result(
                request_id,
                {"protocolVersion": ACP_PROTOCOL_VERSION, "agentCapabilities": default_capabilities()},
            )
            return
        peer.send_result(request_id, {})

    client, transport, peer, _ = acp_env(responder, on_frame=on_frame)
    client.initialize(timeout_seconds=5)
    for _ in range(5):
        transport.send_request("probe", {}, timeout_seconds=5)
    assert seen == sorted(seen) and len(set(seen)) == len(seen) == 6


def test_transport_surfaces_peer_error_response(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_error(request_id, -32601, '"Method not found": session/fork', {"m": "x"})

    _, transport, _, _ = acp_env(responder)
    with pytest.raises(AcpRemoteError) as excinfo:
        transport.send_request("session/fork", {}, timeout_seconds=5)
    assert excinfo.value.code == "remote_error"
    assert excinfo.value.code_number == -32601
    assert "session/fork" in excinfo.value.message


def test_transport_times_out_explicitly(acp_env):
    def responder(method, params, request_id, peer):
        return  # never answers

    _, transport, _, _ = acp_env(responder)
    started = time.monotonic()
    with pytest.raises(AcpError) as excinfo:
        transport.send_request("session/prompt", {}, timeout_seconds=0.2)
    assert excinfo.value.code == "timeout"
    assert time.monotonic() - started < 5


def test_transport_fails_pending_request_when_peer_exits(acp_env):
    def responder(method, params, request_id, peer):
        peer.close()

    _, transport, _, _ = acp_env(responder)
    with pytest.raises(AcpError) as excinfo:
        transport.send_request("session/new", {}, timeout_seconds=5)
    assert excinfo.value.code in {"closed", "write_failed"}


def test_transport_answers_unhandled_inbound_request_with_method_not_found(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_result(request_id, {})

    _, transport, peer, _ = acp_env(responder)
    seen = threading.Event()
    transport.handle_request("session/request_permission", lambda message, respond: False)
    peer.send({"jsonrpc": "2.0", "id": "inbound-1", "method": "session/request_permission", "params": {}})
    assert wait_for(lambda: peer.wait_for_response("inbound-1", timeout=0.1) is not None, timeout=5)
    response = peer.wait_for_response("inbound-1")
    assert response["error"]["code"] == JSONRPC_METHOD_NOT_FOUND


def test_transport_answers_unknown_method_without_a_handler(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_result(request_id, {})

    _, transport, peer, _ = acp_env(responder)
    peer.send({"jsonrpc": "2.0", "id": "inbound-2", "method": "terminal/create", "params": {}})
    assert wait_for(lambda: peer.wait_for_response("inbound-2", timeout=0.1) is not None, timeout=5)
    assert peer.wait_for_response("inbound-2")["error"]["code"] == JSONRPC_METHOD_NOT_FOUND


def test_inbound_handler_failure_becomes_internal_error(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_result(request_id, {})

    _, transport, peer, _ = acp_env(responder)

    def exploding(message, respond):
        raise RuntimeError("boom")

    transport.handle_request("session/request_permission", exploding)
    peer.send({"jsonrpc": "2.0", "id": "inbound-3", "method": "session/request_permission", "params": {}})
    assert wait_for(lambda: peer.wait_for_response("inbound-3", timeout=0.1) is not None, timeout=5)
    assert peer.wait_for_response("inbound-3")["error"]["code"] == -32603


def test_transport_rejects_oversize_frame_and_closes(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_result(request_id, {})

    client, transport, peer, _ = acp_env(responder, max_event_bytes=1024)
    peer.send({"jsonrpc": "2.0", "method": "session/update", "params": {"blob": "x" * 8192}})
    assert wait_for(lambda: transport.is_closed, timeout=5)
    assert transport.close_error is not None
    assert transport.close_error.code == "frame_too_large"
    assert transport.framing_errors


def test_transport_records_malformed_frame_and_keeps_reading(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_result(request_id, {"ok": True})

    client, transport, peer, _ = acp_env(responder)
    # A malformed line must not kill the stream, matching the reference SDK.
    with peer._lock:
        peer._server_stdout.write(b"this is not json\n")
        peer._server_stdout.flush()
    assert transport.send_request("probe", {}, timeout_seconds=5) == {"ok": True}
    assert any(error.code == "malformed_frame" for error in transport.framing_errors)


def test_transport_requires_positive_limits(tmp_path):
    config, _ = make_config(tmp_path)
    peer = ScriptedAcpPeer()
    try:
        process = AcpProcess(("dsh",), peer, owns_process=False)
        with pytest.raises(DshValidationError):
            AcpTransport(process, max_frame_bytes=0)
        with pytest.raises(DshValidationError):
            AcpTransport(process, write_timeout_seconds=0)
    finally:
        peer.close()


def test_send_response_is_one_shot(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_result(request_id, {})

    _, transport, _, _ = acp_env(responder)
    assert transport.send_response("x", {"ok": True}) is True
    assert transport.send_response("x", {"ok": True}) is False
    assert transport.send_error_response("x", -32603, "late") is False


# --- client: handshake ------------------------------------------------------


def test_initialize_negotiates_version_and_capabilities(acp_env):
    client, _, _, _ = acp_env(initialize_responder())
    result = client.initialize(timeout_seconds=5)
    assert result.protocol_version == ACP_PROTOCOL_VERSION
    assert result.agent_name == "deepseek-harness-acp"
    assert result.supports_session_list
    assert result.supports_session_resume
    assert result.supports_session_close
    assert result.supports_images is False
    assert result.auth_methods == ()


def test_initialize_reports_image_capability_when_advertised(acp_env):
    client, _, _, _ = acp_env(initialize_responder(capabilities=default_capabilities(image=True)))
    assert client.initialize(timeout_seconds=5).supports_images is True


def test_initialize_sends_expected_params(acp_env):
    captured = {}

    def on_frame(payload):
        if payload.get("method") == AgentMethod.INITIALIZE:
            captured.update(payload["params"])

    client, _, _, _ = acp_env(initialize_responder(), on_frame=on_frame)
    client.initialize(timeout_seconds=5)
    assert captured["protocolVersion"] == ACP_PROTOCOL_VERSION
    # The transport implements no ACP client extension, so it must claim none.
    assert captured["clientCapabilities"] == {}
    assert captured["clientInfo"]["name"]


def test_initialize_rejects_version_mismatch(acp_env):
    client, _, _, _ = acp_env(initialize_responder(version=2))
    with pytest.raises(AcpError) as excinfo:
        client.initialize(timeout_seconds=5)
    assert excinfo.value.code == "unsupported_protocol_version"


def test_initialize_rejects_missing_protocol_version(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_result(request_id, {"agentInfo": {"name": "x"}})

    client, _, _, _ = acp_env(responder)
    with pytest.raises(AcpError) as excinfo:
        client.initialize(timeout_seconds=5)
    assert excinfo.value.code == "malformed_result"


def test_session_calls_require_initialize(acp_env):
    def responder(method, params, request_id, peer):
        peer.send_result(request_id, {"sessionId": SESSION_ID})

    client, _, _, workspace = acp_env(responder)
    with pytest.raises(AcpError) as excinfo:
        client.list_sessions(timeout_seconds=5)
    assert excinfo.value.code == "not_initialized"
    assert workspace.exists()


# --- client: sessions -------------------------------------------------------


def test_new_session_uses_validated_workspace(acp_env):
    captured = {}

    def after(method, params, request_id, peer):
        captured.update(params)
        peer.send_result(request_id, {"sessionId": SESSION_ID, "configOptions": [{"id": "model"}]})

    client, _, _, workspace = acp_env(initialize_responder(after))
    client.initialize(timeout_seconds=5)
    session = client.new_session(workspace, timeout_seconds=5)
    assert session.session_id == SESSION_ID
    assert session.workspace == workspace.resolve()
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["mcpServers"] == []


def test_new_session_rejects_workspace_outside_allowed_roots(acp_env, tmp_path):
    client, _, _, _ = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(DshValidationError):
        client.new_session(outside, timeout_seconds=5)


def test_new_session_rejects_invalid_remote_session_id(acp_env):
    def after(method, params, request_id, peer):
        peer.send_result(request_id, {"sessionId": "../../etc/passwd"})

    client, _, _, workspace = acp_env(initialize_responder(after))
    client.initialize(timeout_seconds=5)
    with pytest.raises(AcpError) as excinfo:
        client.new_session(workspace, timeout_seconds=5)
    assert excinfo.value.code == "invalid_session_id"


def test_resume_uses_request_session_id_because_dsh_does_not_echo_it(acp_env):
    captured = {}

    def after(method, params, request_id, peer):
        captured.update(params)
        # DSH's session/resume response carries configOptions only.
        peer.send_result(request_id, {"configOptions": [{"id": "reasoning_effort"}]})

    client, _, _, workspace = acp_env(initialize_responder(after))
    client.initialize(timeout_seconds=5)
    session = client.resume_session(SESSION_ID, workspace, timeout_seconds=5)
    assert session.session_id == SESSION_ID
    assert captured["sessionId"] == SESSION_ID
    assert captured["cwd"] == str(workspace.resolve())
    assert session.config_options == ({"id": "reasoning_effort"},)


def test_list_sessions_pages_and_requires_capability(acp_env):
    def after(method, params, request_id, peer):
        peer.send_result(
            request_id,
            {
                "sessions": [
                    {"sessionId": SESSION_ID, "cwd": "/tmp/x", "title": "t", "updatedAt": "2026-01-01"},
                    {"sessionId": SESSION_ID_TWO, "cwd": "/tmp/y"},
                    "not-an-object",
                ],
                "nextCursor": "cursor-2",
            },
        )

    client, _, _, _ = acp_env(initialize_responder(after))
    client.initialize(timeout_seconds=5)
    page = client.list_sessions(timeout_seconds=5)
    assert [item.session_id for item in page.sessions] == [SESSION_ID, SESSION_ID_TWO]
    assert page.sessions[0].title == "t"
    assert page.next_cursor == "cursor-2"

    limited, _, _, _ = acp_env(
        initialize_responder(after, capabilities={"sessionCapabilities": {"close": {}}})
    )
    limited.initialize(timeout_seconds=5)
    with pytest.raises(AcpError) as excinfo:
        limited.list_sessions(timeout_seconds=5)
    assert excinfo.value.code == "unsupported_capability"


def test_close_session_only_forgets_the_addressed_session(acp_env):
    closed = []

    def after(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": params.get("_id", SESSION_ID)})
            return
        if method == AgentMethod.SESSION_CLOSE:
            closed.append(params["sessionId"])
            peer.send_result(request_id, {})
            return
        peer.send_result(request_id, {})

    client, _, _, workspace = acp_env(initialize_responder(after))
    client.initialize(timeout_seconds=5)
    first = client.new_session(workspace, timeout_seconds=5)
    with client._lock:
        client._sessions[SESSION_ID_TWO] = type(first)(
            session_id=SESSION_ID_TWO, workspace=workspace.resolve()
        )
    client.close_session(first.session_id, timeout_seconds=5)
    assert closed == [SESSION_ID]
    assert client.session(SESSION_ID) is None
    assert client.session(SESSION_ID_TWO) is not None


def test_close_session_requires_the_capability(acp_env):
    def after(method, params, request_id, peer):
        peer.send_result(request_id, {"sessionId": SESSION_ID})

    client, _, _, workspace = acp_env(initialize_responder(after))
    client.initialize(timeout_seconds=5)
    session = client.new_session(workspace, timeout_seconds=5)
    # default_capabilities advertises close, so start a peer without it.
    limited, _, _, workspace2 = acp_env(
        initialize_responder(
            lambda method, params, request_id, peer: peer.send_result(
                request_id, {"sessionId": SESSION_ID_TWO}
            ),
            capabilities={"sessionCapabilities": {"list": {}}},
        )
    )
    limited.initialize(timeout_seconds=5)
    limited.new_session(workspace2, timeout_seconds=5)
    with pytest.raises(AcpError) as excinfo:
        limited.close_session(SESSION_ID_TWO, timeout_seconds=5)
    assert excinfo.value.code == "unsupported_capability"
    assert session.session_id == SESSION_ID


def test_set_config_option_updates_cached_options(acp_env):
    captured = {}

    def after(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": SESSION_ID})
            return
        captured.update(params)
        peer.send_result(request_id, {"configOptions": [{"id": "model", "currentValue": "deepseek-v4-flash"}]})

    client, _, _, workspace = acp_env(initialize_responder(after))
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    options = client.set_config_option(SESSION_ID, "model", "deepseek-v4-flash", timeout_seconds=5)
    assert captured == {"sessionId": SESSION_ID, "configId": "model", "value": "deepseek-v4-flash"}
    assert options[0]["currentValue"] == "deepseek-v4-flash"
    assert client.session(SESSION_ID).config_options[0]["id"] == "model"


def test_session_calls_reject_unknown_local_sessions(acp_env):
    client, _, _, _ = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    with pytest.raises(AcpError) as excinfo:
        client.prompt(SESSION_ID, "hi", timeout_seconds=5)
    assert excinfo.value.code == "unknown_session"


# --- client: prompt turn ----------------------------------------------------


def prompt_responder(*, stop_reason="end_turn", text="hello", usage=None, session_id=SESSION_ID):
    def after(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": session_id})
            return
        if method == AgentMethod.SESSION_PROMPT:
            peer.session_update(
                session_id,
                {
                    "sessionUpdate": SessionUpdateKind.AGENT_THOUGHT_CHUNK,
                    "content": {"type": "text", "text": "thinking"},
                },
            )
            peer.session_update(
                session_id,
                {
                    "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": text},
                    "messageId": "m1",
                },
            )
            result = {"stopReason": stop_reason}
            if usage is not None:
                result["usage"] = usage
            peer.send_result(request_id, result)
            return
        peer.send_result(request_id, {})

    return after


def test_prompt_returns_ordered_updates_and_terminal_event(acp_env):
    client, _, _, workspace = acp_env(initialize_responder(prompt_responder(text="answer")))
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    outcome = client.prompt(SESSION_ID, "do the thing", timeout_seconds=5)
    assert outcome.stop_reason == AcpStopReason.END_TURN
    assert outcome.cancelled is False
    assert outcome.text == "answer"
    assert [event.type for event in outcome.updates] == ["thinking", "text", "final"]
    assert outcome.updates[0].data["text"] == "thinking"
    assert outcome.updates[-1].data["stop_reason"] == "end_turn"


def test_prompt_records_usage(acp_env):
    client, _, _, workspace = acp_env(
        initialize_responder(prompt_responder(usage={"totalTokens": 12, "inputTokens": 8, "outputTokens": 4}))
    )
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    outcome = client.prompt(SESSION_ID, "x", timeout_seconds=5)
    assert outcome.usage == {"totalTokens": 12, "inputTokens": 8, "outputTokens": 4}


def test_prompt_requires_a_stop_reason(acp_env):
    def responder(method, params, request_id, peer):
        if method == AgentMethod.INITIALIZE:
            peer.send_result(request_id, {"protocolVersion": 1, "agentCapabilities": default_capabilities()})
        elif method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": SESSION_ID})
        else:
            peer.send_result(request_id, {})

    client, _, _, workspace = acp_env(responder)
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    with pytest.raises(AcpError) as excinfo:
        client.prompt(SESSION_ID, "x", timeout_seconds=5)
    assert excinfo.value.code == "malformed_result"


def test_prompt_is_exclusive_per_session(acp_env):
    release = threading.Event()
    entered = threading.Event()

    def after(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": SESSION_ID})
            return
        if method == AgentMethod.SESSION_PROMPT:
            entered.set()
            release.wait(timeout=5)
            peer.send_result(request_id, {"stopReason": "end_turn"})
            return
        peer.send_result(request_id, {})

    client, _, _, workspace = acp_env(initialize_responder(after))
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    errors = []

    def first():
        try:
            client.prompt(SESSION_ID, "one", timeout_seconds=5)
        except AcpError as exc:
            errors.append(exc)

    worker = threading.Thread(target=first)
    worker.start()
    assert entered.wait(timeout=5)
    with pytest.raises(AcpError) as excinfo:
        client.prompt(SESSION_ID, "two", timeout_seconds=5)
    assert excinfo.value.code == "prompt_in_flight"
    release.set()
    worker.join(timeout=5)
    assert errors == []


def test_prompt_cancellation_reports_cancelled_not_success(acp_env):
    frames = []

    def after(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": SESSION_ID})
            return
        if method == AgentMethod.SESSION_PROMPT:
            # Wait until the client cancels, then settle as ACP requires.
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if any(frame.get("method") == ProtocolMethod.CANCEL_REQUEST for frame in frames):
                    break
                time.sleep(0.01)
            peer.send_result(request_id, {"stopReason": "cancelled"})
            return
        peer.send_result(request_id, {})

    client, _, peer, workspace = acp_env(initialize_responder(after), on_frame=frames.append)
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    cancel_event = threading.Event()
    cancel_event.set()
    outcome = client.prompt(SESSION_ID, "x", timeout_seconds=5, cancel_event=cancel_event)
    assert outcome.cancelled is True
    assert outcome.stop_reason == AcpStopReason.CANCELLED
    assert any(frame.get("method") == ProtocolMethod.CANCEL_REQUEST for frame in frames)


def test_prompt_timeout_sends_cancel_request(acp_env):
    frames = []

    def after(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": SESSION_ID})
            return
        return  # never settles

    client, _, _, workspace = acp_env(initialize_responder(after), on_frame=frames.append)
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    with pytest.raises(AcpError) as excinfo:
        client.prompt(SESSION_ID, "x", timeout_seconds=0.2)
    assert excinfo.value.code == "timeout"
    assert wait_for(
        lambda: any(frame.get("method") == ProtocolMethod.CANCEL_REQUEST for frame in frames),
        timeout=5,
    )


def test_cancel_sends_session_cancel_notification(acp_env):
    client, _, peer, _ = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    client.cancel(SESSION_ID)
    assert wait_for(
        lambda: any(
            frame.get("method") == AgentMethod.SESSION_CANCEL
            and frame.get("params") == {"sessionId": SESSION_ID}
            for frame in peer.captured_notifications()
        ),
        timeout=5,
    )


def test_cancel_ignores_empty_session_id(acp_env):
    client, _, peer, _ = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    client.cancel("")
    time.sleep(0.05)
    assert peer.captured_notifications() == []


# --- client: permissions ----------------------------------------------------


def permission_responder(*, choose_id=ACP_ALLOW_ONCE_OPTION_ID, session_id=SESSION_ID):
    def after(method, params, request_id, peer):
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": session_id})
            return
        if method != AgentMethod.SESSION_PROMPT:
            peer.send_result(request_id, {})
            return

        # The agent asks for permission *while* the prompt is in flight.
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
        assert answer is not None, "client never answered the permission request"
        outcome = answer["result"]["outcome"]
        peer.session_update(
            session_id,
            {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": {"type": "text", "text": "outcome=%s" % outcome["outcome"]},
            },
        )
        peer.send_result(request_id, {"stopReason": "end_turn"})

    return after


def test_prompt_answers_permission_while_in_flight(acp_env):
    """The transport must answer an inbound request during a blocked prompt."""
    client, _, _, workspace = acp_env(initialize_responder(permission_responder()))
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)

    def answer():
        assert wait_for(lambda: client.pending_permissions(), timeout=10)
        request = client.pending_permissions()[0]
        assert request.session_id == SESSION_ID
        assert request.tool_call_id == "call-1"
        assert request.option_ids == (ACP_ALLOW_ONCE_OPTION_ID, ACP_REJECT_ONCE_OPTION_ID)
        client.respond_permission(request.request_id, ACP_ALLOW_ONCE_OPTION_ID)

    threading.Thread(target=answer, daemon=True).start()
    outcome = client.prompt(SESSION_ID, "run it", timeout_seconds=10)
    assert outcome.stop_reason == AcpStopReason.END_TURN
    # DSH folds allow-once into ACP's "selected" outcome.
    assert "outcome=selected" in outcome.text
    assert client.pending_permissions() == ()


def test_permission_rejection_is_reported_to_the_agent(acp_env):
    client, _, _, workspace = acp_env(initialize_responder(permission_responder()))
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)

    def answer():
        assert wait_for(lambda: client.pending_permissions(), timeout=10)
        client.respond_permission(client.pending_permissions()[0].request_id, ACP_REJECT_ONCE_OPTION_ID)

    threading.Thread(target=answer, daemon=True).start()
    outcome = client.prompt(SESSION_ID, "run it", timeout_seconds=10)
    assert "selected" in outcome.text


def test_cancel_permission_sends_cancelled_outcome(acp_env):
    client, _, _, workspace = acp_env(initialize_responder(permission_responder()))
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)

    def answer():
        assert wait_for(lambda: client.pending_permissions(), timeout=10)
        client.cancel_permission(client.pending_permissions()[0].request_id)

    threading.Thread(target=answer, daemon=True).start()
    client.prompt(SESSION_ID, "run it", timeout_seconds=10)


def test_respond_permission_rejects_unoffered_option(acp_env):
    client, _, _, workspace = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    with pytest.raises(AcpError) as excinfo:
        client.respond_permission("nope", ACP_ALLOW_ONCE_OPTION_ID)
    assert excinfo.value.code == "unknown_permission_request"


def test_permission_request_requires_a_decision_exactly_once(acp_env):
    client, _, _, workspace = acp_env(initialize_responder(permission_responder()))
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)

    def answer():
        assert wait_for(lambda: client.pending_permissions(), timeout=10)
        request_id = client.pending_permissions()[0].request_id
        assert client.respond_permission(request_id, ACP_ALLOW_ONCE_OPTION_ID) is True
        with pytest.raises(AcpError):
            client.respond_permission(request_id, ACP_ALLOW_ONCE_OPTION_ID)

    threading.Thread(target=answer, daemon=True).start()
    client.prompt(SESSION_ID, "run it", timeout_seconds=10)


def test_permission_listener_is_notified_once_and_cannot_break_the_protocol(acp_env):
    seen = []

    def listener(request):
        seen.append(request)
        raise RuntimeError("projection failed")

    client, _, _, workspace = acp_env(
        initialize_responder(permission_responder()),
        on_permission=listener,
    )
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)

    def answer():
        assert wait_for(lambda: client.pending_permissions(), timeout=10)
        request = client.pending_permissions()[0]
        client.respond_permission(request.request_id, ACP_ALLOW_ONCE_OPTION_ID)

    threading.Thread(target=answer, daemon=True).start()
    outcome = client.prompt(SESSION_ID, "run it", timeout_seconds=10)

    assert [request.request_id for request in seen] == ["perm-1"]
    # A raising listener is a projection bug, not a protocol failure.
    assert outcome.stop_reason == AcpStopReason.END_TURN
    assert "outcome=selected" in outcome.text
    assert client.pending_permissions() == ()


def test_permission_listener_is_absent_by_default(acp_env):
    client, _, _, _ = acp_env(initialize_responder(permission_responder()))

    assert client.on_permission is None


# --- normalization ----------------------------------------------------------


def test_normalize_text_and_thinking_chunks():
    text = normalize_session_update(
        {
            "sessionId": SESSION_ID,
            "update": {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": {"type": "text", "text": "hi"},
                "messageId": "m1",
            },
        },
        max_text_bytes=1024,
    )
    assert text.type == "text"
    assert text.data == {"text": "hi", "message_id": "m1"}

    thinking = normalize_session_update(
        {
            "sessionId": SESSION_ID,
            "update": {
                "sessionUpdate": SessionUpdateKind.AGENT_THOUGHT_CHUNK,
                "content": {"type": "text", "text": "hmm"},
            },
        },
        max_text_bytes=1024,
    )
    assert thinking.type == "thinking"
    assert thinking.data == {"text": "hmm"}


def test_normalize_chunk_truncates_and_marks():
    event = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": {"type": "text", "text": "abcdefghij"},
            }
        },
        max_text_bytes=4,
    )
    assert event.truncated is True
    assert event.data["text"] == "abcd"


def test_normalize_tool_call_uses_title_as_name_and_accepts_raw_input():
    event = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.TOOL_CALL,
                "toolCallId": "call-1",
                "title": "Bash",
                "kind": "other",
                "status": "in_progress",
                "rawInput": {"command": "ls"},
            }
        },
        max_text_bytes=1024,
    )
    assert event.type == "tool_call"
    assert event.data["call_id"] == "call-1"
    assert event.data["tool"] == "Bash"
    assert event.data["input"] == {"command": "ls"}

    # DSH preserves malformed model JSON as an opaque string.
    as_text = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.TOOL_CALL,
                "toolCallId": "call-2",
                "title": "Bash",
                "rawInput": "{not json",
            }
        },
        max_text_bytes=1024,
    )
    assert as_text.data["input"] == "{not json"


def test_normalize_tool_result_maps_failed_status_to_error():
    completed = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.TOOL_CALL_UPDATE,
                "toolCallId": "call-1",
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "ok"}}],
            }
        },
        max_text_bytes=1024,
    )
    assert completed.type == "tool_result"
    assert completed.data["status"] == "completed"
    assert completed.data["result"] == "ok"

    failed = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.TOOL_CALL_UPDATE,
                "toolCallId": "call-1",
                "status": "failed",
                "content": [{"type": "content", "content": {"type": "text", "text": "boom"}}],
            }
        },
        max_text_bytes=1024,
    )
    assert failed.data["status"] == "error"
    assert failed.data["result"] == "boom"


def test_normalize_usage_and_config_updates():
    usage = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.USAGE_UPDATE,
                "used": 100,
                "size": 1000,
            }
        },
        max_text_bytes=1024,
    )
    assert usage.type == "status" and usage.phase == "usage"
    assert usage.data["usage"] == {"used": 100, "size": 1000}

    config = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.CONFIG_OPTION_UPDATE,
                "configOptions": [{"id": "model", "currentValue": "m"}],
            }
        },
        max_text_bytes=1024,
    )
    assert config.type == "status" and config.phase == "config"
    assert config.data["config_options"][0]["id"] == "model"


def test_normalize_unknown_variant_is_preserved_not_fatal():
    event = normalize_session_update(
        {
            "sessionId": SESSION_ID,
            "update": {"sessionUpdate": "plan", "entries": [{"content": "step"}]},
        },
        max_text_bytes=1024,
    )
    assert event.type == "unknown"
    assert event.source_type == "plan"
    assert event.data["session_id"] == SESSION_ID
    assert event.data["payload"] == {"entries": [{"content": "step"}]}


def test_normalize_redacts_sensitive_values():
    event = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.TOOL_CALL,
                "toolCallId": "call-1",
                "title": "Bash",
                "rawInput": {"api_key": "sk-should-not-leak", "command": "echo token=hunter2"},
            }
        },
        max_text_bytes=1024,
    )
    rendered = json.dumps(event.to_dict())
    assert "sk-should-not-leak" not in rendered
    assert "hunter2" not in rendered


@pytest.mark.parametrize("params", [None, {}, {"update": "nope"}, {"update": {}}, {"update": {"sessionUpdate": ""}}])
def test_normalize_rejects_non_update_payloads(params):
    assert normalize_session_update(params, max_text_bytes=1024) is None


def test_normalized_events_are_headless_compatible():
    event = normalize_session_update(
        {
            "update": {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": {"type": "text", "text": "x"},
            }
        },
        max_text_bytes=1024,
    )
    assert isinstance(event, DshRuntimeEvent)
    assert set(event.to_dict()) <= {"type", "data", "phase", "source_type", "truncated"}


# --- non-prompt notification drain ------------------------------------------


def test_take_notifications_drains_updates_outside_a_prompt(acp_env):
    client, _, peer, _ = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    peer.session_update(
        SESSION_ID,
        {
            "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
            "content": {"type": "text", "text": "unsolicited"},
        },
    )
    events = []

    def collect():
        events.extend(client.take_notifications(timeout=0.1))
        return bool(events)

    assert wait_for(collect, timeout=5)
    assert events[0].type == "text"


def test_orphan_response_is_queued_not_lost(acp_env):
    client, transport, peer, _ = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    peer.send_result("never-requested", {"ok": True})
    assert wait_for(lambda: bool(transport.drain_inbox()), timeout=5)


# --- inbox starvation -------------------------------------------------------


def interleaved_prompt_responder(*, on_update=None, chunks=6, session_id=SESSION_ID):
    """Send updates, then the result, calling ``on_update`` after the first."""
    fired = False

    def responder(method, params, request_id, peer):
        nonlocal fired
        if method == AgentMethod.SESSION_NEW:
            peer.send_result(request_id, {"sessionId": session_id})
            return
        if method != AgentMethod.SESSION_PROMPT:
            peer.send_result(request_id, {})
            return
        for index in range(chunks):
            peer.session_update(
                session_id,
                {
                    "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "c%d;" % index},
                    "messageId": "m1",
                },
            )
            if index == 0 and on_update is not None and not fired:
                fired = True
                on_update()
        peer.send_result(request_id, {"stopReason": "end_turn"})

    return responder


def test_prompt_owns_its_updates_while_another_thread_drains_the_inbox(acp_env):
    """The settled outcome must not depend on who wins a notification race.

    A projection layer (the backend's monitor thread) legitimately drains
    unsolicited updates while a prompt is in flight.  Draining the same inbox
    from both sides used to split the turn between them, so the outcome came
    back incomplete.  The turn now owns its own subscribed stream.
    """
    consumed = threading.Event()

    def first_update_sent():
        consumed.set()

    client, transport, peer, workspace = acp_env(
        initialize_responder(interleaved_prompt_responder(on_update=first_update_sent, chunks=6))
    )
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)

    drained: list = []
    stop = threading.Event()

    def drain_like_a_monitor():
        while not stop.is_set():
            drained.extend(client.take_notifications(timeout=0.02))

    watcher = threading.Thread(target=drain_like_a_monitor, daemon=True)
    watcher.start()
    outcome: list = []
    failure: list = []

    def turn():
        try:
            outcome.append(client.prompt(SESSION_ID, "x", timeout_seconds=5))
        except Exception as exc:  # pragma: no cover - surfaced below
            failure.append(exc)

    worker = threading.Thread(target=turn, daemon=True)
    try:
        worker.start()
        # Once the peer has sent its first update, the monitor is actively
        # draining the same inbox the turn used to depend on.
        assert consumed.wait(5)
        worker.join(timeout=10)
    finally:
        stop.set()
        watcher.join(timeout=2)

    assert failure == []
    assert not worker.is_alive()
    settled = outcome[0]
    assert settled.stop_reason == AcpStopReason.END_TURN
    assert [event.type for event in settled.updates] == ["text"] * 6 + ["final"]
    assert settled.text == "".join("c%d;" % index for index in range(6))
    # The drainer never saw a subscribed turn's chunk: those belong to the turn.
    assert [event for event in drained if event.type == "text"] == []


def test_another_consumer_cannot_steal_a_subscribed_turn(acp_env):
    """A chunk taken elsewhere must still appear in the turn's own outcome."""
    emitted = threading.Event()

    def first_chunk_sent():
        emitted.set()

    client, transport, peer, workspace = acp_env(
        initialize_responder(interleaved_prompt_responder(on_update=first_chunk_sent, chunks=2))
    )
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)

    drained: list = []
    outcome: list = []
    failure: list = []

    def monitor():
        # Exactly what the backend's projection thread does: consume the shared
        # inbox for the whole duration of the turn.
        while not emitted.is_set():
            drained.extend(client.take_notifications(timeout=0.02))

    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()

    def turn():
        try:
            outcome.append(client.prompt(SESSION_ID, "x", timeout_seconds=5))
        except BaseException as exc:  # pragma: no cover - surfaced below
            failure.append(exc)

    worker = threading.Thread(target=turn, daemon=True)
    try:
        worker.start()
        assert emitted.wait(5)
        worker.join(timeout=10)
    finally:
        watcher.join(timeout=5)

    assert failure == []
    assert not worker.is_alive()
    settled = outcome[0]
    # The turn reports every chunk exactly once, no matter who else was reading.
    assert [event.data.get("text") for event in settled.updates if event.type == "text"] == [
        "c0;",
        "c1;",
    ]
    assert settled.text == "c0;c1;"
    # And the other consumer cannot have seen one it did not own.
    assert [event for event in drained if event.type == "text"] == []


def test_prompt_reports_every_update_through_on_event_in_wire_order(acp_env):
    observed: list[str] = []
    client, _, _, workspace = acp_env(
        initialize_responder(
            interleaved_prompt_responder(
                chunks=3,
            )
        )
    )
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)

    outcome = client.prompt(
        SESSION_ID,
        "x",
        timeout_seconds=5,
        on_event=lambda event: observed.append("%s:%s" % (event.type, event.data.get("text") or "")),
    )

    assert observed == ["text:c0;", "text:c1;", "text:c2;", "final:c0;c1;c2;"]
    assert [event.type for event in outcome.updates] == ["text", "text", "text", "final"]
    assert outcome.text == "c0;c1;c2;"


def test_on_event_streams_while_the_prompt_is_still_blocked(acp_env):
    """Live projection must not wait for the whole turn to settle."""
    streamed = threading.Event()
    answered = threading.Event()

    def after_first_update():
        streamed.set()
        # The peer withholds the response until the observer has seen the chunk,
        # so a callback delivered only after settlement would deadlock here.
        assert answered.wait(5)

    client, _, _, workspace = acp_env(
        initialize_responder(interleaved_prompt_responder(on_update=after_first_update, chunks=2))
    )
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    seen: list[str] = []

    def observe(event):
        seen.append(event.data.get("text") or "")
        if event.data.get("text") == "c0;":
            answered.set()

    outcome = client.prompt(SESSION_ID, "x", timeout_seconds=10, on_event=observe)

    assert streamed.is_set()
    assert seen == ["c0;", "c1;", "c0;c1;"]
    assert outcome.text == "c0;c1;"


def test_prompt_claims_updates_queued_before_the_turn_started(acp_env):
    """Opening the subscription must not leave a leading hole in the turn."""
    client, _, peer, workspace = acp_env(
        initialize_responder(interleaved_prompt_responder(chunks=2))
    )
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    peer.session_update(
        SESSION_ID,
        {
            "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
            "content": {"type": "text", "text": "queued;"},
            "messageId": "m0",
        },
    )
    # Let the frame reach the inbox but leave it unconsumed.
    assert wait_for(lambda: bool(transport_inbox(client)), timeout=5)

    outcome = client.prompt(SESSION_ID, "x", timeout_seconds=5)

    assert [event.data.get("text") for event in outcome.updates if event.type == "text"] == [
        "queued;",
        "c0;",
        "c1;",
    ]
    assert outcome.text == "queued;c0;c1;"


def transport_inbox(client):
    """Peek at the shared inbox without consuming a subscribed turn."""
    with client.transport._state:
        return list(client.transport._inbox)


def test_subscription_is_released_when_the_transport_closes(acp_env):
    client, transport, peer, workspace = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    subscription = transport.open_session_updates(SESSION_ID)

    transport.close(terminate=True)

    assert subscription.close() == []
    with pytest.raises(AcpError) as exc_info:
        transport.open_session_updates(SESSION_ID)
    assert exc_info.value.code == "closed"


def test_second_subscription_for_one_session_is_refused(acp_env):
    """One prompt per session is already the contract; the transport enforces it."""
    client, transport, peer, workspace = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    transport.open_session_updates(SESSION_ID)

    with pytest.raises(AcpError) as exc_info:
        transport.open_session_updates(SESSION_ID)
    assert exc_info.value.code == "session_subscribed"


def test_subscription_buffer_is_bounded(acp_env):
    """A pathological turn is bounded and reports what it dropped."""
    client, transport, peer, workspace = acp_env(initialize_responder())
    client.initialize(timeout_seconds=5)
    client.new_session(workspace, timeout_seconds=5)
    subscription = transport.open_session_updates(SESSION_ID)
    frame = AcpMessage(
        {
            "jsonrpc": "2.0",
            "method": ClientMethod.SESSION_UPDATE,
            "params": {"sessionId": SESSION_ID, "update": {"sessionUpdate": "plan"}},
        }
    )

    with transport._state:
        for _ in range(ACP_MAX_SUBSCRIBED_UPDATES + 5):
            transport._claim_update_locked(subscription, frame)

    assert subscription.dropped == 5
    assert len(subscription.drain()) == ACP_MAX_SUBSCRIBED_UPDATES
