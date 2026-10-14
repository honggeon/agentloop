import io
import json
import subprocess
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from auditrail.dsh_headless import (
    DshHeadlessClient,
    DshProcessOutput,
    build_headless_argv,
    parse_headless_line,
    run_subprocess,
)
from auditrail.dsh_runtime import (
    DshProtocolError,
    DshRunRequest,
    DshRuntimeConfig,
    DshValidationError,
)


SESSION_ID = "session-12345678-1234-1234-1234-123456789abc"


class FakeRunner:
    def __init__(self, output=None, error=None):
        self.output = output
        self.error = error
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        if self.error is not None:
            raise self.error
        return self.output


def make_client(tmp_path, output, **config_overrides):
    root = tmp_path / "root"
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    values = {
        "binary": Path("/opt/dsh/bin/dsh"),
        "allowed_profiles": ("headless",),
        "allowed_workspace_roots": (root,),
        "default_timeout_seconds": 30,
        "max_timeout_seconds": 60,
    }
    values.update(config_overrides)
    normalized_lines = []
    for line in output.stdout_lines:
        if isinstance(line, bytes):
            normalized_lines.append(line.replace(b"/workspace", str(workspace).encode("utf-8")))
        else:
            normalized_lines.append(line.replace("/workspace", str(workspace)))
    output = replace(output, stdout_lines=tuple(normalized_lines))
    runner = FakeRunner(output)
    return DshHeadlessClient(DshRuntimeConfig(**values), runner), runner, workspace


def lines(*payloads):
    return tuple(json.dumps(payload, ensure_ascii=False) + "\n" for payload in payloads)


def successful_output(*extra_events, exit_code=0, stderr=b""):
    return DshProcessOutput(
        lines(
            {"type": "session", "sessionId": SESSION_ID, "cwd": "/workspace"},
            {"type": "status", "phase": "turn_start", "turn": 1},
            *extra_events,
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
            {"type": "final", "text": "final answer"},
        ),
        stderr=stderr,
        exit_code=exit_code,
    )


def test_default_runner_never_uses_a_shell(monkeypatch, tmp_path):
    captured = {}

    class FakeProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            self.stdin = None
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    def fake_popen(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("auditrail.dsh_headless.subprocess.Popen", fake_popen)
    output = run_subprocess(
        ("/opt/dsh/bin/dsh", "--json", "hello"),
        cwd=tmp_path,
        timeout_seconds=1,
        cancel_event=None,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )

    assert captured["argv"] == ["/opt/dsh/bin/dsh", "--json", "hello"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    assert output.exit_code == 0


def test_default_runner_terminates_process_group_on_timeout(monkeypatch, tmp_path):
    stopped = []

    class BlockingProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"")
            self.stderr = io.BytesIO(b"")
            self.stdin = None
            self.returncode = None
            self.pid = 4321

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired(["dsh"], timeout)
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    process = BlockingProcess()
    monkeypatch.setattr("auditrail.dsh_headless.subprocess.Popen", lambda *args, **kwargs: process)

    def fake_killpg(pid, sig):
        stopped.append((pid, sig))
        process.returncode = -15

    monkeypatch.setattr("auditrail.dsh_headless.os.killpg", fake_killpg)
    output = run_subprocess(
        ("/opt/dsh/bin/dsh", "--json", "hello"),
        cwd=tmp_path,
        timeout_seconds=0.001,
        cancel_event=None,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
    )

    assert output.timed_out is True
    assert stopped == [(4321, 15)]


def test_blocked_stdout_callback_cannot_defeat_timeout(monkeypatch, tmp_path):
    release_callback = threading.Event()

    class BlockingProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b'{"type":"text","text":"ready"}\n')
            self.stderr = io.BytesIO(b"")
            self.stdin = None
            self.returncode = None
            self.pid = 9876

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired(["dsh"], timeout)
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    process = BlockingProcess()
    monkeypatch.setattr("auditrail.dsh_headless.subprocess.Popen", lambda *args, **kwargs: process)

    def fake_killpg(pid, sig):
        process.returncode = -15

    monkeypatch.setattr("auditrail.dsh_headless.os.killpg", fake_killpg)
    output = run_subprocess(
        ("/opt/dsh/bin/dsh", "--json", "hello"),
        cwd=tmp_path,
        timeout_seconds=0.001,
        cancel_event=None,
        max_stdout_bytes=1024,
        max_stderr_bytes=1024,
        on_stdout_line=lambda line: release_callback.wait(),
    )
    release_callback.set()

    assert output.timed_out is True


