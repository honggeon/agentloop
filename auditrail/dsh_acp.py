"""Low-level Agent Client Protocol transport for the local DSH runtime.

This module implements the ACP v1 stdio transport verified against DSH
0.2.0-rc.2 (see ``docs/reports/2026-10-14-dsh-acp-protocol-spike.md``). It owns
exactly three things:

1. NDJSON framing over a *process-like* object;
2. JSON-RPC 2.0 request/response/notification correlation, including inbound
   agent-to-client requests; and
3. A session-oriented client that normalizes ACP traffic into the same
   transport-neutral events Headless produces.

It deliberately depends on nothing outside the standard library and the core
package: no FastAPI, no backend, no SSE, no session manager. Process ownership,
operation registries, permission policy, and HTTP surfaces are M6B concerns.

The single structural constraint worth stating up front: DSH asks for tool
permission with a *request* while ``session/prompt`` is still in flight. An
implementation that can only answer after the prompt returns deadlocks. This
transport therefore dispatches inbound requests from the reader thread and lets
a handler claim them, and ``AcpClient`` queues permission requests for the
caller to answer at any time.
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

try:  # POSIX-only; absent on Windows, where the write timeout stays unbounded.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX platforms
    fcntl = None  # type: ignore[assignment]

from .dsh_headless import _redact_sensitive, _redact_structure
from .dsh_runtime import (
    DshProtocolError,
    DshRuntimeConfig,
    DshRuntimeEvent,
    DshValidationError,
)


# --- Protocol constants -----------------------------------------------------

#: The only ACP protocol version DSH 0.2.0-rc.2 negotiates.
ACP_PROTOCOL_VERSION = 1

#: ``clientInfo.name`` reported during ``initialize``.
ACP_CLIENT_NAME = "auditrail"

#: ``clientInfo.version`` reported during ``initialize``.
ACP_CLIENT_VERSION = "0.1.0"

#: Maximum characters retained when reporting an unmodeled ``sessionUpdate``.
ACP_MAX_SOURCE_TYPE_CHARS = 64

#: Permission option ids DSH always offers. ``allow_always`` and
#: ``reject_always`` are valid ACP kinds that DSH never sends.
ACP_ALLOW_ONCE_OPTION_ID = "allow-once"
ACP_REJECT_ONCE_OPTION_ID = "reject-once"


class AgentMethod:
    """Agent-side ACP methods (client -> agent)."""

    INITIALIZE = "initialize"
    AUTHENTICATE = "authenticate"
    SESSION_NEW = "session/new"
    SESSION_LIST = "session/list"
    SESSION_RESUME = "session/resume"
    SESSION_CLOSE = "session/close"
    SESSION_SET_CONFIG_OPTION = "session/set_config_option"
    SESSION_PROMPT = "session/prompt"
    SESSION_CANCEL = "session/cancel"


class ClientMethod:
    """Client-side ACP methods (agent -> client)."""

    SESSION_UPDATE = "session/update"
    SESSION_REQUEST_PERMISSION = "session/request_permission"


class ProtocolMethod:
    """Protocol-level ($-prefixed) JSON-RPC methods."""

    CANCEL_REQUEST = "$/cancel_request"


class AcpStopReason:
    """ACP ``PromptResponse.stopReason`` vocabulary.

    ``end_turn`` covers both success and failure in DSH's mapping, so it is not
    a success signal.
    """

    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    MAX_TURN_REQUESTS = "max_turn_requests"
    REFUSAL = "refusal"
    CANCELLED = "cancelled"


class SessionUpdateKind:
    """``session/update`` discriminators DSH and the wider ACP surface define."""

    # Emitted by DSH 0.2.0-rc.2.
    AGENT_MESSAGE_CHUNK = "agent_message_chunk"
    AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
    TOOL_CALL = "tool_call"
    TOOL_CALL_UPDATE = "tool_call_update"
    USAGE_UPDATE = "usage_update"
    CONFIG_OPTION_UPDATE = "config_option_update"
    # Defined by ACP but not mounted in DSH; tolerated as unknown.
    USER_MESSAGE_CHUNK = "user_message_chunk"
    PLAN = "plan"
    PLAN_UPDATE = "plan_update"
    PLAN_REMOVED = "plan_removed"
    AVAILABLE_COMMANDS_UPDATE = "available_commands_update"
    CURRENT_MODE_UPDATE = "current_mode_update"
    SESSION_INFO_UPDATE = "session_info_update"
    COMPACTION_UPDATE = "compaction_update"
    COMPACTION_SUMMARY_CHUNK = "compaction_summary_chunk"


#: JSON-RPC error codes shared with the reference ACP SDK.
JSONRPC_PARSE_ERROR = -32700
JSONRPC_INVALID_REQUEST = -32600
JSONRPC_METHOD_NOT_FOUND = -32601
JSONRPC_INVALID_PARAMS = -32602
JSONRPC_INTERNAL_ERROR = -32603
JSONRPC_REQUEST_CANCELLED = -32800


# --- Framing ----------------------------------------------------------------


def encode_frame(message: Mapping[str, Any]) -> bytes:
    """Encode one NDJSON frame exactly as the ACP transport requires."""
    return json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"


class _AcpWriteTimeout(Exception):
    """A frame could not be written within the configured deadline."""


def _write_frame_with_deadline(stream: Any, data: bytes, *, timeout_seconds: float) -> None:
    """Write one frame, giving up once the peer has not read for too long.

    A peer that stops reading its stdin fills the pipe buffer, after which a
    plain blocking write never returns. While it is stuck it holds the transport's
    single write lock, so *every* other writer queues behind it and the backend
    stops making progress. The fd is switched to non-blocking mode for the write
    and restored afterwards, so no later caller inherits the flag.

    On timeout the frame may be only partly written, which is why the caller
    invalidates the whole transport rather than retrying.
    """
    try:
        descriptor = stream.fileno()
    except (AttributeError, OSError, ValueError):
        # No real fd (for example an in-memory test double): the write cannot be
        # bounded, so keep the previous blocking behaviour.
        stream.write(data)
        _flush_stream(stream)
        return

    deadline = time.monotonic() + timeout_seconds
    remaining = memoryview(data)
    if fcntl is None:  # pragma: no cover - non-POSIX platforms
        stream.write(data)
        _flush_stream(stream)
        return
    try:
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    except (AttributeError, OSError):
        flags = None
    if flags is None:
        stream.write(data)
        _flush_stream(stream)
        return
    fcntl.fcntl(descriptor, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    try:
        while remaining:
            wait = deadline - time.monotonic()
            if wait <= 0:
                raise _AcpWriteTimeout()
            try:
                _, writable, _ = select.select([], [descriptor], [], wait)
            except (OSError, ValueError):
                writable = [descriptor]
            if not writable:
                raise _AcpWriteTimeout()
            try:
                written = os.write(descriptor, remaining)
            except BlockingIOError:
                continue
            if written <= 0:
                raise _AcpWriteTimeout()
            remaining = remaining[written:]
    finally:
        try:
            fcntl.fcntl(descriptor, fcntl.F_SETFL, flags)
        except OSError:
            pass


def _flush_stream(stream: Any) -> None:
    try:
        stream.flush()
    except (OSError, ValueError):
        pass


#: Bound on retained permission requests. A peer that stops being answered must
#: not be able to grow the client's permission table without limit, and a request
#: whose turn already settled is dropped by
#: :meth:`AcpClient.forget_permissions_for_session`.
ACP_MAX_PENDING_PERMISSIONS = 256


def _hashable_request_id(request_id: Any) -> Any:
    """Return a dict-safe id, so an unhashable caller value is a typed error."""
    try:
        hash(request_id)
    except TypeError as exc:
        raise DshValidationError("permission request id must be a string or integer") from exc
    return request_id


def _is_cancellation_signal(error: AcpError, pending: _PendingRequest) -> bool:
    """True when a failed turn is really a cancellation, not a failure.

    Either the caller asked to cancel (the peer may still answer normally), or the
    peer itself reported cancellation with JSON-RPC ``-32800``.
    """
    if pending.cancel_requested:
        return True
    if error.code == "cancelled":
        return True
    if isinstance(error, AcpRemoteError) and error.peer_error.code == JSONRPC_REQUEST_CANCELLED:
        return True
    return False


@dataclass(frozen=True)
class AcpPeerError:
    """A JSON-RPC error object received from the peer."""

    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


class AcpError(DshProtocolError):
    """An ACP-level failure with a stable machine-readable code.

    The detail is always bounded and redacted; it never carries raw stderr or
    credentials.
    """

    def __init__(self, code: str, message: str, *, data: Any = None):
        super().__init__(code, message)
        self.message = message
        if data is not None:
            self.data = data


class AcpRemoteError(AcpError):
    """The peer answered a request with a JSON-RPC error response."""

    def __init__(self, peer_error: AcpPeerError, *, method: str = ""):
        # Peer text is untrusted: cap and redact it, and never attach the raw
        # error object, so a huge or credential-bearing payload cannot be
        # amplified into an SSE message or leak through diagnostics.
        detail = peer_error.message[:512] if peer_error.message else "ACP peer reported an error"
        if method:
            detail = "%s (%s)" % (detail, method)
        data = _redact_structure(peer_error.data) if peer_error.data is not None else None
        super().__init__("remote_error", _redact_sensitive(detail), data=data)
        self.peer_error = AcpPeerError(
            code=peer_error.code,
            message=_redact_sensitive(peer_error.message[:512]),
            data=data,
        )
        self.method = method

    @property
    def code_number(self) -> int:
        return self.peer_error.code


@dataclass(frozen=True)
class AcpMessage:
    """One decoded JSON-RPC frame, classified but otherwise untouched."""

    raw: Mapping[str, Any]

    @property
    def method(self) -> Optional[str]:
        value = self.raw.get("method")
        return value if isinstance(value, str) else None

    @property
    def request_id(self) -> Any:
        return self.raw.get("id")

    @property
    def params(self) -> Any:
        return self.raw.get("params")

    @property
    def has_id(self) -> bool:
        return "id" in self.raw

    @property
    def is_request(self) -> bool:
        return self.has_id and isinstance(self.raw.get("method"), str)

    @property
    def is_notification(self) -> bool:
        return not self.has_id and isinstance(self.raw.get("method"), str)

    @property
    def is_response(self) -> bool:
        # A frame that carries a method is a request, whatever else it holds, so
        # it must not be correlated against a pending request id.
        return self.has_id and "method" not in self.raw

    @property
    def is_error(self) -> bool:
        return self.is_response and "error" in self.raw

    @property
    def result(self) -> Any:
        return self.raw.get("result")

    @property
    def peer_error(self) -> Optional[AcpPeerError]:
        value = self.raw.get("error")
        if not isinstance(value, Mapping):
            return None
        code = value.get("code")
        return AcpPeerError(
            code=code if isinstance(code, int) else JSONRPC_INTERNAL_ERROR,
            message=str(value.get("message") or ""),
            data=value.get("data"),
        )

    def to_dict(self) -> dict[str, Any]:
        return dict(self.raw)


def validate_jsonrpc_id(value: Any) -> Any:
    """Return a correlation-safe JSON-RPC id, or raise for an unusable one.

    JSON-RPC allows a string or a number, but Python equates ``True == 1`` and
    ``1.0 == 1``. Without normalizing, a peer could settle an unrelated pending
    request (for example the ``initialize`` handshake) by answering with a
    boolean or float id. Bools and floats are therefore rejected, not coerced.
    """
    if isinstance(value, str):
        if not value or len(value.encode("utf-8")) > 256:
            raise DshValidationError("invalid JSON-RPC id")
        return value
    if isinstance(value, bool) or not isinstance(value, int):
        raise DshValidationError("invalid JSON-RPC id")
    return value


def decode_frame(data: bytes) -> AcpMessage:
    """Decode one complete NDJSON frame.

    Blank lines are rejected: the caller already decided a line is present, so an
    empty payload is a peer framing violation rather than padding.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AcpError("malformed_frame", "ACP frame was not valid UTF-8") from exc
    stripped = text.strip()
    if not stripped:
        raise AcpError("malformed_frame", "ACP frame was empty")
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AcpError("malformed_frame", "ACP frame was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AcpError("malformed_frame", "ACP frame was not a JSON object")
    if payload.get("jsonrpc") != "2.0":
        raise AcpError("malformed_frame", "ACP frame did not declare JSON-RPC 2.0")
    if "id" in payload:
        try:
            payload["id"] = validate_jsonrpc_id(payload["id"])
        except DshValidationError as exc:
            # Dropped here so a peer cannot smuggle ``true``/``1.0`` into the
            # pending-request table, where ``True == 1`` would settle request 1.
            raise AcpError("malformed_frame", "ACP frame carried an unusable JSON-RPC id") from exc
    return AcpMessage(payload)


@dataclass(frozen=True)
class AcpFrameRead:
    """Outcome of reading one NDJSON frame."""

    frame: Optional[bytes] = None
    too_large: bool = False
    discarded_bytes: int = 0

    @property
    def end_of_stream(self) -> bool:
        return self.frame is None and not self.too_large


class AcpFrameReader:
    """Bounded NDJSON frame reader over a byte stream.

    Bytes are buffered across calls, so a stream implementation that returns more
    than the requested size (or a blocking pipe that returns a partial frame) is
    handled without losing data. An oversize frame is drained to its newline so
    the stream stays framed, then reported as ``too_large`` rather than silently
    truncated.
    """

    def __init__(self, stream: Any, *, max_bytes: int, chunk_size: int = 4096):
        if max_bytes <= 0:
            raise DshValidationError("ACP frame limit must be positive")
        if chunk_size <= 0:
            raise DshValidationError("ACP read chunk size must be positive")
        self._stream = stream
        self._max_bytes = max_bytes
        self._chunk_size = chunk_size
        self._read_chunk = getattr(stream, "read1", None) or stream.read
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def read(self) -> AcpFrameRead:
        """Read the next frame, blocking only until data is available."""
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                frame = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                if len(frame) > self._max_bytes:
                    return AcpFrameRead(too_large=True, discarded_bytes=len(frame))
                return AcpFrameRead(frame=frame)
            if len(self._buffer) > self._max_bytes:
                return self._drain_oversize()
            chunk = self._read_chunk(self._chunk_size)
            if not chunk:
                if not self._buffer:
                    return AcpFrameRead()
                frame = bytes(self._buffer)
                self._buffer.clear()
                if len(frame) > self._max_bytes:
                    return AcpFrameRead(too_large=True, discarded_bytes=len(frame))
                return AcpFrameRead(frame=frame)
            self._buffer.extend(chunk)

    def _drain_oversize(self) -> AcpFrameRead:
        """Discard an over-limit frame through its terminating newline.

        ``discarded`` counts only bytes that belong to the rejected frame, so a
        newline that also carried the start of the next frame is not overcounted.
        """
        discarded = 0
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                discarded += newline
                del self._buffer[: newline + 1]
                return AcpFrameRead(too_large=True, discarded_bytes=discarded)
            discarded += len(self._buffer)
            self._buffer.clear()
            chunk = self._read_chunk(self._chunk_size)
            if not chunk:
                return AcpFrameRead(too_large=True, discarded_bytes=discarded)
            self._buffer.extend(chunk)


def read_frame_bytes(stream: Any, *, max_bytes: int) -> AcpFrameRead:
    """Read one frame from a fresh reader over ``stream``.

    Convenient for finite streams and tests. Long-lived transports keep an
    :class:`AcpFrameReader` so partial frames are not lost between calls.
    """
    return AcpFrameReader(stream, max_bytes=max_bytes).read()


# --- Process boundary -------------------------------------------------------


class AcpProcess:
    """A started ACP server process.

    The transport only needs this narrow surface, which lets deterministic tests
    inject in-memory pipes instead of spawning anything.
    """

    def __init__(
        self,
        argv: Sequence[str],
        process: Any,
        *,
        owns_process: bool = True,
    ):
        self.argv = tuple(argv)
        self._process = process
        self._owns_process = owns_process

    @property
    def pid(self) -> int:
        return int(getattr(self._process, "pid", 0) or 0)

    @property
    def stdin(self) -> Any:
        return self._process.stdin

    @property
    def stdout(self) -> Any:
        return self._process.stdout

    @property
    def stderr(self) -> Any:
        return self._process.stderr

    @property
    def owns_process(self) -> bool:
        return self._owns_process

    def poll(self) -> Optional[int]:
        return self._process.poll()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        return self._process.wait(timeout=timeout)

    def close_stdin(self) -> None:
        """Close stdin. DSH treats this as the supported shutdown request."""
        stdin = self._process.stdin
        if stdin is None:
            return
        try:
            stdin.close()
        except OSError:
            pass

    def terminate(self, *, force: bool = False) -> None:
        """Stop the process group. Tool subprocesses share the group."""
        if not self._owns_process:
            self.close_stdin()
            return
        pid = self.pid
        if pid <= 0:
            # ``os.getpgid(0)`` is this process's own group, so signalling with a
            # missing pid would kill the backend instead of the peer. Only the
            # stdin close is safe here.
            self.close_stdin()
            return
        if os.name == "posix":
            import signal

            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                self._signal_directly(force=force)
        else:
            self._signal_directly(force=force)
        try:
            self._process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            if not force:
                self.terminate(force=True)
        except OSError:
            pass

    def _signal_directly(self, *, force: bool) -> None:
        """Fall back to signalling the handle itself when the group is gone."""
        if self.poll() is not None:
            return
        try:
            (self._process.kill if force else self._process.terminate)()
        except (AttributeError, OSError):
            # A partial or already-detached handle must not raise out of close().
            pass


AcpProcessFactory = Callable[..., AcpProcess]


def spawn_acp_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Optional[Mapping[str, str]] = None,
    extra_env: Optional[Mapping[str, str]] = None,
) -> AcpProcess:
    """Start ``dsh --profile acp`` with an argv array and no shell.

    ``extra_env`` is merged over ``env``/``os.environ`` so a caller can force a
    non-networking telemetry mode without dropping credentials the profile needs.
    """
    if os.name != "posix":
        raise OSError("local DSH ACP runtime requires POSIX process-group isolation")
    merged = dict(os.environ if env is None else env)
    if extra_env:
        merged.update({str(key): str(value) for key, value in extra_env.items()})
    process = subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        start_new_session=True,
        env=merged,
    )
    return AcpProcess(argv, process)


