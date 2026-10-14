"""Safe DSH Headless invocation and NDJSON normalization."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Protocol, Sequence

from .dsh_runtime import (
    DshExecutionResult,
    DshInvocation,
    DshProtocolError,
    DshRunRequest,
    DshRuntimeConfig,
    DshRuntimeError,
    DshRuntimeEvent,
    DshValidationError,
    validate_request,
    validate_session_id,
)


@dataclass(frozen=True)
class DshProcessOutput:
    """Bounded process output returned by an injectable runner."""

    stdout_lines: Iterable[str | bytes]
    stderr: str | bytes = b""
    exit_code: Optional[int] = 0
    timed_out: bool = False
    cancelled: bool = False
    stdout_limit_exceeded: bool = False
    stderr_truncated: bool = False
    stream_error: Optional[DshRuntimeError] = None


class DshProcessRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        cancel_event: Optional[threading.Event],
        max_stdout_bytes: int,
        max_stderr_bytes: int,
        on_stdout_line: Optional[Callable[[bytes], None]],
    ) -> DshProcessOutput: ...


_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    r'''(?i)(["']?\b(?:token|password|secret|api[_-]?key|credential)\b["']?(?:\s*[:=]\s*|\s+))(?:"[^"]*"|'[^']*')'''
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key|credential)\b(\s*(?::|=|\s)\s*)([^\s,;\"']+)"
)
_BEARER_RE = re.compile(r"(?i)(\b(?:authorization\s*:\s*)?bearer\s+)([^\s,;]+)")
_URL_CREDENTIAL_RE = re.compile(r"(?i)\b(https?://)([^/\s:@]+):([^/\s@]+)@")
_SENSITIVE_KEY_RE = re.compile(r"(?i)^(?:authorization|token|password|secret|api[_-]?key|credential)$")
_STATUS_PHASES = {"turn_start", "step_start", "step_end", "turn_end"}
_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def build_headless_argv(config: DshRuntimeConfig, request: DshRunRequest) -> tuple[str, ...]:
    """Validate a request and construct the only supported Headless argv."""
    invocation = validate_request(config, request)
    return _argv_for_invocation(config, invocation)


def _argv_for_invocation(config: DshRuntimeConfig, invocation: DshInvocation) -> tuple[str, ...]:
    argv = [str(config.binary), "--profile", invocation.profile, "--json"]
    if invocation.session_id is not None:
        argv.extend(("--session-id", invocation.session_id))
    # Commander treats a leading '-' as an option unless the positional is
    # separated explicitly. Normal messages retain the documented short argv.
    if invocation.message.startswith("-"):
        argv.append("--")
    argv.append(invocation.message)
    return tuple(argv)


def run_subprocess(
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout_seconds: float,
    cancel_event: Optional[threading.Event],
    max_stdout_bytes: int,
    max_stderr_bytes: int,
    on_stdout_line: Optional[Callable[[bytes], None]] = None,
) -> DshProcessOutput:
    """Run one owned process without a shell and stream bounded NDJSON lines."""
    if os.name != "posix":
        raise OSError("local DSH runtime requires POSIX process-group isolation")
    if cancel_event is not None and cancel_event.is_set():
        return DshProcessOutput((), exit_code=None, cancelled=True)

    stdin_payload = b"-" if argv and argv[-1] == "-" else None
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover - Popen contract
        _terminate_process_tree(process)
        raise OSError("DSH process pipes were not created")
    if stdin_payload is not None:
        if process.stdin is None:  # pragma: no cover - Popen contract
            _terminate_process_tree(process)
            raise OSError("DSH process stdin was not created")
        try:
            process.stdin.write(stdin_payload)
            process.stdin.close()
        except OSError:
            _terminate_process_tree(process)
            raise

    stdout_lines: list[bytes] = []
    stderr_bytes = bytearray()
    stdout_count = 0
    stderr_count = 0
    stdout_limit_exceeded = False
    stderr_truncated = False
    stream_error: Optional[DshRuntimeError] = None
    output_lock = threading.Lock()
    stop_requested = threading.Event()

    def deliver_line(line: bytes) -> bool:
        nonlocal stream_error
        if on_stdout_line is None:
            stdout_lines.append(line)
            return True
        try:
            on_stdout_line(line)
            return True
        except DshProtocolError as exc:
            stream_error = DshRuntimeError(exc.code, str(exc))
        except Exception:
            stream_error = DshRuntimeError("event_callback_failed", "DSH event consumer failed")
        stop_requested.set()
        return False

    def read_stdout() -> None:
        nonlocal stdout_count, stdout_limit_exceeded
        pending = bytearray()
        read_chunk = getattr(process.stdout, "read1", process.stdout.read)
        while True:
            try:
                chunk = read_chunk(8192)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            ready: list[bytes] = []
            with output_lock:
                stdout_count += len(chunk)
                if stdout_count > max_stdout_bytes:
                    stdout_limit_exceeded = True
                    stop_requested.set()
                    return
                pending.extend(chunk)
                while True:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        break
                    ready.append(bytes(pending[: newline + 1]))
                    del pending[: newline + 1]
            for line in ready:
                if not deliver_line(line):
                    return
        with output_lock:
            final_line = bytes(pending) if pending else None
        if final_line is not None:
            deliver_line(final_line)

    def read_stderr() -> None:
        nonlocal stderr_count, stderr_truncated
        for chunk in iter(lambda: process.stderr.read(8192), b""):
            with output_lock:
                remaining = max_stderr_bytes - len(stderr_bytes)
                if remaining > 0:
                    stderr_bytes.extend(chunk[:remaining])
                stderr_count += len(chunk)
                if stderr_count > max_stderr_bytes:
                    stderr_truncated = True

    stdout_thread = threading.Thread(target=read_stdout, name="dsh-stdout", daemon=True)
    stderr_thread = threading.Thread(target=read_stderr, name="dsh-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()

    timed_out = False
    cancelled = False
    deadline = time.monotonic() + timeout_seconds
    while process.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            cancelled = True
            _terminate_process_tree(process)
            break
        if stop_requested.is_set():
            _terminate_process_tree(process)
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process_tree(process)
            break
        try:
            process.wait(timeout=min(0.05, remaining))
        except subprocess.TimeoutExpired:
            continue

    if process.poll() is None:
        _terminate_process_tree(process)
    stdout_thread.join(timeout=0.2)
    stderr_thread.join(timeout=0.2)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        _terminate_process_tree(process, force=True)
        try:
            process.stdout.close()
            process.stderr.close()
        except OSError:
            pass
        stdout_thread.join(timeout=1.0)
        stderr_thread.join(timeout=1.0)
    with output_lock:
        return DshProcessOutput(
            tuple(stdout_lines),
            bytes(stderr_bytes),
            process.returncode,
            timed_out=timed_out,
            cancelled=cancelled,
            stdout_limit_exceeded=stdout_limit_exceeded,
            stderr_truncated=stderr_truncated,
            stream_error=stream_error,
        )


def _terminate_process_tree(process: subprocess.Popen[bytes], *, force: bool = False) -> None:
    """Stop the isolated DSH process group, including spawned tool processes."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            if process.poll() is None:
                (process.kill if force else process.terminate)()
    elif process.poll() is None:
        (process.kill if force else process.terminate)()
    try:
        process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        if not force:
            _terminate_process_tree(process, force=True)
    except OSError:
        pass