def test_default_runner_bounds_stdout_without_a_newline(monkeypatch, tmp_path):
    class FinishedProcess:
        def __init__(self):
            self.stdout = io.BytesIO(b"x" * 100)
            self.stderr = io.BytesIO(b"")
            self.stdin = None
            self.returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(
        "auditrail.dsh_headless.subprocess.Popen",
        lambda *args, **kwargs: FinishedProcess(),
    )
    output = run_subprocess(
        ("/opt/dsh/bin/dsh", "--json", "hello"),
        cwd=tmp_path,
        timeout_seconds=1,
        cancel_event=None,
        max_stdout_bytes=20,
        max_stderr_bytes=20,
    )
    assert output.stdout_limit_exceeded is True
    assert tuple(output.stdout_lines) == ()


def test_builds_exact_new_and_resumed_argv(tmp_path):
    output = successful_output()
    client, runner, workspace = make_client(tmp_path, output)

    request = DshRunRequest("run tests", "headless", workspace)
    assert build_headless_argv(client.config, request) == (
        "/opt/dsh/bin/dsh",
        "--profile",
        "headless",
        "--json",
        "run tests",
    )
    client.run(request)
    assert runner.calls[0][0] == build_headless_argv(client.config, request)
    assert runner.calls[0][1]["cwd"] == workspace.resolve()

    resumed = DshRunRequest("continue", "headless", workspace, session_id=SESSION_ID)
    assert build_headless_argv(client.config, resumed) == (
        "/opt/dsh/bin/dsh",
        "--profile",
        "headless",
        "--json",
        "--session-id",
        SESSION_ID,
        "continue",
    )


def test_events_stream_before_runner_returns(tmp_path):
    client, static_runner, workspace = make_client(tmp_path, successful_output())
    stream_lines = tuple(static_runner.output.stdout_lines)
    lifecycle = []

    class StreamingRunner:
        def __call__(self, argv, *, on_stdout_line, **kwargs):
            lifecycle.append("runner_started")
            for line in stream_lines:
                on_stdout_line(line.encode("utf-8") if isinstance(line, str) else line)
            lifecycle.append("runner_returning")
            return DshProcessOutput((), exit_code=0)

    client.runner = StreamingRunner()
    result = client.run(
        DshRunRequest("hello", "headless", workspace),
        on_event=lambda event: lifecycle.append(event.type),
    )

    assert result.succeeded is True
    assert lifecycle[0] == "runner_started"
    assert lifecycle.index("session") < lifecycle.index("runner_returning")
    assert lifecycle.index("final") < lifecycle.index("runner_returning")


def test_failing_event_callback_returns_structured_error(tmp_path):
    client, _, workspace = make_client(tmp_path, successful_output())

    def fail_callback(_event):
        raise RuntimeError("consumer failed")

    result = client.run(DshRunRequest("hello", "headless", workspace), on_event=fail_callback)

    assert result.succeeded is False
    assert result.error.code == "event_callback_failed"
    assert result.events[-1].type == "error"


def test_leading_dash_message_is_positional_not_a_flag(tmp_path):
    client, _, workspace = make_client(tmp_path, successful_output())
    argv = build_headless_argv(client.config, DshRunRequest("--patch malicious", "headless", workspace))
    assert argv[-2:] == ("--", "--patch malicious")
    assert argv.count("--patch") == 0

    literal_dash = build_headless_argv(client.config, DshRunRequest("-", "headless", workspace))
    assert literal_dash[-2:] == ("--", "-")