def build_acp_argv(config: DshRuntimeConfig, profile: str) -> tuple[str, ...]:
    """Build the only supported ACP argv: ``dsh --profile <acp>`` and nothing else.

    The ACP app accepts no arguments, so unlike Headless there is no task text
    to place, and no caller-supplied flag can reach the process.
    """
    validated = config.validate_profile(profile)
    if config.judge_profile and validated == config.judge_profile:
        raise DshValidationError("the DSH judge profile cannot run an interactive ACP session")
    return (str(config.binary), "--profile", validated)


# --- Transport --------------------------------------------------------------


@dataclass
class _PendingRequest:
    request_id: Any
    method: str
    deadline: Optional[float]
    cancellable: bool
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Optional[AcpError] = None
    cancel_requested: bool = False


#: Inbound request handler: return ``True`` when it claims the request.
AcpRequestHandler = Callable[[AcpMessage, Callable[[Any], None]], bool]

#: Permission listener: notified once per claimed ``session/request_permission``.
AcpPermissionListener = Callable[["AcpPermissionRequest"], None]

#: Bounded per-turn buffer of ``session/update`` frames.
ACP_MAX_SUBSCRIBED_UPDATES = 4096

#: Bounded buffer of unclaimed inbound frames (notifications for a session that
#: has no open subscription, plus responses to unknown or already-settled
#: requests). A peer that streams updates forever without a matching consumer, or
#: that answers ids nobody sent, must not be able to grow the backend without
#: limit. The newest frames are dropped and counted, mirroring the subscription
#: buffer, so a stalled projection degrades visibly instead of exhausting memory.
ACP_MAX_UNCLAIMED_INBOX = 4096


