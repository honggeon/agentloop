"""Local DSH Headless chat orchestration and SSE projection."""

from __future__ import annotations

import asyncio
import json
import queue
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from auditrail.cli import _run_dir, _write_run_outputs
from auditrail.dsh_headless import DshHeadlessClient
from auditrail.dsh_runtime import DshExecutionResult, DshRunRequest, DshRuntimeEvent, DshValidationError, validate_request
from auditrail.dsh_source import find_session_file, load_session_response, source_meta
from auditrail.runtime import build_runtime_graph, build_runtime_trace, save_expectations
from auditrail.trajectory import messages_from_session_response
from backend.app.config import build_dsh_runtime_config, get_settings
from backend.app.routes.runs import _enrich_run_item, _run_item, get_batch_summary


router = APIRouter(prefix="/api/dsh")


class OperationConflictError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class ActiveDshOperation:
    operation_id: str
    workspace: Path
    requested_session_id: str | None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    session_id: str | None = None
    started_at: float = field(default_factory=time.time)
    state: str = "starting"
    worker: threading.Thread | None = None
    reaper_started: bool = False


class ActiveDshOperationRegistry:
    """Own active local operations and enforce session/concurrency invariants."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operations: dict[str, ActiveDshOperation] = {}
        self._sessions: dict[str, str] = {}

    def register(self, *, workspace: Path, session_id: str | None, max_concurrency: int) -> ActiveDshOperation:
        with self._lock:
            if session_id and session_id in self._sessions:
                raise OperationConflictError("session_busy", "DSH session already has an active turn")
            if len(self._operations) >= max_concurrency:
                raise OperationConflictError("runtime_busy", "local DSH runtime is at capacity")
            operation = ActiveDshOperation(
                operation_id=uuid.uuid4().hex,
                workspace=workspace,
                requested_session_id=session_id,
                session_id=session_id,
            )
            self._operations[operation.operation_id] = operation
            if session_id:
                self._sessions[session_id] = operation.operation_id
            return operation

    def claim_session(self, operation_id: str, session_id: str) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise OperationConflictError("operation_missing", "DSH operation is no longer active")
            owner = self._sessions.get(session_id)
            if owner is not None and owner != operation_id:
                raise OperationConflictError("session_busy", "DSH session already has an active turn")
            previous = operation.session_id
            if previous and previous != session_id and self._sessions.get(previous) == operation_id:
                self._sessions.pop(previous, None)
            operation.session_id = session_id
            self._sessions[session_id] = operation_id

    def attach_worker(self, operation_id: str, worker: threading.Thread) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise OperationConflictError("operation_missing", "DSH operation is no longer active")
            operation.worker = worker
            operation.state = "running"

    def cancel(self, operation_id: str) -> ActiveDshOperation | None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                return None
            operation.state = "cancelling"
            operation.cancel_event.set()
            return operation

    def finish(self, operation_id: str) -> None:
        with self._lock:
            operation = self._operations.pop(operation_id, None)
            if operation and operation.session_id and self._sessions.get(operation.session_id) == operation_id:
                self._sessions.pop(operation.session_id, None)

    def settle(self, operation_id: str, join_timeout: float = 1.0) -> bool:
        operation = self.get(operation_id)
        if operation is None:
            return True
        worker = operation.worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(join_timeout, 0.0))
        if worker is None or not worker.is_alive():
            self.finish(operation_id)
            return True
        self._start_reaper(operation)
        return False

    def _start_reaper(self, operation: ActiveDshOperation) -> None:
        with self._lock:
            current = self._operations.get(operation.operation_id)
            if current is None or current.reaper_started:
                return
            current.reaper_started = True
            current.state = "reaping"

        def reap() -> None:
            worker = operation.worker
            if worker is not None:
                worker.join()
            self.finish(operation.operation_id)

        threading.Thread(
            target=reap,
            name="dsh-reaper-%s" % operation.operation_id[:8],
            daemon=True,
        ).start()

    def cancel_all(self, join_timeout: float = 5.0) -> None:
        with self._lock:
            operations = list(self._operations.values())
            for operation in operations:
                operation.state = "cancelling"
                operation.cancel_event.set()
        deadline = time.monotonic() + max(join_timeout, 0.0)
        for operation in operations:
            worker = operation.worker
            if worker is not None and worker.is_alive():
                worker.join(timeout=max(deadline - time.monotonic(), 0.0))
        for operation in operations:
            worker = operation.worker
            if worker is not None and worker.is_alive():
                self._start_reaper(operation)
            else:
                self.finish(operation.operation_id)

    def get(self, operation_id: str) -> ActiveDshOperation | None:
        with self._lock:
            return self._operations.get(operation_id)


active_operations = ActiveDshOperationRegistry()
_run_publish_lock = threading.Lock()


@router.post("/chat")
async def dsh_chat(request: Request) -> StreamingResponse:
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        payload = None
    return StreamingResponse(_chat_event_stream(payload), media_type="text/event-stream")


@router.post("/chat/{operation_id}/cancel")
def cancel_dsh_chat(operation_id: str) -> dict[str, Any]:
    operation = active_operations.cancel(operation_id)
    if operation is None:
        raise HTTPException(status_code=404, detail="active DSH operation not found")
    return {
        "ok": True,
        "operation_id": operation.operation_id,
        "session_id": operation.session_id,
        "state": operation.state,
    }


def shutdown_active_operations() -> None:
    active_operations.cancel_all()


async def _chat_event_stream(payload: Any) -> AsyncIterator[str]:
    if not isinstance(payload, dict):
        yield _error_event("invalid_request", "DSH request body must be an object", stage="validate")
        return
    settings = get_settings()
    operation: ActiveDshOperation | None = None
    worker: threading.Thread | None = None
    try:
        try:
            config = build_dsh_runtime_config(settings)
            request = _request_from_payload(payload)
            invocation = validate_request(config, request)
            request = DshRunRequest(
                message=invocation.message,
                profile=invocation.profile,
                workspace=invocation.workspace,
                session_id=invocation.session_id,
                timeout_seconds=invocation.timeout_seconds,
            )
            await asyncio.to_thread(_validate_resume_workspace, settings.dsh_home, request)
        except (DshValidationError, FileNotFoundError, TypeError, ValueError) as exc:
            yield _error_event("invalid_request", str(exc), stage="validate")
            return

        try:
            operation = active_operations.register(
                workspace=request.workspace,
                session_id=request.session_id,
                max_concurrency=settings.dsh_max_concurrency,
            )
        except OperationConflictError as exc:
            yield _error_event(exc.code, str(exc), stage="register", session_id=request.session_id)
            return

        event_queue: queue.Queue[tuple[str, Any]] = queue.Queue()

        def execute() -> None:
            try:
                result = client.run(
                    request,
                    cancel_event=operation.cancel_event,
                    on_event=lambda event: event_queue.put(("event", event)),
                )
                event_queue.put(("result", result))
            except Exception:
                event_queue.put(("exception", "local DSH execution failed"))

        try:
            client = DshHeadlessClient(config)
            worker = threading.Thread(
                target=execute,
                name="dsh-operation-%s" % operation.operation_id[:8],
                daemon=True,
            )
            active_operations.attach_worker(operation.operation_id, worker)
            worker.start()
        except Exception:
            yield _error_event(
                "execution_failed",
                "local DSH execution failed to start",
                stage="execute",
                operation_id=operation.operation_id,
                session_id=operation.session_id,
            )
            return

        yield _sse(
            "run_started",
            {
                "operation_id": operation.operation_id,
                "session_id": request.session_id,
                "run_id": "dsh-%s" % operation.operation_id,
                "provider": "dsh",
            },
        )

        messages: list[dict[str, Any]] = [{"role": "user", "content": request.message}]
        result: DshExecutionResult | None = None
        projection_error: OperationConflictError | None = None
        while result is None:
            try:
                kind, value = event_queue.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if kind == "exception":
                yield _error_event(
                    "execution_failed",
                    str(value),
                    stage="execute",
                    operation_id=operation.operation_id,
                    session_id=operation.session_id,
                )
                return
            if kind == "result":
                result = value
                break

            event: DshRuntimeEvent = value
            if event.type == "session":
                emitted_session_id = str(event.data.get("session_id") or "")
                try:
                    active_operations.claim_session(operation.operation_id, emitted_session_id)
                except OperationConflictError as exc:
                    projection_error = exc
                    operation.cancel_event.set()
            _project_runtime_event(messages, event)
            yield _sse(
                "agent_event",
                {
                    "operation_id": operation.operation_id,
                    "session_id": operation.session_id,
                    "event": event.to_dict(),
                },
            )
            for projected_event in _projection_events(operation, messages, request.profile):
                yield projected_event

        if projection_error is not None:
            yield _error_event(
                projection_error.code,
                str(projection_error),
                stage="register",
                operation_id=operation.operation_id,
                session_id=operation.session_id,
            )
            return
        if not result.succeeded:
            error = result.error
            yield _error_event(
                error.code if error is not None else "execution_failed",
                error.message if error is not None else "local DSH execution did not complete",
                stage="execute",
                operation_id=operation.operation_id,
                session_id=result.session_id or operation.session_id,
                cancelled=result.cancelled,
                timed_out=result.timed_out,
            )
            return

        session_id = str(result.session_id)
        import_task = asyncio.create_task(
            asyncio.to_thread(
                _import_durable_run,
                settings,
                request,
                session_id,
                str(payload.get("acceptance_criteria") or ""),
            )
        )
        try:
            imported = await asyncio.shield(import_task)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(import_task)
            except Exception:
                pass
            raise
        except Exception:
            yield _error_event(
                "import_failed",
                "durable DSH session import failed",
                stage="import",
                operation_id=operation.operation_id,
                session_id=session_id,
            )
            return

        run_dir = imported["run_dir"]
        yield _sse("graph_update", {"run_id": run_dir.name, "graph": imported["graph"]})
        yield _sse("trace_update", {"run_id": run_dir.name, "trace": imported["trace"]})
        yield _sse("audit_update", {"run_id": run_dir.name, "audit": imported["audit"]})
        if worker is not None and worker.is_alive():
            await asyncio.to_thread(worker.join)
        active_operations.finish(operation.operation_id)
        yield _sse(
            "run_finished",
            {
                "operation_id": operation.operation_id,
                "session_id": session_id,
                "run_id": run_dir.name,
                "summary": imported["summary"],
                "run": imported["run"],
                "batch_summary": imported["batch_summary"],
            },
        )
    finally:
        if operation is not None and active_operations.get(operation.operation_id) is not None:
            if worker is not None and worker.is_alive():
                operation.cancel_event.set()
            active_operations.settle(operation.operation_id)


def _import_durable_run(
    settings: Any,
    request: DshRunRequest,
    session_id: str,
    acceptance_criteria: str,
) -> dict[str, Any]:
    staging_dir: Path | None = None
    published_dir: Path | None = None
    try:
        session_path = find_session_file(settings.dsh_home, session_id)
        session_response = load_session_response(session_path)
        durable_session = session_response.get("session") or {}
        if durable_session.get("session_id") != session_id:
            raise ValueError("durable DSH session id does not match execution result")
        durable_workspace = Path(str(durable_session.get("workspace") or "")).resolve(strict=False)
        if durable_workspace != request.workspace:
            raise ValueError("durable DSH session workspace does not match the request")

        settings.runs_dir.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix=".dsh-import-", dir=settings.runs_dir))
        metadata = source_meta(session_response)
        metadata.update(
            {"interaction_mode": "auditrail-chat", "dsh_profile": request.profile, "session_role": "evaluand"}
        )
        _write_run_outputs(staging_dir, session_response, source=metadata)
        save_expectations(staging_dir, acceptance_criteria)
        durable_messages = messages_from_session_response(session_response)
        audit = json.loads((staging_dir / "audit.json").read_text(encoding="utf-8"))
        summary = json.loads((staging_dir / "summary.json").read_text(encoding="utf-8"))
        graph = build_runtime_graph(durable_messages)
        trace = build_runtime_trace(
            durable_messages,
            trace_id=session_id,
            session_id=session_id,
            agent_name=str(durable_session.get("title") or "DSH Runtime"),
            agent_version=str(durable_session.get("model") or ""),
        )

        with _run_publish_lock:
            base = _run_dir(settings.runs_dir, "dsh-%s" % session_id.removeprefix("session-"))
            published_dir = _available_run_dir(base)
            staging_dir.rename(published_dir)
            staging_dir = None
        run = _enrich_run_item(_run_item(published_dir))
        batch_summary = get_batch_summary()
        return {
            "run_dir": published_dir,
            "audit": audit,
            "summary": summary,
            "graph": graph,
            "trace": trace,
            "run": run,
            "batch_summary": batch_summary,
        }
    except Exception:
        if published_dir is not None and published_dir.exists():
            shutil.rmtree(published_dir)
        raise
    finally:
        if staging_dir is not None and staging_dir.exists():
            shutil.rmtree(staging_dir)


def _available_run_dir(base: Path) -> Path:
    if not base.exists():
        return base
    suffix = 2
    while True:
        candidate = base.with_name("%s-%s" % (base.name, suffix))
        if not candidate.exists():
            return candidate
        suffix += 1


def _validate_resume_workspace(dsh_home: Path, request: DshRunRequest) -> None:
    if request.session_id is None:
        return
    try:
        session_path = find_session_file(dsh_home, request.session_id)
        session_response = load_session_response(session_path)
    except (FileNotFoundError, ValueError) as exc:
        raise DshValidationError("DSH session is not available for resume") from exc
    session = session_response.get("session") or {}
    durable_workspace = Path(str(session.get("workspace") or "")).resolve(strict=False)
    if durable_workspace != request.workspace:
        raise DshValidationError("DSH session belongs to a different workspace")


def _request_from_payload(payload: dict[str, Any]) -> DshRunRequest:
    message = payload.get("message")
    profile = payload.get("profile")
    workspace = payload.get("workspace")
    session_id = payload.get("session_id")
    acceptance_criteria = payload.get("acceptance_criteria")
    if not isinstance(message, str):
        raise DshValidationError("DSH message must be a non-empty string")
    if not isinstance(profile, str):
        raise DshValidationError("DSH profile is not allowed")
    if not isinstance(workspace, str):
        raise DshValidationError("DSH workspace must be an absolute path")
    if session_id is not None and not isinstance(session_id, str):
        raise DshValidationError("invalid DSH session id")
    if acceptance_criteria is not None and not isinstance(acceptance_criteria, str):
        raise DshValidationError("DSH acceptance criteria must be a string")
    return DshRunRequest(
        message=message,
        profile=profile,
        workspace=Path(workspace),
        session_id=session_id,
        timeout_seconds=payload.get("timeout"),
    )


def _project_runtime_event(messages: list[dict[str, Any]], event: DshRuntimeEvent) -> None:
    if event.type == "thinking":
        messages.append({"role": "assistant", "content": "", "reasoning": str(event.data.get("text") or "")})
    elif event.type == "text":
        text = str(event.data.get("text") or "")
        if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("tool_calls") and not messages[-1].get("reasoning"):
            messages[-1]["content"] = str(messages[-1].get("content") or "") + text
        else:
            messages.append({"role": "assistant", "content": text})
    elif event.type == "tool_call":
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": event.data.get("call_id"),
                        "name": event.data.get("tool"),
                        "arguments": event.data.get("input"),
                    }
                ],
            }
        )
    elif event.type == "tool_result":
        messages.append(
            {
                "role": "tool",
                "tool_call_id": event.data.get("call_id"),
                "tool_name": event.data.get("tool"),
                "status": event.data.get("status"),
                "content": event.data.get("result") or "",
            }
        )
    elif event.type == "final":
        messages[:] = [
            message
            for message in messages
            if not (
                message.get("role") == "assistant"
                and not message.get("tool_calls")
                and not message.get("reasoning")
                and message.get("content")
            )
        ]
        messages.append({"role": "assistant", "content": str(event.data.get("text") or "")})


def _projection_events(operation: ActiveDshOperation, messages: list[dict[str, Any]], profile: str) -> Iterable[str]:
    trace_id = operation.session_id or operation.operation_id
    run_id = "dsh-%s" % operation.operation_id
    yield _sse("graph_update", {"run_id": run_id, "graph": build_runtime_graph(messages)})
    yield _sse(
        "trace_update",
        {
            "run_id": run_id,
            "trace": build_runtime_trace(
                messages,
                trace_id=trace_id,
                session_id=operation.session_id or "",
                agent_name="DSH Runtime",
                agent_version=profile,
            ),
        },
    )


def _error_event(
    code: str,
    detail: str,
    *,
    stage: str,
    operation_id: str | None = None,
    session_id: str | None = None,
    **extra: Any,
) -> str:
    payload: dict[str, Any] = {"code": code, "detail": detail, "stage": stage}
    if operation_id:
        payload["operation_id"] = operation_id
    if session_id:
        payload["session_id"] = session_id
    payload.update(extra)
    return _sse("error", payload)


def _sse(event: str, payload: Any) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(payload, ensure_ascii=False, default=str))