def test_normalizes_all_documented_event_types_and_tool_inputs(tmp_path):
    output = successful_output(
        {"type": "status", "phase": "step_start", "turn": 1, "step": 1},
        {"type": "thinking", "text": "reason"},
        {"type": "text", "text": "working"},
        {"type": "tool_call", "callId": "a", "tool": "bash", "input": {"command": "pwd"}},
        {"type": "tool_call", "callId": "b", "tool": "legacy", "input": "raw text"},
        {"type": "tool_result", "callId": "a", "status": "completed", "result": "ok"},
        {"type": "status", "phase": "step_end", "turn": 1, "step": 1, "usage": {"totalTokens": 3}},
    )
    client, _, workspace = make_client(tmp_path, output)
    seen = []
    result = client.run(DshRunRequest("hello", "headless", workspace), on_event=seen.append)

    assert result.succeeded is True
    assert result.session_id == SESSION_ID
    assert result.final_text == "final answer"
    assert result.turn_end_reason == "completed"
    assert [event.type for event in result.events] == [
        "session",
        "status",
        "status",
        "thinking",
        "text",
        "tool_call",
        "tool_call",
        "tool_result",
        "status",
        "status",
        "final",
    ]
    tool_calls = [event for event in result.events if event.type == "tool_call"]
    assert tool_calls[0].data["input"] == {"command": "pwd"}
    assert tool_calls[1].data["input"] == "raw text"
    assert seen == list(result.events)


def test_unknown_events_and_truncation_markers_are_preserved():
    event = parse_headless_line(
        json.dumps({"type": "future_event", "value": 3, "truncated": True}),
        line_number=1,
        max_event_bytes=1024,
    )
    assert event.type == "unknown"
    assert event.source_type == "future_event"
    assert event.data["payload"]["value"] == 3
    assert event.truncated is True


def test_unknown_event_payloads_are_secret_redacted():
    event = parse_headless_line(
        json.dumps(
            {
                "type": "future_event",
                "authorization": "Bearer top-secret-token",
                "api_key": "opaque-value-without-token-syntax",
                "url": "https://user:password@example.test/path",
            }
        ),
        line_number=1,
        max_event_bytes=1024,
    )
    rendered = json.dumps(event.data)
    assert "top-secret-token" not in rendered
    assert "opaque-value-without-token-syntax" not in rendered
    assert "user:password" not in rendered
    assert "[redacted]" in rendered


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "tool_call", "input": {}},
        {"type": "tool_result", "callId": "a", "status": "bogus", "result": "ok"},
        {"type": "status", "phase": "turn_start"},
        {"type": "status", "phase": "step_start", "turn": 1},
        {"type": "status", "phase": "turn_end", "turn": 1},
    ],
)
def test_malformed_known_events_fail_explicitly(payload):
    with pytest.raises(DshProtocolError, match="DSH"):
        parse_headless_line(json.dumps(payload), line_number=1, max_event_bytes=1024)


def test_lossless_final_can_use_aggregate_stdout_bound():
    text = "x" * 100
    event = parse_headless_line(
        json.dumps({"type": "final", "text": text}),
        line_number=1,
        max_event_bytes=32,
        max_final_bytes=1024,
    )
    assert event.type == "final"
    assert event.data["text"] == text