class AcpSessionUpdates:
    """The ``session/update`` notifications of one in-flight prompt turn.

    A turn and a long-lived projection layer (one that streams live updates to a
    UI) both need the same notifications.  Draining one shared inbox from both
    sides instead splits every turn arbitrarily between them: whichever consumer
    wins the race takes the chunk, so neither sees the whole turn and the
    settled outcome can come back incomplete.

    A subscription removes the race instead of tolerating it.  While one is open
    the transport delivers that session's updates *here and only here*, in wire
    order, and the subscription owns them for the life of the turn.  Frames
    already queued when the subscription opened are claimed too, so a turn never
    starts with a hole.

    The buffer is bounded: a turn that somehow produces more updates than
    :data:`ACP_MAX_SUBSCRIBED_UPDATES` drops the newest and reports it through
    :attr:`dropped`, rather than growing without limit.
    """

    def __init__(self, transport: "AcpTransport", session_id: str):
        self._transport = transport
        self.session_id = session_id
        self._queue: list[AcpMessage] = []
        self._seen: set[Any] = set()
        self._dropped = 0
        self._closed = False

    @property
    def dropped(self) -> int:
        """Count of updates dropped because the bounded buffer was full."""
        return self._dropped

    def drain(self) -> list[AcpMessage]:
        """Take the frames collected so far, leaving the subscription open."""
        return self._transport._drain_session_updates(self)

    def close(self) -> list[AcpMessage]:
        """Detach and return every frame still owned by this subscription."""
        return self._transport._close_session_updates(self)


#: Bound on the per-connection set of answered inbound request ids. The set must
#: be bounded because ids are peer-chosen; evicting the oldest only risks
#: re-answering an id the peer reused after thousands of others.
ACP_MAX_RESPONDED_IDS = 4096


