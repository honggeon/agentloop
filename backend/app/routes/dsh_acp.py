"""HTTP/SSE surface for the long-lived ACP runtime.

Contract (see README "Local DSH ACP runtime"):

- ``POST /api/dsh/acp/prompt`` registers one prompt operation and streams SSE.
  Pre-stream validation and registry conflicts are ordinary JSON errors; every
  failure that can only be known while streaming is an ``event: error``.
- SSE events, in order: ``started``, zero or more ``runtime_event`` /
  ``permission_requested``, ``completed`` (the ACP turn settled), then exactly
  one of ``run_imported`` (a run directory was published) or ``error`` with
  ``stage="import"`` (no run was published; the session id is preserved).
- An operation is successful only when ``run_imported`` arrives.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from auditrail.dsh_acp import ACP_ALLOW_ONCE_OPTION_ID, ACP_REJECT_ONCE_OPTION_ID
from backend.app.config import build_dsh_runtime_config, get_settings
from backend.app.runtime_manager import (
    AcpManagerError,
    AcpOperation,
    AcpRuntimeManager,
)

router = APIRouter(prefix="/api/dsh/acp")
_manager_lock = threading.Lock()
_manager: AcpRuntimeManager | None = None


def set_acp_runtime_manager(manager: AcpRuntimeManager | None) -> None:
    """Inject a deterministic manager in tests or clear the lazy singleton."""
    global _manager
    with _manager_lock:
        _manager = manager


def get_acp_runtime_manager() -> AcpRuntimeManager:
    global _manager
    settings = get_settings()
    if not settings.dsh_runtime_enabled or not settings.dsh_acp_enabled:
        raise HTTPException(status_code=503, detail="ACP runtime is disabled")
    with _manager_lock:
        if _manager is None:
            try:
                config = build_dsh_runtime_config(settings)
            except ValueError as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            _manager = AcpRuntimeManager(
                config,
                profile="acp",
                run_root=settings.runs_dir,
                dsh_home=settings.dsh_home,
                max_concurrency=settings.dsh_max_concurrency,
            )
        return _manager


def _manager_error(exc: AcpManagerError) -> HTTPException:
    if exc.code in {"unknown_session", "unknown_operation", "unknown_permission_request"}:
        status = 404
    elif exc.code in {"prompt_in_flight", "runtime_busy"}:
        status = 409
    elif exc.code in {"manager_closed", "resume_failed"}:
        status = 503
    else:
        status = 400
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@router.get("/options")
def acp_options() -> dict[str, Any]:
    settings = get_settings()
    if not settings.dsh_runtime_enabled or not settings.dsh_acp_enabled:
        return {"enabled": False, "disabled_reason": "ACP runtime is disabled", "capabilities": {}}
    try:
        manager = get_acp_runtime_manager()
    except HTTPException as exc:
        return {"enabled": False, "disabled_reason": exc.detail, "capabilities": {}}
    return {
        "enabled": True,
        "disabled_reason": None,
        "profile": "acp",
        "capabilities": manager.capabilities(),
        "permission_policy": {
            "allow": [ACP_ALLOW_ONCE_OPTION_ID],
            "reject": [ACP_REJECT_ONCE_OPTION_ID],
            "allow_always": False,
        },
    }


@router.get("/sessions")
@router.get("/session/list")
def list_sessions() -> dict[str, Any]:
    """List the sessions this backend currently owns, not DSH's persisted list."""
    manager = get_acp_runtime_manager()
    return {"sessions": manager.list_sessions()}


@router.post("/session/new")
@router.post("/session")
def create_session(payload: dict[str, Any]) -> dict[str, Any]:
    manager = get_acp_runtime_manager()
    workspace = str(payload.get("workspace") or payload.get("cwd") or "").strip()
    if not workspace:
        raise HTTPException(status_code=400, detail="workspace is required")
    try:
        session = manager.create_session(workspace)
    except AcpManagerError as exc:
        raise _manager_error(exc) from exc
    return {
        "session_id": session.session_id,
        "workspace": str(session.workspace),
        "state": session.state,
        "capabilities": manager.capabilities(),
    }


@router.post("/session/resume")
def resume_session(payload: dict[str, Any]) -> dict[str, Any]:
    manager = get_acp_runtime_manager()
    session_id = str(payload.get("session_id") or "").strip()
    workspace = str(payload.get("workspace") or payload.get("cwd") or "").strip()
    if not session_id or not workspace:
        raise HTTPException(status_code=400, detail="session_id and workspace are required")
    try:
        session = manager.create_session(workspace, session_id=session_id)
    except AcpManagerError as exc:
        raise _manager_error(exc) from exc
    return {"session_id": session.session_id, "workspace": str(session.workspace), "state": session.state}