def test_event_after_final_is_a_protocol_failure(tmp_path):
    output = DshProcessOutput(
        lines(
            {"type": "session", "sessionId": SESSION_ID, "cwd": "/workspace"},
            {"type": "final", "text": "answer"},
            {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
        ),
        exit_code=0,
    )
    client, _, workspace = make_client(tmp_path, output)
    result = client.run(DshRunRequest("hello", "headless", workspace))
    assert result.error.code == "event_after_final"


def test_invalid_emitted_session_id_is_a_protocol_failure(tmp_path):
    output = DshProcessOutput(lines({"type": "session", "sessionId": "../secret", "cwd": "/workspace"}))
    client, _, workspace = make_client(tmp_path, output)
    result = client.run(DshRunRequest("hello", "headless", workspace))
    assert result.error.code == "malformed_event"
    assert "../secret" not in result.error.message


@pytest.mark.parametrize(
    ("output", "error_code"),
    [
        (DshProcessOutput(("{bad json\n",), exit_code=0), "malformed_ndjson"),
        (DshProcessOutput(lines({"type": "error", "message": "boom"}), exit_code=1), "dsh_error"),
        (successful_output(exit_code=1), "nonzero_exit"),
        (
            DshProcessOutput(
                lines(
                    {"type": "session", "sessionId": SESSION_ID, "cwd": "/workspace"},
                    {"type": "status", "phase": "turn_end", "turn": 1, "reason": {"kind": "completed"}},
                ),
                exit_code=0,
            ),
            "missing_final",
        ),
        (
            DshProcessOutput(
                lines(
                    {"type": "session", "sessionId": SESSION_ID, "cwd": "/workspace"},
                    {"type": "final", "text": "answer"},
                ),
                exit_code=0,
            ),
            "missing_turn_end",
        ),
    ],
)
def test_terminal_inconsistencies_are_explicit(tmp_path, output, error_code):
    client, _, workspace = make_client(tmp_path, output)
    result = client.run(DshRunRequest("hello", "headless", workspace))
    assert result.succeeded is False
    assert result.error.code == error_code
    assert result.events[-1].type == "error"


@pytest.mark.parametrize(
    ("output", "code", "timed_out", "cancelled"),
    [
        (DshProcessOutput((), exit_code=-15, timed_out=True), "timeout", True, False),
        (DshProcessOutput((), exit_code=-15, cancelled=True), "cancelled", False, True),
    ],
)
def test_timeout_and_cancellation_are_not_success(tmp_path, output, code, timed_out, cancelled):
    client, _, workspace = make_client(tmp_path, output)
    result = client.run(DshRunRequest("hello", "headless", workspace))
    assert result.error.code == code
    assert result.timed_out is timed_out
    assert result.cancelled is cancelled
    assert result.succeeded is False


def test_stdout_and_event_bounds_fail_explicitly(tmp_path):
    output = DshProcessOutput((b'x' * 21,), exit_code=-15, stdout_limit_exceeded=True)
    client, _, workspace = make_client(
        tmp_path,
        output,
        max_event_bytes=10,
        max_stdout_bytes=20,
    )
    result = client.run(DshRunRequest("hello", "headless", workspace))
    assert result.error.code == "stdout_limit"


def test_quoted_secrets_with_spaces_are_fully_redacted(tmp_path):
    secret = "top secret value"
    event = parse_headless_line(
        json.dumps({"type": "error", "message": f'password="{secret}"'}),
        line_number=1,
        max_event_bytes=1024,
    )
    output = successful_output(stderr=f'API_KEY="{secret}"'.encode())
    client, _, workspace = make_client(tmp_path, output)
    result = client.run(DshRunRequest("hello", "headless", workspace))

    assert secret not in event.data["message"]
    assert secret not in result.stderr_diagnostics
    assert "[redacted]" in event.data["message"]
    assert "[redacted]" in result.stderr_diagnostics


def test_stderr_is_truncated_and_secrets_are_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_API_KEY", "highly-secret-value")
    output = successful_output(stderr=b"API_KEY=highly-secret-value and more diagnostic data")
    client, _, workspace = make_client(tmp_path, output, max_stderr_bytes=24)
    result = client.run(DshRunRequest("hello", "headless", workspace))

    assert "highly-secret-value" not in result.stderr_diagnostics
    assert "[redacted]" in result.stderr_diagnostics
    assert result.stderr_diagnostics.endswith("...[stderr truncated]")


def test_runner_exception_does_not_echo_secret_or_message(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVICE_TOKEN", "runtime-secret-value")
    client, runner, workspace = make_client(tmp_path, successful_output())
    runner.error = RuntimeError("runtime-secret-value -- user prompt")
    result = client.run(DshRunRequest("private user prompt", "headless", workspace))

    assert result.error.code == "spawn_failed"
    assert "runtime-secret-value" not in result.error.message
    assert "private user prompt" not in result.error.message


def test_invalid_request_never_reaches_runner(tmp_path):
    client, runner, workspace = make_client(tmp_path, successful_output())
    with pytest.raises(DshValidationError):
        client.run(DshRunRequest("hello", "--patch", workspace))
    with pytest.raises(DshValidationError):
        client.run(DshRunRequest("hello", "headless", workspace, session_id="../bad"))
    assert runner.calls == []