class AcpTransport:
    """Bidirectional NDJSON JSON-RPC transport over one ACP process.

    Threading model: one reader thread consumes stdout and dispatches
    classified frames; any number of caller threads may call
    :meth:`send_request`. Writes are serialized so frames never interleave.
    """

    def __init__(
        self,
        process: Any,
        *,
        max_frame_bytes: int = 4 * 1024 * 1024,
        write_timeout_seconds: float = 10.0,
    ):
        if max_frame_bytes <= 0:
            raise DshValidationError("ACP frame limit must be positive")
        if write_timeout_seconds <= 0:
            raise DshValidationError("ACP write timeout must be positive")
        self._process = process
        self._max_frame_bytes = max_frame_bytes
        self._write_timeout = write_timeout_seconds

        self._state = threading.Condition()
        self._inbox: list[AcpMessage] = []
        self._inbox_dropped = 0
        self._subscriptions: dict[str, AcpSessionUpdates] = {}
        self._pending: dict[Any, _PendingRequest] = {}
        self._handlers: dict[str, AcpRequestHandler] = {}
        self._responded: dict[Any, None] = {}
        self._framing_errors: list[AcpError] = []
        self._next_id = 1
        self._stdout_exhausted = False
        self._closed = False
        self._close_error: Optional[AcpError] = None
        self._write_lock = threading.Lock()
        self._write_failed: Optional[AcpError] = None
        self._stderr_bytes = bytearray()
        self._stderr_capped = False
        self._stderr_thread: Optional[threading.Thread] = None
        self._reader_thread: Optional[threading.Thread] = None

    # -- lifecycle --

    @property
    def process(self) -> Any:
        return self._process

    @property
    def is_closed(self) -> bool:
        with self._state:
            return self._closed

    @property
    def close_error(self) -> Optional[AcpError]:
        with self._state:
            return self._close_error

    @property
    def framing_errors(self) -> tuple[AcpError, ...]:
        with self._state:
            return tuple(self._framing_errors)

    @property
    def inbox_dropped(self) -> int:
        """Count of unclaimed inbound frames dropped because the buffer was full."""
        with self._state:
            return self._inbox_dropped

    def _append_inbox_locked(self, message: AcpMessage) -> None:
        """Queue one unclaimed frame, keeping the inbox bounded."""
        if len(self._inbox) >= ACP_MAX_UNCLAIMED_INBOX:
            self._inbox_dropped += 1
            return
        self._inbox.append(message)
        self._state.notify_all()

    @property
    def stderr_diagnostics(self) -> str:
        with self._state:
            data = bytes(self._stderr_bytes)
            capped = self._stderr_capped
        text = _redact_sensitive(data.decode("utf-8", errors="replace"))
        if capped:
            text = text.rstrip() + "\n...[stderr truncated]"
        return text

    def start(self) -> AcpTransport:
        """Start the reader (and bounded stderr drain) threads."""
        with self._state:
            if self._reader_thread is not None:
                return self
            self._reader_thread = threading.Thread(
                target=self._read_loop,
                name="dsh-acp-reader",
                daemon=True,
            )
            self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            name="dsh-acp-stderr",
            daemon=True,
        )
        self._stderr_thread.start()
        return self

    def is_alive(self) -> bool:
        """True only when the process runs and this transport can still be used.

        The process can outlive its usefulness: a frame-too-large, write timeout,
        or reader failure closes the transport while the peer keeps running. A
        caller that reuses the client on process liveness alone would keep sending
        into a closed transport, so liveness includes the closed flag.
        """
        with self._state:
            if self._closed:
                return False
        return self._process.poll() is None

    def close_stdin(self) -> None:
        self._process.close_stdin()

    def close(self, *, terminate: bool = True, force: bool = False) -> None:
        """Close stdin, stop the process, and fail every in-flight request."""
        self._process.close_stdin()
        if terminate:
            self._process.terminate(force=force)
        with self._state:
            if not self._closed:
                self._closed = True
                if self._close_error is None:
                    self._close_error = AcpError("closed", "ACP transport was closed")
                self._fail_locked(self._close_error)
                self._state.notify_all()

    def terminate(self, *, force: bool = True) -> None:
        self._process.terminate(force=force)

    # -- handlers --

    def handle_request(self, method: str, handler: AcpRequestHandler) -> None:
        """Register the handler for an inbound agent-to-client request method."""
        if not isinstance(method, str) or not method:
            raise DshValidationError("ACP request method must be a non-empty string")
        with self._state:
            self._handlers[method] = handler

    # -- sending --

    def send_notification(self, method: str, params: Any = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def send_response(self, request_id: Any, result: Any) -> bool:
        """Answer an inbound request exactly once."""
        with self._state:
            if not self._claim_responded_locked(request_id):
                return False
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})
        return True

    def send_error_response(self, request_id: Any, code: int, message: str) -> bool:
        """Refuse an inbound request exactly once with a JSON-RPC error."""
        detail = _redact_sensitive(str(message or "ACP request failed"))[:512]
        with self._state:
            if not self._claim_responded_locked(request_id):
                return False
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": int(code), "message": detail},
            }
        )
        return True

    def _claim_responded_locked(self, request_id: Any) -> bool:
        """Claim the right to answer one inbound id; False when already answered.

        The record is bounded so a peer that sends unbounded distinct ids cannot
        grow the set forever. Eviction only loses the guarantee for an id reused
        after thousands of others, which the caller detects as a fresh request.
        """
        if request_id in self._responded:
            return False
        self._responded[request_id] = None
        while len(self._responded) > ACP_MAX_RESPONDED_IDS:
            self._responded.pop(next(iter(self._responded)), None)
        return True

    def send_request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout_seconds: Optional[float] = None,
        deadline: Optional[float] = None,
        cancellable: bool = False,
        cancel_event: Optional[threading.Event] = None,
        pending: Optional[_PendingRequest] = None,
    ) -> Any:
        """Send a request and block until its response arrives.

        Args:
            method: ACP method name.
            params: JSON-serializable parameters.
            timeout_seconds: relative timeout; converted to an absolute deadline.
            deadline: absolute ``time.monotonic()`` deadline; wins over
                ``timeout_seconds`` and lets several calls share one budget.
            cancellable: send ``$/cancel_request`` when a cancel is observed, and
                report a cancelled outcome rather than hanging.
            cancel_event: cooperative cancellation signal.
            pending: an optional caller-owned request record. The caller can read
                its ``cancel_requested`` flag after the call settles, which is how
                a cancelled turn is reported as cancelled even when the peer folds
                cancellation into a normal result.

        Returns:
            The JSON-RPC ``result`` payload.

        Raises:
            AcpError: on timeout, cancellation, transport close, or write failure.
            AcpRemoteError: when the peer answered with a JSON-RPC error.
        """
        if not isinstance(method, str) or not method:
            raise DshValidationError("ACP method must be a non-empty string")
        now = time.monotonic()
        if deadline is None and timeout_seconds is not None:
            if timeout_seconds <= 0:
                raise DshValidationError("ACP request timeout must be positive")
            deadline = now + float(timeout_seconds)

        with self._state:
            if self._closed:
                raise self._close_error or AcpError("closed", "ACP transport is closed")
            request_id = self._next_id
            self._next_id += 1
            if pending is None:
                pending = _PendingRequest(
                    request_id=request_id,
                    method=method,
                    deadline=deadline,
                    cancellable=cancellable,
                )
            else:
                # A caller-owned record keeps its identity but must be registered
                # under the id this transport actually allocated.
                pending.request_id = request_id
                pending.method = method
                pending.deadline = deadline
                pending.cancellable = cancellable
            self._pending[request_id] = pending

        try:
            self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        except AcpError:
            with self._state:
                self._pending.pop(request_id, None)
            raise
        return self._await_response(pending, cancel_event=cancel_event)

    def _await_response(
        self,
        pending: _PendingRequest,
        *,
        cancel_event: Optional[threading.Event],
    ) -> Any:
        while True:
            if cancel_event is not None and cancel_event.is_set() and not pending.cancel_requested:
                pending.cancel_requested = True
                if pending.cancellable:
                    self._try_send_cancel_request(pending.request_id)
            with self._state:
                if pending.event.is_set():
                    break
                if self._closed:
                    if pending.error is None and pending.result is None:
                        pending.error = self._close_error or AcpError("closed", "ACP transport is closed")
                    pending.event.set()
                    break
                # Wait only as long as the earliest relevant condition: this
                # request's deadline, or one heartbeat when a cancel signal or
                # peer exit must be observed promptly.
                wait_for: Optional[float] = 0.05 if pending.deadline is None else max(
                    pending.deadline - time.monotonic(), 0.0
                )
                if cancel_event is not None or pending.cancellable:
                    # Bound the wait so a cancel signal is observed promptly
                    # even when the request's own deadline is far away.
                    wait_for = min(wait_for, 0.05)
                self._state.wait(timeout=wait_for)
                if pending.event.is_set():
                    break
                if self._stdout_exhausted:
                    pending.error = AcpError(
                        "closed",
                        "ACP peer closed the stream during %s" % pending.method,
                    )
                    pending.event.set()
                    break
                if pending.deadline is not None and time.monotonic() >= pending.deadline:
                    pending.error = AcpError(
                        "timeout",
                        "ACP request %s exceeded its deadline" % pending.method,
                    )
                    pending.event.set()
                    break
        with self._state:
            self._pending.pop(pending.request_id, None)
            error = pending.error
            result = pending.result
            cancel_requested = pending.cancel_requested
        if error is not None:
            if error.code == "timeout" and pending.cancellable:
                self._try_send_cancel_request(pending.request_id)
            if error.code == "cancelled" and cancel_requested:
                raise AcpError(
                    "cancelled",
                    "ACP request %s was cancelled" % pending.method,
                )
            raise error
        return result

    def _try_send_cancel_request(self, request_id: Any) -> None:
        try:
            self.send_notification(ProtocolMethod.CANCEL_REQUEST, {"requestId": request_id})
        except AcpError:
            pass

    # -- receiving --

    def take_notification(self, *, timeout: Optional[float] = None) -> Optional[AcpMessage]:
        """Pop the next queued inbound notification, or ``None``."""
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        with self._state:
            while True:
                for index, message in enumerate(self._inbox):
                    if message.is_notification:
                        del self._inbox[index]
                        return message
                if self._closed or self._stdout_exhausted:
                    return None
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._state.wait(timeout=remaining)

    def drain_inbox(self) -> tuple[AcpMessage, ...]:
        """Remove and return every queued inbound message.

        Subscribed turns deliver through :meth:`open_session_updates` instead,
        so their updates never appear here.
        """
        with self._state:
            queued = tuple(self._inbox)
            self._inbox.clear()
        return queued

    # -- per-session update subscriptions --

    def open_session_updates(self, session_id: str) -> AcpSessionUpdates:
        """Claim one session's ``session/update`` stream for an in-flight turn.

        Frames for that session already sitting in the inbox move to the
        subscription, so opening one never drops an update that arrived just
        before the prompt was written to the peer.
        """
        key = str(session_id)
        if not key:
            raise DshValidationError("ACP subscription needs a session id")
        with self._state:
            if self._closed:
                raise self._close_error or AcpError("closed", "ACP transport is closed")
            subscription = self._subscriptions.get(key)
            if subscription is not None and not subscription._closed:
                raise AcpError(
                    "session_subscribed",
                    "ACP session already has a subscribed update stream",
                )
            claimed = [message for message in self._inbox if _is_session_update(message, key)]
            if claimed:
                self._inbox = [
                    message for message in self._inbox if not _is_session_update(message, key)
                ]
            subscription = AcpSessionUpdates(self, key)
            self._subscriptions[key] = subscription
            for message in claimed:
                self._claim_update_locked(subscription, message)
        return subscription

    def _drain_session_updates(self, subscription: AcpSessionUpdates) -> list[AcpMessage]:
        with self._state:
            claimed = subscription._queue
            subscription._queue = []
            return claimed

    def _close_session_updates(self, subscription: AcpSessionUpdates) -> list[AcpMessage]:
        with self._state:
            subscription._closed = True
            if self._subscriptions.get(subscription.session_id) is subscription:
                del self._subscriptions[subscription.session_id]
            claimed = subscription._queue
            subscription._queue = []
            return claimed

    def _claim_update_locked(self, subscription: AcpSessionUpdates, message: AcpMessage) -> None:
        request_id = message.request_id
        key: Any = None
        if request_id is not None:
            key = request_id if isinstance(request_id, (str, int)) and not isinstance(request_id, bool) else str(request_id)
            if key in subscription._seen:
                return
        if len(subscription._queue) >= ACP_MAX_SUBSCRIBED_UPDATES:
            subscription._dropped += 1
            return
        if not subscription._queue:
            # A fresh batch: per-id deduplication only ever spans one batch.
            subscription._seen.clear()
        if key is not None:
            subscription._seen.add(key)
        subscription._queue.append(message)

    # -- internals --

    def _write(self, message: Mapping[str, Any]) -> None:
        with self._state:
            if self._write_failed is not None:
                raise self._write_failed
            if self._closed:
                # A deliberate local close must not look like a wire failure, and
                # a fire-and-forget frame must not be written to a closed peer.
                raise self._close_error or AcpError("closed", "ACP transport is closed")
        try:
            data = encode_frame(message)
        except (TypeError, ValueError, RecursionError) as exc:
            # Caller-supplied params/results reach here unfiltered, so a
            # non-serializable value must become a typed transport error rather
            # than a bare TypeError out of prompt()/respond_permission().
            raise AcpError("write_failed", "ACP frame was not JSON serializable") from exc
        with self._write_lock:
            with self._state:
                if self._write_failed is not None:
                    raise self._write_failed
            stdin = self._process.stdin
            error: AcpError
            if stdin is None:
                error = AcpError("write_failed", "ACP process stdin is unavailable")
                self._invalidate_write(error)
                raise error
            try:
                _write_frame_with_deadline(stdin, data, timeout_seconds=self._write_timeout)
            except _AcpWriteTimeout:
                # A partial frame may already be on the wire, so the peer's
                # stream can no longer be trusted: fail the transport instead of
                # letting the next writer append to a corrupt frame.
                error = AcpError("write_timeout", "ACP frame write exceeded %ss" % self._write_timeout)
                self._invalidate_write(error)
                raise error
            except (OSError, ValueError) as exc:
                error = AcpError("write_failed", "failed to write an ACP frame: %s" % type(exc).__name__)
                self._invalidate_write(error)
                raise error

    def _invalidate_write(self, error: AcpError) -> None:
        """Record a terminal write failure and fail every in-flight request."""
        with self._state:
            if self._write_failed is None:
                self._write_failed = error
            self._closed = True
            if self._close_error is None:
                self._close_error = error
            self._fail_locked(error)
            self._state.notify_all()

    def _read_loop(self) -> None:
        stream = self._process.stdout
        close_error: Optional[AcpError] = None
        try:
            if stream is None:
                close_error = AcpError("closed", "ACP process stdout is unavailable")
            else:
                reader = AcpFrameReader(stream, max_bytes=self._max_frame_bytes)
                while True:
                    read_result = reader.read()
                    if read_result.too_large:
                        self._record_framing_error(
                            AcpError(
                                "frame_too_large",
                                "ACP frame exceeded the configured limit",
                            )
                        )
                        close_error = AcpError(
                            "frame_too_large",
                            "ACP peer sent a frame beyond the configured limit",
                        )
                        break
                    frame = read_result.frame
                    if frame is None:
                        break
                    message = self._decode(frame)
                    if message is None:
                        continue
                    if not self._dispatch(message):
                        break
        except Exception:  # pragma: no cover - defensive reader guard
            close_error = AcpError("reader_failed", "ACP reader failed")
        finally:
            exit_code = None
            try:
                exit_code = self._process.poll()
            except Exception:  # pragma: no cover - defensive
                exit_code = None
            with self._state:
                self._stdout_exhausted = True
                self._closed = True
                if close_error is None:
                    detail = "ACP stream closed"
                    if exit_code is not None:
                        detail = "ACP process exited (exit code %s)" % exit_code
                    close_error = AcpError("closed", detail)
                if self._close_error is None:
                    self._close_error = close_error
                self._fail_locked(self._close_error)
                self._state.notify_all()

    def _decode(self, frame: bytes) -> Optional[AcpMessage]:
        try:
            return decode_frame(frame)
        except AcpError as exc:
            self._record_framing_error(exc)
            return None

    def _record_framing_error(self, error: AcpError) -> None:
        with self._state:
            if len(self._framing_errors) < 16:
                self._framing_errors.append(error)

    def _dispatch(self, message: AcpMessage) -> bool:
        """Route one decoded frame. Returns ``False`` to stop reading."""
        if message.is_request:
            self._handle_inbound_request(message)
            return True
        with self._state:
            if message.is_response:
                pending = self._pending.get(message.request_id)
                if pending is None:
                    # Response to an unknown or already-settled request.
                    self._append_inbox_locked(message)
                    return True
                if message.is_error:
                    peer_error = message.peer_error or AcpPeerError(
                        JSONRPC_INTERNAL_ERROR,
                        "ACP peer returned a malformed error object",
                    )
                    pending.error = AcpRemoteError(peer_error, method=pending.method)
                else:
                    pending.result = message.result
                pending.event.set()
                self._state.notify_all()
                return True
            self._deliver_notification_locked(message)
        return True

    def _deliver_notification_locked(self, message: AcpMessage) -> None:
        """Route one inbound notification to a subscription, else to the inbox.

        A subscribed session's updates reach the subscription only.  That is what
        makes a turn's outcome complete instead of a race against every other
        consumer of the shared inbox.
        """
        subscription: Optional[AcpSessionUpdates] = None
        session_id = _notification_session_id(message)
        if session_id is not None:
            candidate = self._subscriptions.get(session_id)
            if candidate is not None and not candidate._closed:
                subscription = candidate
        if subscription is not None:
            self._claim_update_locked(subscription, message)
        else:
            self._append_inbox_locked(message)
        self._state.notify_all()

    def _handle_inbound_request(self, message: AcpMessage) -> None:
        method = message.method or ""
        with self._state:
            already_answered = message.request_id in self._responded
            closed = self._closed
        if closed:
            return
        if already_answered:
            # Answering again would double-settle the peer's id, but staying
            # silent deadlocks a peer that reused one. Report the conflict so the
            # peer can fail fast instead of waiting forever.
            with self._state:
                conflict_id = self._conflict_request_id(message.request_id)
            self.send_error_response(
                conflict_id,
                JSONRPC_INTERNAL_ERROR,
                "ACP request id was already answered",
            )
            return
        responder = _InboundResponder(self, message.request_id)
        with self._state:
            handler = self._handlers.get(method)
        if handler is None:
            responder.refuse(JSONRPC_METHOD_NOT_FOUND, "unsupported ACP method: %s" % method)
            return
        try:
            claimed = bool(handler(message, responder.respond))
        except Exception:
            responder.refuse(JSONRPC_INTERNAL_ERROR, "ACP request handler failed")
            return
        if not claimed:
            responder.refuse(JSONRPC_METHOD_NOT_FOUND, "unsupported ACP method: %s" % method)

    def _conflict_request_id(self, request_id: Any) -> Any:
        """Return an id safe to echo for a conflicting request response."""
        if request_id in self._responded:
            # Already answered under this exact value, so echoing it is truthful
            # but would be deduplicated; use a distinguishable string form.
            return "conflict:%s" % request_id
        return request_id

    def _fail_locked(self, error: AcpError) -> None:
        for pending in self._pending.values():
            if not pending.event.is_set():
                pending.error = error
                pending.event.set()

    # -- stderr --

    def _drain_stderr(self, limit: int = 16 * 1024) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                with self._state:
                    remaining = limit - len(self._stderr_bytes)
                    if remaining > 0:
                        self._stderr_bytes.extend(chunk[:remaining])
                    elif not self._stderr_capped:
                        self._stderr_capped = True
        except (OSError, ValueError):  # pragma: no cover - pipe closed under us
            return


