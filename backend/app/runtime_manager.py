"""Long-lived ACP runtime ownership for the local DSH backend.

The manager is deliberately independent of FastAPI.  It owns one ACP process,
its sessions, and short-lived prompt operations; routes only project the queues
owned here into HTTP/SSE responses.
"""
from __future__ import annotations

import queue
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from auditrail.cli import _available_run_dir, _run_dir, _write_run_outputs
from auditrail.dsh_acp import (
    ACP_ALLOW_ONCE_OPTION_ID,
    ACP_REJECT_ONCE_OPTION_ID,
    AcpClient,
    AcpError,
    AcpProcess,
    AcpTransport,
    build_acp_argv,
    spawn_acp_process,
)
from auditrail.dsh_runtime import DshRuntimeConfig, DshValidationError
from auditrail.dsh_source import find_acp_session_file, load_session_response, source_meta
from auditrail.runtime import save_expectations

#: A settled operation stays queryable for status polling, but the registry is
#: bounded: a long-lived server must not accumulate prompt text and event queues
#: for every turn it ever ran.
MAX_RETAINED_OPERATIONS = 32

#: The only permission answers DSH offers and this runtime accepts.
PERMISSION_OPTION_IDS = (ACP_ALLOW_ONCE_OPTION_ID, ACP_REJECT_ONCE_OPTION_ID)


class AcpManagerError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class AcpOperation:
    operation_id: str
    session_id: str
    prompt: str
    queue: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    state: str = "starting"
    result: Any = None
    error: Optional[dict[str, Any]] = None
    run_id: Optional[str] = None
    acceptance_criteria: str = ""
    worker: Optional[threading.Thread] = None
    created_at: float = field(default_factory=time.time)

    def emit(self, event: str, data: Any) -> None:
        self.queue.put({"event": event, "data": data})


@dataclass
class ManagedSession:
    session_id: str
    workspace: Path
    client: AcpClient
    generation: int
    active_operation: Optional[str] = None
    state: str = "ready"
    last_error: Optional[str] = None