@router.post("/prompt")
@router.post("/session/{session_id}/prompt")
async def prompt(
    request: Request,
    payload: dict[str, Any],
    session_id: str | None = None,
) -> StreamingResponse:
    manager = get_acp_runtime_manager()
    sid = str(session_id or payload.get("session_id") or "").strip()
    message = payload.get("message", payload.get("prompt"))
    criteria = payload.get("acceptance_criteria")
    if not sid or not isinstance(message, str) or not message.strip():
        raise HTTPException(status_code=400, detail="session_id and prompt are required")
    if criteria is not None and not isinstance(criteria, str):
        raise HTTPException(status_code=400, detail="acceptance_criteria must be a string")
    try:
        operation = manager.prompt(sid, message, acceptance_criteria=criteria or "")
    except AcpManagerError as exc:
        raise _manager_error(exc) from exc
    return StreamingResponse(
        _operation_stream(request, manager, operation),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _operation_stream(
    request: Request,
    manager: AcpRuntimeManager,
    operation: AcpOperation,
) -> AsyncIterator[str]:
    """Project one operation's queue into SSE until it settles.

    Every exit path must release the operation. A client disconnect can close
    this generator at the current ``yield`` instead of at the ``is_disconnected``
    check, so the cancellation lives in ``finally``; otherwise the ACP prompt
    would keep running and hold the session busy.
    """
    try:
        while True:
            if await request.is_disconnected():
                yield _sse(
                    "error",
                    {
                        "code": "client_disconnected",
                        "detail": "ACP client disconnected",
                        "operation_id": operation.operation_id,
                        "session_id": operation.session_id,
                    },
                )
                break
            try:
                item = await asyncio.to_thread(operation.queue.get, True, 0.1)
            except queue.Empty:
                if operation.done.is_set():
                    break
                continue
            yield _sse(item["event"], item["data"])
            if item["event"] in {"error", "completed"}:
                if operation.done.is_set() and operation.queue.empty():
                    break
    finally:
        if not operation.done.is_set():
            try:
                manager.cancel(operation.operation_id)
            except AcpManagerError:
                pass


@router.get("/operation/{operation_id}")
def operation_status(operation_id: str) -> dict[str, Any]:
    manager = get_acp_runtime_manager()
    try:
        operation = manager.get_operation(operation_id)
    except AcpManagerError as exc:
        raise _manager_error(exc) from exc
    return {
        "operation_id": operation.operation_id,
        "session_id": operation.session_id,
        "state": operation.state,
        "done": operation.done.is_set(),
        "error": operation.error,
        "run_id": operation.run_id,
    }


@router.post("/cancel")
@router.post("/operation/{operation_id}/cancel")
def cancel(payload: dict[str, Any] | None = None, operation_id: str | None = None) -> dict[str, Any]:
    manager = get_acp_runtime_manager()
    oid = str(operation_id or (payload or {}).get("operation_id") or "").strip()
    if not oid:
        raise HTTPException(status_code=400, detail="operation_id is required")
    try:
        manager.cancel(oid)
    except AcpManagerError as exc:
        raise _manager_error(exc) from exc
    return {"ok": True, "operation_id": oid, "state": "cancelling"}


@router.post("/permission/respond")
@router.post("/operation/{operation_id}/permission")
def permission(
    payload: dict[str, Any] | None = None,
    operation_id: str | None = None,
) -> dict[str, Any]:
    manager = get_acp_runtime_manager()
    body = payload or {}
    oid = str(operation_id or body.get("operation_id") or "").strip()
    request_id = body.get("request_id")
    option_id = str(body.get("option_id") or body.get("optionId") or "").strip()
    if not oid or request_id is None or not option_id:
        raise HTTPException(
            status_code=400,
            detail="operation_id, request_id, and option_id are required",
        )
    try:
        manager.respond_permission(oid, request_id, option_id)
    except AcpManagerError as exc:
        raise _manager_error(exc) from exc
    return {"ok": True, "operation_id": oid, "request_id": request_id, "option_id": option_id}


@router.delete("/session/{session_id}")
@router.post("/session/{session_id}/close")
def close_session(session_id: str) -> dict[str, Any]:
    manager = get_acp_runtime_manager()
    try:
        manager.close_session(session_id)
    except AcpManagerError as exc:
        raise _manager_error(exc) from exc
    return {"ok": True, "session_id": session_id, "state": "closed"}


def _sse(event: str, payload: Any) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(payload, ensure_ascii=False, default=str))


def shutdown_acp_runtime() -> None:
    global _manager
    with _manager_lock:
        manager, _manager = _manager, None
    if manager is not None:
        manager.shutdown()