class _InboundResponder:
    """Answers exactly one inbound agent-to-client request."""

    def __init__(self, transport: AcpTransport, request_id: Any):
        self._transport = transport
        self._request_id = request_id

    @property
    def request_id(self) -> Any:
        return self._request_id

    def respond(self, result: Any) -> bool:
        return self._transport.send_response(self._request_id, result)

    def refuse(self, code: int, message: str) -> bool:
        return self._transport.send_error_response(self._request_id, code, message)


# --- Typed protocol payloads ------------------------------------------------


@dataclass(frozen=True)
class AcpInitializeResult:
    """The negotiated result of ``initialize``."""

    protocol_version: int
    agent_name: str = ""
    agent_version: str = ""
    prompt_capabilities: Mapping[str, Any] = field(default_factory=dict)
    session_capabilities: Mapping[str, Any] = field(default_factory=dict)
    mcp_capabilities: Mapping[str, Any] = field(default_factory=dict)
    auth_methods: tuple[Any, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def supports_images(self) -> bool:
        return self.prompt_capabilities.get("image") is True

    @property
    def supports_session_list(self) -> bool:
        return "list" in self.session_capabilities

    @property
    def supports_session_resume(self) -> bool:
        return "resume" in self.session_capabilities

    @property
    def supports_session_close(self) -> bool:
        return "close" in self.session_capabilities


@dataclass(frozen=True)
class AcpSession:
    """A live in-memory ACP session handle."""

    session_id: str
    workspace: Path
    config_options: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class AcpSessionSummary:
    """One entry of a ``session/list`` page."""

    session_id: str
    cwd: str
    title: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class AcpSessionPage:
    """One page of persisted, resumable sessions."""

    sessions: tuple[AcpSessionSummary, ...] = ()
    next_cursor: Optional[str] = None


@dataclass(frozen=True)
class AcpPermissionRequest:
    """An inbound ``session/request_permission`` awaiting a decision.

    The request is claimed by the client immediately; DSH stays blocked until
    :meth:`AcpClient.respond_permission` or
    :meth:`AcpClient.cancel_permission` sends the matching response.
    """

    request_id: Any
    session_id: str
    tool_call_id: str
    options: tuple[Mapping[str, Any], ...] = ()
    raw_params: Mapping[str, Any] = field(default_factory=dict)

    @property
    def option_ids(self) -> tuple[str, ...]:
        return tuple(
            str(option.get("optionId"))
            for option in self.options
            if isinstance(option, Mapping) and option.get("optionId") is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "session_id": self.session_id,
            "tool_call_id": self.tool_call_id,
            "options": [dict(option) for option in self.options],
        }


@dataclass(frozen=True)
class AcpPromptOutcome:
    """Settled result of one ``session/prompt`` call."""

    session_id: str
    stop_reason: str
    cancelled: bool
    usage: Optional[Mapping[str, Any]] = None
    updates: tuple[DshRuntimeEvent, ...] = ()
    text: str = ""
    truncated: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)

    @property
    def end_turn(self) -> bool:
        return self.stop_reason == AcpStopReason.END_TURN