class AcpRuntimeManager:
    """Own a restartable ACP process and all sessions created through it."""

    def __init__(
        self,
        config: DshRuntimeConfig,
        *,
        profile: str = "acp",
        process_factory: Callable[..., AcpProcess] = spawn_acp_process,
        transport_factory: Callable[..., AcpTransport] = AcpTransport,
        client_factory: Callable[..., AcpClient] = AcpClient,
        run_root: Path | None = None,
        dsh_home: Path | None = None,
        max_concurrency: int = 1,
    ):
        self.config = config
        self.profile = config.validate_profile(profile)
        self.process_factory = process_factory
        self.transport_factory = transport_factory
        self.client_factory = client_factory
        self.run_root = Path(run_root) if run_root else None
        self.dsh_home = Path(dsh_home) if dsh_home else None
        if int(max_concurrency) <= 0:
            raise ValueError("max_concurrency must be positive")
        self.max_concurrency = int(max_concurrency)
        self._lock = threading.RLock()
        self._publish_lock = threading.Lock()
        self._process: Optional[AcpProcess] = None
        self._transport: Optional[AcpTransport] = None
        self._client: Optional[AcpClient] = None
        self._generation = 0
        self._sessions: dict[str, ManagedSession] = {}
        self._operations: dict[str, AcpOperation] = {}
        self._closed = False

    @property
    def process(self) -> Optional[AcpProcess]:
        with self._lock:
            return self._process

    def start(self, workspace: str | Path) -> None:
        """Start and handshake the ACP process without creating a session."""
        resolved = self.config.validate_workspace(workspace)
        with self._lock:
            self._start_locked(resolved)

    def restart(self, workspace: str | Path | None = None) -> None:
        """Force a process restart; existing sessions resume on their next use."""
        with self._lock:
            if self._transport is not None:
                self._transport.close(terminate=True, force=True)
            self._process = self._transport = self._client = None
            if workspace is not None:
                target: Path | None = Path(workspace)
            else:
                target = next(iter(self._sessions.values())).workspace if self._sessions else None
            if target is not None:
                self._start_locked(self.config.validate_workspace(target))

    def capabilities(self) -> dict[str, Any]:
        with self._lock:
            result = getattr(self._client, "initialize_result", None) if self._client else None
            return {
                "protocol_version": int(result.protocol_version) if result is not None else 1,
                "running": bool(self._process and self._process.poll() is None),
                "supports_images": bool(result and result.supports_images),
                "supports_session_list": bool(result and result.supports_session_list),
                "supports_session_resume": bool(result and result.supports_session_resume),
                "supports_session_close": bool(result and result.supports_session_close),
                "permission_options": list(PERMISSION_OPTION_IDS),
            }

    def _start_locked(self, workspace: Path) -> AcpClient:
        if self._closed:
            raise AcpManagerError("manager_closed", "ACP runtime manager is closed")
        if self._process is not None and self._process.poll() is None and self._client is not None:
            return self._client
        if self._transport is not None:
            try:
                self._transport.close(terminate=True, force=True)
            except Exception:
                pass
        argv = build_acp_argv(self.config, self.profile)
        process = self.process_factory(argv, cwd=workspace)
        transport = self.transport_factory(process)
        transport.start()
        client = self.client_factory(
            transport,
            self.config,
            profile=self.profile,
            on_permission=self._record_permission,
        )
        try:
            client.initialize(timeout_seconds=self.config.default_timeout_seconds)
        except Exception:
            transport.close(terminate=True, force=True)
            raise
        self._process, self._transport, self._client = process, transport, client
        self._generation += 1
        for session in self._sessions.values():
            session.state = "needs_resume"
            session.last_error = "ACP process restarted"
            session.client = client
        return client

    def _ensure_session_client(self, session: ManagedSession) -> AcpClient:
        with self._lock:
            client = self._start_locked(session.workspace)
            if session.generation != self._generation:
                try:
                    client.resume_session(
                        session.session_id,
                        session.workspace,
                        timeout_seconds=self.config.default_timeout_seconds,
                    )
                except Exception as exc:
                    session.state = "error"
                    session.last_error = str(exc)
                    raise AcpManagerError("resume_failed", "ACP session could not be resumed") from exc
                session.generation = self._generation
                session.client = client
                session.state = "ready"
            return client

    def create_session(self, workspace: str | Path, session_id: str | None = None) -> ManagedSession:
        try:
            resolved = self.config.validate_workspace(workspace)
        except DshValidationError as exc:
            raise AcpManagerError("invalid_workspace", str(exc)) from exc
        with self._lock:
            client = self._start_locked(resolved)
            try:
                if session_id:
                    session = client.resume_session(
                        session_id,
                        resolved,
                        timeout_seconds=self.config.default_timeout_seconds,
                    )
                else:
                    session = client.new_session(
                        resolved,
                        timeout_seconds=self.config.default_timeout_seconds,
                    )
            except AcpError as exc:
                raise AcpManagerError(exc.code, str(exc)) from exc
            managed = ManagedSession(session.session_id, resolved, client, self._generation)
            self._sessions[managed.session_id] = managed
            return managed

    def get_session(self, session_id: str) -> ManagedSession:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if session is None:
                raise AcpManagerError("unknown_session", "unknown ACP session")
            return session

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "session_id": session.session_id,
                    "workspace": str(session.workspace),
                    "state": session.state,
                    "active_operation": session.active_operation,
                    "last_error": session.last_error,
                }
                for session in self._sessions.values()
            ]

    def get_operation(self, operation_id: str) -> AcpOperation:
        with self._lock:
            operation = self._operations.get(str(operation_id))
            if operation is None:
                raise AcpManagerError("unknown_operation", "unknown ACP operation")
            return operation

    def prompt(self, session_id: str, text: str, *, acceptance_criteria: str = "") -> AcpOperation:
        with self._lock:
            session = self._sessions.get(str(session_id))
            if session is None:
                raise AcpManagerError("unknown_session", "unknown ACP session")
            if session.active_operation:
                raise AcpManagerError("prompt_in_flight", "ACP session already has an active prompt")
            active_count = sum(1 for item in self._operations.values() if not item.done.is_set())
            if active_count >= self.max_concurrency:
                raise AcpManagerError("runtime_busy", "local ACP runtime is at capacity")
            operation = AcpOperation(
                uuid.uuid4().hex,
                session.session_id,
                str(text),
                acceptance_criteria=str(acceptance_criteria or ""),
            )
            session.active_operation = operation.operation_id
            self._operations[operation.operation_id] = operation
            self._prune_operations_locked()
            operation.worker = threading.Thread(
                target=self._run_prompt,
                args=(session, operation),
                daemon=True,
                name="dsh-acp-prompt-%s" % operation.operation_id[:8],
            )
            operation.worker.start()
            return operation

    def _prune_operations_locked(self) -> None:
        """Drop the oldest settled operations once the retention bound is passed.

        Active operations are never pruned, and an operation that is still being
        streamed keeps working because the SSE projection holds its own reference.
        """
        settled = [item for item in self._operations.values() if item.done.is_set()]
        excess = len(settled) - MAX_RETAINED_OPERATIONS
        if excess <= 0:
            return
        settled.sort(key=lambda item: item.created_at)
        for item in settled[:excess]:
            self._operations.pop(item.operation_id, None)

    def _run_prompt(self, session: ManagedSession, operation: AcpOperation) -> None:
        operation.state = "running"
        operation.emit("started", {"operation_id": operation.operation_id, "session_id": session.session_id})
        monitor_stop = threading.Event()
        # The monitor thread and ``AcpClient.prompt`` drain the same notification
        # inbox, so neither one sees every chunk: text is accumulated from what
        # this manager actually emitted rather than from the client's own (racy)
        # ``outcome.text``.
        text_parts: list[str] = []
        try:
            client = self._ensure_session_client(session)

            def emit_event(event: Any) -> None:
                if event.type == "text":
                    text_parts.append(str(event.data.get("text") or ""))
                operation.emit("runtime_event", event.to_dict())

            def monitor() -> None:
                while not monitor_stop.is_set() and not operation.done.is_set():
                    for event in client.take_notifications(timeout=0.05):
                        emit_event(event)

            watcher = threading.Thread(
                target=monitor,
                daemon=True,
                name="dsh-acp-events-%s" % operation.operation_id[:8],
            )
            watcher.start()
            try:
                outcome = client.prompt(
                    session.session_id,
                    operation.prompt,
                    timeout_seconds=self.config.max_timeout_seconds,
                    cancel_event=operation.cancel_event,
                )
            finally:
                monitor_stop.set()
                watcher.join(timeout=1)
            for event in outcome.updates:
                emit_event(event)
            operation.result = outcome
            operation.state = "cancelled" if outcome.cancelled else "completed"
            operation.emit(
                "completed",
                {
                    "session_id": session.session_id,
                    "stop_reason": outcome.stop_reason,
                    "text": "".join(text_parts),
                    "cancelled": outcome.cancelled,
                },
            )
            if not outcome.cancelled:
                self._settle_import(session, operation)
        except AcpManagerError as exc:
            self._cancel_pending_permissions(session)
            operation.state = "failed"
            operation.error = {"code": exc.code, "detail": str(exc), "session_id": session.session_id}
            operation.emit("error", operation.error)
        except DshValidationError as exc:
            # ``AcpClient.prompt`` validates the message before it is sent, so an
            # oversized or NUL-bearing prompt fails locally with a precise code.
            self._cancel_pending_permissions(session)
            operation.state = "failed"
            operation.error = {
                "code": "invalid_message",
                "detail": str(exc),
                "session_id": session.session_id,
            }
            operation.emit("error", operation.error)
        except AcpError as exc:
            self._cancel_pending_permissions(session)
            operation.state = "cancelled" if exc.code == "cancelled" else "failed"
            operation.error = {"code": exc.code, "detail": str(exc), "session_id": session.session_id}
            operation.emit("error", operation.error)
        except Exception:
            self._cancel_pending_permissions(session)
            operation.state = "failed"
            operation.error = {
                "code": "runtime_error",
                "detail": "ACP runtime operation failed",
                "session_id": session.session_id,
            }
            operation.emit("error", operation.error)
        finally:
            monitor_stop.set()
            operation.done.set()
            with self._lock:
                if session.active_operation == operation.operation_id:
                    session.active_operation = None

    def _settle_import(self, session: ManagedSession, operation: AcpOperation) -> None:
        """Publish the durable run, or report an explicit import-stage error.

        The turn is already settled when this runs: a failure here means no
        auditrail run was produced, so the operation must not look successful.
        """
        try:
            run_id = self._import_durable(session, operation)
        except AcpManagerError as exc:
            error: dict[str, Any] = {
                "code": exc.code,
                "detail": str(exc),
                "stage": "import",
                "session_id": session.session_id,
            }
        except Exception:
            error = {
                "code": "import_failed",
                "detail": "durable DSH session import failed",
                "stage": "import",
                "session_id": session.session_id,
            }
        else:
            operation.run_id = run_id
            operation.emit("run_imported", {"run_id": run_id, "session_id": session.session_id})
            return
        operation.state = "failed"
        operation.error = error
        operation.emit("error", error)

    def _import_durable(self, session: ManagedSession, operation: AcpOperation) -> str:
        """Import the authoritative ACP recording as a new run directory.

        The ACP session id is an opaque UUID, so the recording is resolved by id
        and then verified against the live session's id and workspace before
        anything is written.  The run is staged and published atomically so a
        partially written run directory is never visible to the dashboard.
        """
        if self.run_root is None or self.dsh_home is None:
            raise AcpManagerError("import_failed", "ACP durable import is not configured")
        try:
            session_path = find_acp_session_file(self.dsh_home, session.session_id)
        except (ValueError, FileNotFoundError) as exc:
            raise AcpManagerError(
                "import_failed",
                "no durable DSH recording for this ACP session",
            ) from exc
        try:
            response = load_session_response(session_path)
        except Exception as exc:
            raise AcpManagerError("import_failed", "durable DSH recording could not be read") from exc
        durable_session = response.get("session") or {}
        if durable_session.get("session_id") != session.session_id:
            raise AcpManagerError(
                "import_failed",
                "durable DSH session id does not match the ACP session",
            )
        durable_workspace = Path(str(durable_session.get("workspace") or "")).resolve(strict=False)
        if durable_workspace != session.workspace:
            raise AcpManagerError(
                "import_failed",
                "durable DSH session workspace does not match the ACP session",
            )

        metadata = source_meta(response)
        metadata.update(
            {"interaction_mode": "auditrail-chat", "dsh_profile": self.profile, "session_role": "evaluand"}
        )
        self.run_root.mkdir(parents=True, exist_ok=True)
        staging: Path | None = Path(tempfile.mkdtemp(prefix=".dsh-acp-import-", dir=self.run_root))
        published: Path | None = None
        try:
            _write_run_outputs(staging, response, source=metadata)
            save_expectations(staging, operation.acceptance_criteria)
            with self._publish_lock:
                published = _available_run_dir(_run_dir(self.run_root, "dsh-%s" % session.session_id))
                staging.rename(published)
                staging = None
        except Exception:
            if published is not None and published.exists():
                shutil.rmtree(published, ignore_errors=True)
            raise
        finally:
            if staging is not None and staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        return published.name

    def _record_permission(self, permission: Any) -> None:
        """Push a newly claimed permission request to its owning operation.

        The request is pushed by the transport as soon as DSH asks, instead of
        being polled, so a fast answer cannot make the SSE event disappear.
        """
        with self._lock:
            session = self._sessions.get(permission.session_id)
            operation_id = session.active_operation if session is not None else None
            operation = self._operations.get(operation_id) if operation_id else None
        if operation is None:
            return
        payload = permission.to_dict()
        payload["operation_id"] = operation.operation_id
        operation.emit("permission_requested", payload)

    def _cancel_pending_permissions(self, session: ManagedSession) -> None:
        """Answer every unanswered permission request instead of leaving DSH blocked."""
        try:
            pending = session.client.pending_permissions()
        except Exception:
            return
        for permission in pending:
            if permission.session_id and permission.session_id != session.session_id:
                continue
            try:
                session.client.cancel_permission(permission.request_id)
            except Exception:
                continue

    def respond_permission(self, operation_id: str, request_id: Any, option_id: str) -> None:
        operation = self.get_operation(operation_id)
        session = self.get_session(operation.session_id)
        if option_id not in PERMISSION_OPTION_IDS:
            raise AcpManagerError(
                "invalid_permission_option",
                "only %s are supported" % " or ".join(PERMISSION_OPTION_IDS),
            )
        if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
            raise AcpManagerError(
                "unknown_permission_request",
                "permission request id must be a string or integer",
            )
        pending = {item.request_id: item for item in session.client.pending_permissions()}
        request = pending.get(request_id)
        if request is None:
            raise AcpManagerError("unknown_permission_request", "no such pending permission request")
        if request.session_id and request.session_id != session.session_id:
            raise AcpManagerError(
                "unknown_permission_request",
                "permission request belongs to another ACP session",
            )
        try:
            session.client.respond_permission(request_id, option_id)
        except AcpError as exc:
            raise AcpManagerError(exc.code, str(exc)) from exc
        except ValueError as exc:
            raise AcpManagerError(
                "invalid_permission_option",
                "permission option was not offered by the peer",
            ) from exc

    def cancel(self, operation_id: str) -> None:
        operation = self.get_operation(operation_id)
        if operation.done.is_set():
            return
        operation.cancel_event.set()
        operation.state = "cancelling"
        session = self.get_session(operation.session_id)
        self._cancel_pending_permissions(session)
        try:
            session.client.cancel(operation.session_id)
        except Exception:
            pass

    def close_session(self, session_id: str) -> None:
        """Quiescently close one session; the local handle goes away either way.

        Closing is best-effort: a peer that already lost the connection owns no
        live session for this id, and the ACP process closes every session it
        still owns when the connection ends, so a dropped handle cannot leak.
        """
        session = self.get_session(session_id)
        if session.active_operation:
            self.cancel(session.active_operation)
        try:
            session.client.close_session(
                session.session_id,
                timeout_seconds=self.config.default_timeout_seconds,
            )
        except Exception:
            pass
        with self._lock:
            self._sessions.pop(session.session_id, None)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            operations = list(self._operations.values())
            sessions = list(self._sessions)
        for operation in operations:
            try:
                self.cancel(operation.operation_id)
            except AcpManagerError:
                continue
        for operation in operations:
            if operation.worker:
                operation.worker.join(timeout=2)
        with self._lock:
            for session_id in sessions:
                self._sessions.pop(session_id, None)
            if self._transport:
                try:
                    self._transport.close(terminate=True, force=True)
                except Exception:
                    pass
            self._process = self._transport = self._client = None