class DshHeadlessClient:
    """Execute one validated DSH Headless turn through an injectable runner."""

    def __init__(self, config: DshRuntimeConfig, runner: DshProcessRunner = run_subprocess):
        self.config = config
        self.runner = runner

    def run(
        self,
        request: DshRunRequest,
        *,
        cancel_event: Optional[threading.Event] = None,
        on_event: Optional[Callable[[DshRuntimeEvent], None]] = None,
        invocation: Optional[DshInvocation] = None,
    ) -> DshExecutionResult:
        if invocation is None:
            invocation = validate_request(self.config, request)
        argv = _argv_for_invocation(self.config, invocation)
        events: list[DshRuntimeEvent] = []
        session_id: Optional[str] = None
        final_text: Optional[str] = None
        turn_end_reason: Optional[str] = None
        source_error: Optional[DshRuntimeError] = None
        protocol_error: Optional[DshRuntimeError] = None
        stdout_count = 0
        line_number = 0

        def consume_line(raw_line: str | bytes) -> None:
            nonlocal session_id, final_text, turn_end_reason, source_error
            nonlocal protocol_error, stdout_count, line_number
            line_bytes = raw_line.encode("utf-8") if isinstance(raw_line, str) else raw_line
            line_number += 1
            stdout_count += len(line_bytes)
            if stdout_count > self.config.max_stdout_bytes:
                raise DshProtocolError("stdout_limit", "DSH stdout exceeded the configured limit")
            event = parse_headless_line(
                line_bytes,
                line_number=line_number,
                max_event_bytes=self.config.max_event_bytes,
                max_final_bytes=self.config.max_stdout_bytes,
            )
            if event is None:
                return
            if final_text is not None:
                raise DshProtocolError("event_after_final", "DSH emitted an event after final")
            if source_error is not None:
                raise DshProtocolError("event_after_error", "DSH emitted an event after an error")
            if not events and event.type not in {"session", "error"}:
                raise DshProtocolError("missing_session", "DSH stream did not begin with a session event")
            if event.type == "session":
                emitted_session_id = str(event.data["session_id"])
                if session_id is not None or events:
                    raise DshProtocolError("duplicate_session", "DSH emitted an unexpected session event")
                if invocation.session_id is not None and emitted_session_id != invocation.session_id:
                    raise DshProtocolError("session_mismatch", "DSH resumed a different session")
                try:
                    emitted_cwd = Path(str(event.data["cwd"])).resolve(strict=False)
                except (OSError, ValueError) as exc:
                    raise DshProtocolError("workspace_mismatch", "DSH reported an invalid workspace") from exc
                if emitted_cwd != invocation.workspace:
                    raise DshProtocolError("workspace_mismatch", "DSH reported a different workspace")
                session_id = emitted_session_id
            elif event.type == "status" and event.phase == "turn_end":
                turn_end_reason = _reason_kind(event.data.get("reason"))
            elif event.type == "final":
                if final_text is not None:
                    raise DshProtocolError("duplicate_final", "DSH emitted more than one final event")
                final_text = str(event.data.get("text") or "")
            elif event.type == "error" and source_error is None:
                source_error = DshRuntimeError(
                    str(event.data.get("code") or "dsh_error"),
                    str(event.data.get("message") or "DSH reported an execution error"),
                )
            events.append(event)
            if on_event is not None:
                on_event(event)

        try:
            process_output = self.runner(
                argv,
                cwd=invocation.workspace,
                timeout_seconds=invocation.timeout_seconds,
                cancel_event=cancel_event,
                max_stdout_bytes=self.config.max_stdout_bytes,
                max_stderr_bytes=self.config.max_stderr_bytes,
                on_stdout_line=consume_line,
            )
        except Exception:
            return _failure_result(
                "spawn_failed",
                "failed to start configured DSH runtime",
                on_event=on_event,
            )

        for raw_line in process_output.stdout_lines:
            try:
                consume_line(raw_line)
            except DshProtocolError as exc:
                protocol_error = DshRuntimeError(exc.code, str(exc))
                break
            except Exception:
                protocol_error = DshRuntimeError("event_callback_failed", "DSH event consumer failed")
                break
        if protocol_error is None:
            protocol_error = process_output.stream_error

        diagnostics = _bounded_diagnostic(
            process_output.stderr,
            self.config.max_stderr_bytes,
            already_truncated=process_output.stderr_truncated,
        )
        error = _settled_error(
            process_output,
            protocol_error=protocol_error,
            source_error=source_error,
            session_id=session_id,
            final_text=final_text,
            turn_end_reason=turn_end_reason,
        )
        if error is not None and not any(
            event.type == "error" and event.data.get("code") == error.code for event in events
        ):
            generated = DshRuntimeEvent("error", {"code": error.code, "message": error.message})
            events.append(generated)
            _notify_event(on_event, generated)

        return DshExecutionResult(
            session_id=session_id,
            final_text=final_text,
            turn_end_reason=turn_end_reason,
            exit_code=process_output.exit_code,
            timed_out=process_output.timed_out,
            cancelled=process_output.cancelled,
            events=tuple(events),
            stderr_diagnostics=diagnostics,
            error=error,
        )