# --- Normalization ----------------------------------------------------------


def _content_block_text(block: Any) -> str:
    if not isinstance(block, Mapping):
        return ""
    if block.get("type") == "text":
        return str(block.get("text") or "")
    if block.get("type") == "resource_link":
        return "[resource_link name=%s uri=%s]" % (block.get("name"), block.get("uri"))
    return ""


def _tool_call_content_text(content: Any) -> str:
    """Extract text from a ``tool_call_update.content`` list.

    DSH only produces ``type: "content"`` entries wrapping a standard content
    block; the other ACP variants (diff, terminal) are reported as a marker
    rather than dropped silently.
    """
    if not isinstance(content, Iterable) or isinstance(content, (str, bytes, Mapping)):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("type")
        if kind == "content":
            text = _content_block_text(item.get("content"))
            if text:
                parts.append(text)
        elif kind == "diff":
            parts.append("[diff %s]" % item.get("path"))
        elif kind == "terminal":
            parts.append("[terminal %s]" % item.get("terminalId"))
    return "".join(parts)


def normalize_session_update(
    params: Any,
    *,
    max_text_bytes: int,
) -> Optional[DshRuntimeEvent]:
    """Normalize one ``session/update`` payload into a ``DshRuntimeEvent``.

    Returns ``None`` when the payload cannot be attributed to an update at all.
    Unknown variants are preserved as ``unknown`` with a redacted payload so a
    newer DSH cannot silently terminate the stream.
    """
    if not isinstance(params, Mapping):
        return None
    update = params.get("update")
    if not isinstance(update, Mapping):
        return None
    kind = update.get("sessionUpdate")
    if not isinstance(kind, str) or not kind:
        return None

    truncated = False
    if kind in (SessionUpdateKind.AGENT_MESSAGE_CHUNK, SessionUpdateKind.AGENT_THOUGHT_CHUNK):
        text = _content_block_text(update.get("content"))
        encoded = text.encode("utf-8")
        if len(encoded) > max_text_bytes:
            text = encoded[:max_text_bytes].decode("utf-8", errors="replace")
            truncated = True
        event_type = "text" if kind == SessionUpdateKind.AGENT_MESSAGE_CHUNK else "thinking"
        data: dict[str, Any] = {"text": _redact_sensitive(text)}
        if update.get("messageId") is not None:
            data["message_id"] = str(update.get("messageId"))
        return DshRuntimeEvent(event_type, data, truncated=truncated)

    if kind == SessionUpdateKind.TOOL_CALL:
        # DSH uses `title` as the tool name and always sends kind="other".
        name = update.get("name") or update.get("title") or ""
        raw_input = update.get("rawInput")
        if raw_input is not None and not isinstance(raw_input, (dict, list, str)):
            raw_input = str(raw_input)
        data = {
            "call_id": _bounded_id(update.get("toolCallId")),
            "tool": _redact_sensitive(str(name))[:256],
            "input": _redact_structure(raw_input) if raw_input is not None else None,
            "kind": str(update.get("kind") or "")[:64],
            "status": str(update.get("status") or "")[:64],
        }
        return DshRuntimeEvent("tool_call", data)

    if kind == SessionUpdateKind.TOOL_CALL_UPDATE:
        status = str(update.get("status") or "")
        result = _redact_sensitive(_tool_call_content_text(update.get("content")))
        data = {
            "call_id": _bounded_id(update.get("toolCallId")),
            "status": "error" if status == "failed" else status or "unknown",
            "result": result,
            "title": _redact_sensitive(str(update.get("title") or ""))[:256],
        }
        return DshRuntimeEvent("tool_result", data)

    if kind == SessionUpdateKind.USAGE_UPDATE:
        return DshRuntimeEvent(
            "status",
            {
                "turn": None,
                "usage": {"used": update.get("used"), "size": update.get("size")},
            },
            phase="usage",
        )

    if kind == SessionUpdateKind.CONFIG_OPTION_UPDATE:
        options = update.get("configOptions")
        normalized = [_redact_structure(option) for option in options] if isinstance(options, list) else []
        return DshRuntimeEvent("status", {"config_options": normalized}, phase="config")

    source_type = _redact_sensitive(kind)[:ACP_MAX_SOURCE_TYPE_CHARS]
    payload = {key: value for key, value in update.items() if key != "sessionUpdate"}
    data = {}
    if params.get("sessionId") is not None:
        data["session_id"] = str(params.get("sessionId"))
    data["payload"] = _redact_structure(payload)
    return DshRuntimeEvent("unknown", data, source_type=source_type)


def _bounded_id(value: Any) -> str:
    return _redact_sensitive(str(value if value is not None else ""))[:256]


# --- Client -----------------------------------------------------------------


class AcpClient:
    """Session-oriented ACP client over an :class:`AcpTransport`.

    The client owns protocol semantics that DSH verifies (workspace, profile,
    one prompt per session) and converts ACP traffic into the shared
    ``DshRuntimeEvent`` vocabulary. It owns no process registry and no policy
    decision: permission requests are surfaced through ``on_permission`` and
    ``pending_permissions()``, and answered by the caller.
    """

    def __init__(
        self,
        transport: AcpTransport,
        config: DshRuntimeConfig,
        *,
        profile: str = "acp",
        client_name: str = ACP_CLIENT_NAME,
        client_version: str = ACP_CLIENT_VERSION,
        on_permission: Optional[AcpPermissionListener] = None,
    ):
        self.transport = transport
        self.config = config
        self.profile = config.validate_profile(profile)
        self.client_name = client_name
        self.client_version = client_version
        self.on_permission = on_permission
        self._lock = threading.Lock()
        self._sessions: dict[str, AcpSession] = {}
        self._inflight: set[str] = set()
        self._permissions: dict[Any, AcpPermissionRequest] = {}
        self._initialize_result: Optional[AcpInitializeResult] = None
        self.transport.handle_request(
            ClientMethod.SESSION_REQUEST_PERMISSION,
            self._claim_permission_request,
        )

    # -- handshake --

    @property
    def initialize_result(self) -> Optional[AcpInitializeResult]:
        return self._initialize_result

    def initialize(self, *, timeout_seconds: Optional[float] = None) -> AcpInitializeResult:
        """Perform the ``initialize`` handshake and verify the version."""
        result = self.transport.send_request(
            AgentMethod.INITIALIZE,
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                # The transport implements no ACP client extension, so it must
                # not claim one.
                "clientCapabilities": {},
                "clientInfo": {"name": self.client_name, "version": self.client_version},
            },
            timeout_seconds=timeout_seconds,
        )
        normalized = self._parse_initialize(result)
        if normalized.protocol_version != ACP_PROTOCOL_VERSION:
            raise AcpError(
                "unsupported_protocol_version",
                "ACP peer negotiated protocol version %s, expected %s"
                % (normalized.protocol_version, ACP_PROTOCOL_VERSION),
            )
        self._initialize_result = normalized
        return normalized

    @staticmethod
    def _parse_initialize(result: Any) -> AcpInitializeResult:
        if not isinstance(result, Mapping):
            raise AcpError("malformed_result", "ACP initialize returned a non-object result")
        version = result.get("protocolVersion")
        if not isinstance(version, int) or isinstance(version, bool):
            raise AcpError("malformed_result", "ACP initialize omitted protocolVersion")
        agent_info = result.get("agentInfo") if isinstance(result.get("agentInfo"), Mapping) else {}
        capabilities = result.get("agentCapabilities")
        capabilities = capabilities if isinstance(capabilities, Mapping) else {}
        prompt_capabilities = capabilities.get("promptCapabilities")
        session_capabilities = capabilities.get("sessionCapabilities")
        mcp_capabilities = capabilities.get("mcpCapabilities")
        auth_methods = result.get("authMethods")
        return AcpInitializeResult(
            protocol_version=version,
            agent_name=str(agent_info.get("name") or ""),
            agent_version=str(agent_info.get("version") or ""),
            prompt_capabilities=dict(prompt_capabilities) if isinstance(prompt_capabilities, Mapping) else {},
            session_capabilities=dict(session_capabilities) if isinstance(session_capabilities, Mapping) else {},
            mcp_capabilities=dict(mcp_capabilities) if isinstance(mcp_capabilities, Mapping) else {},
            auth_methods=tuple(auth_methods) if isinstance(auth_methods, list) else (),
            raw=dict(result),
        )

    # -- sessions --

    def new_session(
        self,
        workspace: str | Path,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> AcpSession:
        """Create a session in a validated workspace."""
        resolved = self.config.validate_workspace(workspace)
        result = self.transport.send_request(
            AgentMethod.SESSION_NEW,
            {"cwd": str(resolved), "mcpServers": []},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(result, Mapping):
            raise AcpError("malformed_result", "ACP session/new returned a non-object result")
        session_id = self._validate_remote_session_id(result.get("sessionId"))
        session = AcpSession(
            session_id=session_id,
            workspace=resolved,
            config_options=_config_options_tuple(result.get("configOptions")),
        )
        with self._lock:
            self._sessions[session_id] = session
        return session

    def resume_session(
        self,
        session_id: str,
        workspace: str | Path,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> AcpSession:
        """Resume a persisted session, verifying the canonical workspace."""
        remote_id = self._validate_remote_session_id(session_id)
        resolved = self.config.validate_workspace(workspace)
        result = self.transport.send_request(
            AgentMethod.SESSION_RESUME,
            {"sessionId": remote_id, "cwd": str(resolved), "mcpServers": []},
            timeout_seconds=timeout_seconds,
        )
        if result is not None and not isinstance(result, Mapping):
            raise AcpError("malformed_result", "ACP session/resume returned a non-object result")
        payload = result if isinstance(result, Mapping) else {}
        # DSH does not echo sessionId on resume; the request id is authoritative.
        session = AcpSession(
            session_id=remote_id,
            workspace=resolved,
            config_options=_config_options_tuple(payload.get("configOptions")),
        )
        with self._lock:
            self._sessions[remote_id] = session
        return session

    def list_sessions(
        self,
        *,
        cwd: str | Path | None = None,
        cursor: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
    ) -> AcpSessionPage:
        """List persisted, resumable root sessions."""
        self._require_capability("supports_session_list", "session/list")
        params: dict[str, Any] = {}
        if cwd is not None:
            params["cwd"] = str(self.config.validate_workspace(cwd))
        if cursor:
            params["cursor"] = str(cursor)
        result = self.transport.send_request(
            AgentMethod.SESSION_LIST,
            params,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(result, Mapping):
            raise AcpError("malformed_result", "ACP session/list returned a non-object result")
        raw_sessions = result.get("sessions")
        summaries: list[AcpSessionSummary] = []
        if isinstance(raw_sessions, list):
            for item in raw_sessions:
                if not isinstance(item, Mapping):
                    continue
                raw_id = item.get("sessionId")
                if not isinstance(raw_id, str) or not raw_id:
                    continue
                summaries.append(
                    AcpSessionSummary(
                        session_id=raw_id,
                        cwd=str(item.get("cwd") or ""),
                        title=str(item.get("title") or ""),
                        updated_at=str(item.get("updatedAt") or ""),
                    )
                )
        next_cursor = result.get("nextCursor")
        return AcpSessionPage(
            sessions=tuple(summaries),
            next_cursor=str(next_cursor) if isinstance(next_cursor, str) and next_cursor else None,
        )

    def set_config_option(
        self,
        session_id: str,
        config_id: str,
        value: Any,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> tuple[Mapping[str, Any], ...]:
        """Set an advertised model or reasoning-effort option."""
        remote_id = self._require_local_session(session_id)
        result = self.transport.send_request(
            AgentMethod.SESSION_SET_CONFIG_OPTION,
            {"sessionId": remote_id, "configId": str(config_id), "value": value},
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(result, Mapping):
            raise AcpError("malformed_result", "ACP session/set_config_option returned a non-object result")
        options = _config_options_tuple(result.get("configOptions"))
        with self._lock:
            current = self._sessions.get(remote_id)
            if current is not None:
                self._sessions[remote_id] = AcpSession(
                    session_id=current.session_id,
                    workspace=current.workspace,
                    config_options=options,
                )
        return options

    def close_session(
        self,
        session_id: str,
        *,
        timeout_seconds: Optional[float] = None,
    ) -> None:
        """Quiescently close one session without touching the others."""
        self._require_capability("supports_session_close", "session/close")
        remote_id = self._require_local_session(session_id)
        self.transport.send_request(
            AgentMethod.SESSION_CLOSE,
            {"sessionId": remote_id},
            timeout_seconds=timeout_seconds,
        )
        self._forget_session(remote_id)

    # -- prompt turn --

    def prompt(
        self,
        session_id: str,
        text: str,
        *,
        timeout_seconds: Optional[float] = None,
        cancel_event: Optional[threading.Event] = None,
        on_event: Optional[Callable[[DshRuntimeEvent], None]] = None,
    ) -> AcpPromptOutcome:
        """Send one prompt turn and collect its ordered updates.

        Only one prompt may be in flight per session; a second concurrent call
        fails locally rather than producing an ambiguous server response.

        The turn owns its own ``session/update`` stream: while the prompt is in
        flight the transport delivers this session's updates to a private
        subscription instead of the shared inbox, so the returned
        :attr:`AcpPromptOutcome.updates` is the complete, ordered turn even when
        another thread is also consuming notifications.  Frame ids already queued
        when the prompt starts are claimed, so the turn has no leading hole.

        Args:
            timeout_seconds: budget for the whole turn.
            cancel_event: cooperative cancellation signal.
            on_event: optional observer invoked once per event, in wire order,
                as the turn progresses. A projection layer that wants to stream
                live updates uses this instead of racing the same notifications
                out of the inbox; the outcome still carries every event, so the
                observer is a live view, not a substitute for the result.

        Returns:
            The settled outcome: ordered updates, accumulated text, stop reason,
            and usage. A cancelled turn reports ``cancelled``, never success.

        Raises:
            AcpError: on timeout, transport close, protocol failure, or when
                another prompt is already in flight for the session.
            DshValidationError: when the message fails local validation.
        """
        remote_id = self._require_local_session(session_id)
        message = self.config.validate_message(text)
        with self._lock:
            if remote_id in self._inflight:
                raise AcpError(
                    "prompt_in_flight",
                    "ACP session already has an in-flight prompt",
                )
            self._inflight.add(remote_id)
        events: list[DshRuntimeEvent] = []
        try:
            subscription = self.transport.open_session_updates(remote_id)
            pump = self._start_turn_pump(subscription, events, on_event)
            try:
                return self._run_prompt(
                    remote_id,
                    message,
                    subscription,
                    events,
                    on_event=on_event,
                    timeout_seconds=timeout_seconds,
                    cancel_event=cancel_event,
                )
            finally:
                if pump is not None:
                    stop, watcher = pump
                    stop.set()
                    watcher.join(timeout=1)
                # Claiming the subscription returns every frame still owned by
                # the turn, including one that arrived while the response was
                # being correlated.
                for frame in subscription.close():
                    self._append_turn_event(events, frame, on_event)
        finally:
            with self._lock:
                self._inflight.discard(remote_id)

    def _start_turn_pump(
        self,
        subscription: AcpSessionUpdates,
        events: list[DshRuntimeEvent],
        on_event: Optional[Callable[[DshRuntimeEvent], None]],
    ) -> Optional[tuple[threading.Event, threading.Thread]]:
        """Stream the turn's updates to ``on_event`` while it is still running.

        The turn owns its subscription and the prompt call blocks until the
        response arrives, so an observer would otherwise see the whole turn at
        once at the end.  This pump drains the subscription on a short interval
        and reports each event as it arrives; the outcome is still built from the
        same events, so nothing is reported twice.
        """
        if on_event is None:
            return None
        stop = threading.Event()

        def pump() -> None:
            while not stop.is_set():
                for frame in subscription.drain():
                    self._append_turn_event(events, frame, on_event)
                stop.wait(0.02)

        watcher = threading.Thread(
            target=pump,
            daemon=True,
            name="dsh-acp-turn-%s" % subscription.session_id[:8],
        )
        watcher.start()
        return stop, watcher

    def _run_prompt(
        self,
        remote_id: str,
        message: str,
        subscription: AcpSessionUpdates,
        events: list[DshRuntimeEvent],
        *,
        on_event: Optional[Callable[[DshRuntimeEvent], None]],
        timeout_seconds: Optional[float],
        cancel_event: Optional[threading.Event],
    ) -> AcpPromptOutcome:
        pending = _PendingRequest(
            request_id=None,
            method=AgentMethod.SESSION_PROMPT,
            deadline=None,
            cancellable=True,
        )
        try:
            result = self.transport.send_request(
                AgentMethod.SESSION_PROMPT,
                {"sessionId": remote_id, "prompt": [{"type": "text", "text": message}]},
                timeout_seconds=timeout_seconds,
                cancellable=True,
                cancel_event=cancel_event,
                pending=pending,
            )
        except AcpError as exc:
            if _is_cancellation_signal(exc, pending):
                for frame in subscription.drain():
                    self._append_turn_event(events, frame, on_event)
                return AcpPromptOutcome(
                    session_id=remote_id,
                    stop_reason=AcpStopReason.CANCELLED,
                    cancelled=True,
                    updates=tuple(events),
                )
            raise
        for frame in subscription.drain():
            self._append_turn_event(events, frame, on_event)

        payload = result if isinstance(result, Mapping) else {}
        stop_reason = payload.get("stopReason")
        if not isinstance(stop_reason, str) or not stop_reason:
            raise AcpError("malformed_result", "ACP session/prompt omitted stopReason")
        usage = payload.get("usage")
        text_parts = [
            str(event.data.get("text") or "")
            for event in events
            if event.type == "text"
        ]
        # The prompt response is the settlement signal, so it is also surfaced as
        # a terminal event for projection layers that only consume events.
        final_event = DshRuntimeEvent(
            "final",
            {"text": "".join(text_parts), "stop_reason": stop_reason},
        )
        if on_event is not None:
            # A projection layer must not be able to break a settled turn.
            try:
                on_event(final_event)
            except Exception:
                pass
        events.append(final_event)
        # A turn the caller cancelled is never reported as success, even when the
        # peer folds cancellation into a normal ``end_turn`` result. Completion
        # would otherwise let the caller import a user-aborted turn as a durable,
        # evaluated run. The peer's own reason stays visible in ``raw``.
        cancelled = stop_reason == AcpStopReason.CANCELLED or pending.cancel_requested
        return AcpPromptOutcome(
            session_id=remote_id,
            stop_reason=AcpStopReason.CANCELLED if cancelled else stop_reason,
            cancelled=cancelled,
            usage=dict(usage) if isinstance(usage, Mapping) else None,
            updates=tuple(events),
            text=final_event.data["text"],
            truncated=any(event.truncated for event in events),
            raw=dict(payload),
        )

    def _append_turn_event(
        self,
        events: list[DshRuntimeEvent],
        frame: AcpMessage,
        on_event: Optional[Callable[[DshRuntimeEvent], None]],
    ) -> None:
        """Normalize one subscribed frame into the turn, notifying the observer."""
        event = normalize_session_update(frame.params, max_text_bytes=self.config.max_event_bytes)
        if event is None:
            return
        if on_event is not None:
            on_event(event)
        events.append(event)

    def cancel(self, session_id: str) -> None:
        """Send ``session/cancel``. Unknown session ids are no-ops by contract."""
        remote_id = str(session_id)
        if not remote_id:
            return
        self.transport.send_notification(AgentMethod.SESSION_CANCEL, {"sessionId": remote_id})

    def take_notifications(self, *, timeout: Optional[float] = None) -> tuple[DshRuntimeEvent, ...]:
        """Drain queued ``session/update`` notifications outside a prompt call."""
        message = self.transport.take_notification(timeout=timeout)
        if message is None:
            return ()
        return self._normalize_notification(message)

    def _normalize_notification(self, message: AcpMessage) -> tuple[DshRuntimeEvent, ...]:
        if message.method != ClientMethod.SESSION_UPDATE:
            return ()
        event = normalize_session_update(message.params, max_text_bytes=self.config.max_event_bytes)
        return () if event is None else (event,)

    # -- permissions --

    def pending_permissions(self) -> tuple[AcpPermissionRequest, ...]:
        """Permission requests awaiting a decision, in arrival order."""
        with self._lock:
            return tuple(self._permissions.values())

    def respond_permission(self, request_id: Any, option_id: str) -> bool:
        """Select one advertised permission option exactly once.

        The answer is gated by this transport's own allowlist as well as by what
        the peer advertised, so a peer cannot widen the policy by offering (for
        example) an ``allow_always`` option: only an explicit once-only allow or
        reject is ever sent.
        """
        key = _hashable_request_id(request_id)
        with self._lock:
            request = self._permissions.get(key)
        if request is None:
            raise AcpError("unknown_permission_request", "no such pending permission request")
        if option_id not in (ACP_ALLOW_ONCE_OPTION_ID, ACP_REJECT_ONCE_OPTION_ID):
            raise DshValidationError("permission option is not accepted by this runtime")
        if option_id not in request.option_ids:
            raise DshValidationError("permission option was not offered by the peer")
        sent = self.transport.send_response(
            key,
            {"outcome": {"outcome": "selected", "optionId": option_id}},
        )
        if sent:
            self._forget_permission(key)
        return sent

    def cancel_permission(self, request_id: Any) -> bool:
        """Answer ``cancelled``, which DSH folds into its cancelled outcome."""
        key = _hashable_request_id(request_id)
        with self._lock:
            request = self._permissions.get(key)
        if request is None:
            raise AcpError("unknown_permission_request", "no such pending permission request")
        sent = self.transport.send_response(key, {"outcome": {"outcome": "cancelled"}})
        if sent:
            self._forget_permission(key)
        return sent

    def _claim_permission_request(
        self,
        message: AcpMessage,
        respond: Callable[[Any], None],
    ) -> bool:
        del respond  # The request is answered later, via respond_permission.
        params = message.params
        if not isinstance(params, Mapping):
            return False
        session_id = str(params.get("sessionId") or "").strip()
        if not session_id:
            # A peer-supplied request that names no session cannot be routed to an
            # owner, so nothing would ever answer it. Reject it here instead of
            # retaining an unanswerable entry for the life of the process.
            return False
        tool_call = params.get("toolCall")
        tool_call_id = ""
        if isinstance(tool_call, Mapping):
            tool_call_id = str(tool_call.get("toolCallId") or "")
        options = params.get("options")
        request = AcpPermissionRequest(
            request_id=message.request_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
            options=tuple(dict(option) for option in options if isinstance(option, Mapping))
            if isinstance(options, list)
            else (),
            raw_params=dict(params),
        )
        with self._lock:
            self._permissions[message.request_id] = request
            while len(self._permissions) > ACP_MAX_PENDING_PERMISSIONS:
                self._permissions.pop(next(iter(self._permissions)), None)
        listener = self.on_permission
        if listener is not None:
            # The listener runs outside the client lock: a projection layer that
            # takes its own lock must not be able to deadlock the reader thread.
            try:
                listener(request)
            except Exception:
                # A projection failure must not turn an answerable permission
                # request into a protocol error; the request stays pending.
                pass
        return True

    def forget_permissions_for_session(self, session_id: str) -> None:
        """Drop permissions an abandoned turn can no longer answer.

        Called when a turn settles so a later HTTP request cannot answer a
        permission request that belongs to a finished turn.
        """
        key = str(session_id)
        with self._lock:
            for request_id, request in list(self._permissions.items()):
                if request.session_id == key:
                    self._permissions.pop(request_id, None)

    def _forget_permission(self, request_id: Any) -> None:
        with self._lock:
            self._permissions.pop(request_id, None)

    # -- helpers --

    def session(self, session_id: str) -> Optional[AcpSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def _require_local_session(self, session_id: str) -> str:
        remote_id = self._validate_remote_session_id(session_id)
        with self._lock:
            known = remote_id in self._sessions
        if not known:
            raise AcpError("unknown_session", "ACP session is not open on this connection")
        return remote_id

    def _forget_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            self._inflight.discard(session_id)

    def _require_capability(self, attribute: str, method: str) -> None:
        if self._initialize_result is None:
            raise AcpError("not_initialized", "ACP initialize has not completed")
        if not getattr(self._initialize_result, attribute):
            raise AcpError(
                "unsupported_capability",
                "ACP peer did not advertise %s" % method,
            )

    @staticmethod
    def _validate_remote_session_id(value: Any) -> str:
        """Validate an opaque remote session id.

        ACP session ids are random UUIDs, not Headless ``session-...`` ids, so
        this enforces only the properties that matter: non-empty, bounded, and
        free of path separators and control characters.
        """
        if not isinstance(value, str):
            raise AcpError("invalid_session_id", "ACP session id must be a string")
        if (
            not value.strip()
            or value in {".", ".."}
            or len(value.encode("utf-8")) > 256
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise AcpError("invalid_session_id", "ACP session id is not a usable opaque identifier")
        return value


def _notification_session_id(message: AcpMessage) -> Optional[str]:
    """The session a ``session/update`` notification belongs to, if any."""
    if message.method != ClientMethod.SESSION_UPDATE:
        return None
    params = message.params
    if not isinstance(params, Mapping):
        return None
    session_id = params.get("sessionId")
    return session_id if isinstance(session_id, str) and session_id else None


def _is_session_update(message: AcpMessage, session_id: str) -> bool:
    return _notification_session_id(message) == session_id


def _config_options_tuple(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(dict(option) for option in value if isinstance(option, Mapping))