def parse_headless_line(
    raw_line: str | bytes,
    *,
    line_number: int,
    max_event_bytes: int,
    max_final_bytes: Optional[int] = None,
) -> Optional[DshRuntimeEvent]:
    """Parse and normalize one bounded Headless NDJSON line."""
    encoded = raw_line.encode("utf-8") if isinstance(raw_line, str) else raw_line
    hard_limit = max_event_bytes if max_final_bytes is None else max_final_bytes
    if len(encoded) > hard_limit:
        raise DshProtocolError("event_limit", f"DSH event on line {line_number} exceeded the configured limit")
    try:
        text = encoded.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise DshProtocolError("malformed_ndjson", f"DSH output line {line_number} was not valid UTF-8") from exc
    if not text:
        return None
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise DshProtocolError("malformed_ndjson", f"DSH output line {line_number} was not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        raise DshProtocolError("malformed_event", f"DSH output line {line_number} was not an event object")

    event_type = payload["type"]
    if len(encoded) > max_event_bytes and event_type != "final":
        raise DshProtocolError("event_limit", f"DSH event on line {line_number} exceeded the configured limit")
    truncated = payload.get("truncated") is True
    if event_type == "session":
        try:
            session_id = validate_session_id(payload.get("sessionId"))
        except DshValidationError as exc:
            raise DshProtocolError("malformed_event", "DSH session event contained an invalid session id") from exc
        cwd = payload.get("cwd")
        if not isinstance(cwd, str):
            raise DshProtocolError("malformed_event", "DSH session event omitted cwd")
        return DshRuntimeEvent("session", {"session_id": session_id, "cwd": cwd}, truncated=truncated)
    if event_type == "status":
        phase = payload.get("phase")
        if phase not in _STATUS_PHASES:
            return _unknown_event(payload, truncated)
        if not truncated:
            turn = payload.get("turn")
            if type(turn) is not int or turn < 0:
                raise DshProtocolError("malformed_event", "DSH status event omitted a valid turn")
            if phase in {"step_start", "step_end"}:
                step = payload.get("step")
                if type(step) is not int or step < 0:
                    raise DshProtocolError("malformed_event", "DSH step status omitted a valid step")
            if phase == "turn_end" and _reason_kind(payload.get("reason")) is None:
                raise DshProtocolError("malformed_event", "DSH turn_end status omitted its reason")
        data = {key: payload[key] for key in ("turn", "step", "usage", "reason") if key in payload}
        return DshRuntimeEvent("status", data, phase=phase, truncated=truncated)
    if event_type in {"thinking", "text", "final"}:
        value = payload.get("text")
        if not isinstance(value, str):
            raise DshProtocolError("malformed_event", f"DSH {event_type} event omitted text")
        return DshRuntimeEvent(event_type, {"text": value}, truncated=truncated)
    if event_type == "tool_call":
        call_id = payload.get("callId")
        tool = payload.get("tool")
        tool_input = payload.get("input")
        if not truncated and (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(tool, str)
            or not tool
            or "input" not in payload
        ):
            raise DshProtocolError("malformed_event", "DSH tool_call event omitted required fields")
        if tool_input is not None and not isinstance(tool_input, (dict, str)):
            raise DshProtocolError("malformed_event", "DSH tool_call input must be an object or text")
        data = {"call_id": call_id, "tool": tool, "input": tool_input}
        return DshRuntimeEvent("tool_call", data, truncated=truncated)
    if event_type == "tool_result":
        call_id = payload.get("callId")
        status = payload.get("status")
        if not truncated and (
            not isinstance(call_id, str)
            or not call_id
            or status not in {"completed", "error"}
            or not isinstance(payload.get("result"), str)
        ):
            raise DshProtocolError("malformed_event", "DSH tool_result event omitted required fields")
        data = {"call_id": call_id, "status": status, "result": payload.get("result")}
        return DshRuntimeEvent("tool_result", data, truncated=truncated)
    if event_type == "error":
        message = _redact_sensitive(str(payload.get("message") or "DSH reported an execution error"))
        code_value = str(payload.get("code") or "dsh_error")
        code = code_value if _ERROR_CODE_RE.fullmatch(code_value) else "dsh_error"
        return DshRuntimeEvent("error", {"code": code, "message": message}, truncated=truncated)
    return _unknown_event(payload, truncated)


def _unknown_event(payload: dict, truncated: bool) -> DshRuntimeEvent:
    source_type = _redact_sensitive(str(payload.get("type") or ""))[:64]
    return DshRuntimeEvent(
        "unknown",
        {"payload": _redact_structure({key: value for key, value in payload.items() if key != "type"})},
        source_type=source_type,
        truncated=truncated,
    )


def _redact_structure(value: object) -> object:
    if isinstance(value, str):
        return _redact_sensitive(value)
    if isinstance(value, list):
        return [_redact_structure(item) for item in value]
    if isinstance(value, dict):
        return {
            _redact_sensitive(str(key))[:128]: (
                "[redacted]" if _SENSITIVE_KEY_RE.fullmatch(str(key)) else _redact_structure(item)
            )
            for key, item in value.items()
        }
    return value


def _reason_kind(reason: object) -> Optional[str]:
    if isinstance(reason, str):
        return reason
    if isinstance(reason, dict) and isinstance(reason.get("kind"), str):
        return reason["kind"]
    return None


def _settled_error(
    output: DshProcessOutput,
    *,
    protocol_error: Optional[DshRuntimeError],
    source_error: Optional[DshRuntimeError],
    session_id: Optional[str],
    final_text: Optional[str],
    turn_end_reason: Optional[str],
) -> Optional[DshRuntimeError]:
    if output.cancelled:
        return DshRuntimeError("cancelled", "DSH execution was cancelled")
    if output.timed_out:
        return DshRuntimeError("timeout", "DSH execution exceeded the configured timeout")
    if output.stdout_limit_exceeded:
        return DshRuntimeError("stdout_limit", "DSH stdout exceeded the configured limit")
    if protocol_error is not None:
        return protocol_error
    if source_error is not None:
        return source_error
    if output.exit_code != 0:
        return DshRuntimeError("nonzero_exit", "DSH process exited unsuccessfully")
    if session_id is None:
        return DshRuntimeError("missing_session", "DSH process exited without a session event")
    if final_text is None:
        return DshRuntimeError("missing_final", "DSH process exited without a final event")
    if turn_end_reason is None:
        return DshRuntimeError("missing_turn_end", "DSH process exited without a turn_end status")
    if turn_end_reason != "completed":
        return DshRuntimeError("turn_incomplete", "DSH turn did not complete successfully")
    return None


def _notify_event(
    on_event: Optional[Callable[[DshRuntimeEvent], None]],
    event: DshRuntimeEvent,
) -> None:
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:
        pass


def _failure_result(
    code: str,
    message: str,
    *,
    on_event: Optional[Callable[[DshRuntimeEvent], None]],
) -> DshExecutionResult:
    event = DshRuntimeEvent("error", {"code": code, "message": message})
    _notify_event(on_event, event)
    return DshExecutionResult(None, None, None, None, False, False, (event,), error=DshRuntimeError(code, message))


def _bounded_diagnostic(value: str | bytes, limit: int, *, already_truncated: bool) -> str:
    encoded = value.encode("utf-8", errors="replace") if isinstance(value, str) else value
    truncated = already_truncated or len(encoded) > limit
    bounded = encoded[:limit].decode("utf-8", errors="replace")
    bounded = _redact_sensitive(bounded)
    if truncated:
        bounded = bounded.rstrip() + "\n...[stderr truncated]"
    return bounded


def _redact_sensitive(text: str) -> str:
    redacted = _QUOTED_SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}[redacted]", text)
    redacted = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", redacted)
    redacted = _BEARER_RE.sub(lambda match: f"{match.group(1)}[redacted]", redacted)
    redacted = _URL_CREDENTIAL_RE.sub(lambda match: f"{match.group(1)}[redacted]@", redacted)
    for key, value in os.environ.items():
        upper = key.upper()
        if len(value) >= 8 and any(marker in upper for marker in ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "CREDENTIAL")):
            redacted = redacted.replace(value, "[redacted]")
    return redacted
