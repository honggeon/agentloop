from __future__ import annotations

import json
import re
import base64
from typing import Any, Generator, Iterable

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from agentloop.cli import _run_dir, _source_meta, _write_run_outputs
from agentloop.client import AgentClient
from agentloop.runtime import build_runtime_graph, build_runtime_trace, save_expectations
from agentloop.trajectory import messages_from_session_response
from backend.app.config import get_settings
from backend.app.routes.runs import _enrich_run_item, _run_item, get_batch_summary


router = APIRouter(prefix="/api")


@router.post("/live/evaluate")
def live_evaluate(payload: dict[str, Any]) -> StreamingResponse:
    return StreamingResponse(_event_stream(payload), media_type="text/event-stream")


@router.post("/live/evaluate-with-files")
def live_evaluate_with_files(payload: dict[str, Any]) -> StreamingResponse:
    payload = dict(payload)
    uploads = []
    for item in payload.get("files") or []:
        if not isinstance(item, dict):
            continue
        filename = str(item.get("filename") or "attachment")
        encoded = str(item.get("content_base64") or "")
        if not encoded:
            continue
        uploads.append({"filename": filename, "content": base64.b64decode(encoded)})
    payload["uploads"] = uploads
    return StreamingResponse(_event_stream(payload), media_type="text/event-stream")


@router.post("/live/clarify/respond")
def live_clarify_respond(payload: dict[str, Any]) -> dict[str, Any]:
    client = _client_from_payload(payload)
    session_id = str(payload.get("session_id") or "").strip()
    response = str(payload.get("response") or "").strip()
    if not session_id or not response:
        return {"ok": False, "error": "session_id and response are required"}
    result = client.respond_clarify(session_id, response)
    return {"ok": True, "result": result}


@router.post("/live/approval/respond")
def live_approval_respond(payload: dict[str, Any]) -> dict[str, Any]:
    client = _client_from_payload(payload)
    session_id = str(payload.get("session_id") or "").strip()
    choice = str(payload.get("choice") or "").strip() or ("once" if payload.get("approved") else "deny")
    approval_id = str(payload.get("approval_id") or "").strip() or None
    if not session_id:
        return {"ok": False, "error": "session_id is required"}
    result = client.respond_approval(session_id, choice, approval_id=approval_id)
    return {"ok": True, "result": result}


@router.post("/live/message/respond")
def live_message_respond(payload: dict[str, Any]) -> StreamingResponse:
    return StreamingResponse(_message_response_stream(payload), media_type="text/event-stream")


def _event_stream(payload: dict[str, Any]) -> Iterable[str]:
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    user_id = str(payload.get("user_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    acceptance_criteria = str(payload.get("acceptance_criteria") or "").strip()
    token = str(payload.get("token") or "").strip() or None
    model = str(payload.get("model") or "").strip()
    title = str(payload.get("title") or "agentloop runtime evaluation").strip() or "agentloop runtime evaluation"
    skill = str(payload.get("skill") or "").strip()
    uploads = [item for item in payload.get("uploads") or [] if isinstance(item, dict)]
    timeout = _float_or_default(payload.get("timeout"), 60.0)
    max_turns = _int_or_default(payload.get("max_turns"), 20)
    auto_continue = _bool_or_default(payload.get("auto_continue"), False)
    auto_continue_limit = _int_or_default(payload.get("auto_continue_limit"), 8)

    if not base_url or not user_id or not message:
        yield _sse("error", {"detail": "base_url, user_id, and message are required"})
        return

    client = AgentClient(base_url, user_id, token=token, timeout=timeout)
    session_id = ""
    stage = "create_session"
    try:
        yield _sse("remote_step", {"stage": stage, "status": "started"})
        created = client.create_session(title=title, model=model)
        session = created.get("session") or {}
        session_id = str(session.get("session_id") or "")
        if not session_id:
            raise RuntimeError("session creation response did not include session.session_id")
        yield _sse("remote_step", {"stage": stage, "status": "ok", "session_id": session_id})

        attachments = []
        for upload in uploads:
            stage = "upload_file"
            filename = str(upload.get("filename") or "attachment")
            content = upload.get("content") or b""
            if not isinstance(content, bytes):
                continue
            yield _sse("remote_step", {"stage": stage, "status": "started", "filename": filename})
            attachments.append(client.upload_file(session_id, filename, content))
            yield _sse("remote_step", {"stage": stage, "status": "ok", "filename": filename})
        if attachments:
            yield _sse("files_uploaded", {"session_id": session_id, "files": attachments})

        stage = "start_chat"
        yield _sse("remote_step", {"stage": stage, "status": "started", "skill": skill or None})
        start_kwargs: dict[str, Any] = {"model": model or None, "max_turns": max_turns}
        if attachments:
            start_kwargs["attachments"] = attachments
        if skill:
            start_kwargs["skill"] = skill
        started = client.start_chat(session_id, message, **start_kwargs)
        stream_id = str(started.get("stream_id") or "")
        if not stream_id:
            raise RuntimeError("chat start response did not include stream_id")
        yield _sse("remote_step", {"stage": stage, "status": "ok", "stream_id": stream_id})

        yield _sse("run_started", {"session_id": session_id, "stream_id": stream_id, "run_id": session_id})
        yield _sse(
            "graph_update",
            {"run_id": session_id, "graph": build_runtime_graph([{"role": "user", "content": message}])},
        )
        yield _sse(
            "trace_update",
            {
                "run_id": session_id,
                "trace": build_runtime_trace(
                    [{"role": "user", "content": message}],
                    trace_id=session_id,
                    session_id=session_id,
                    agent_name=title,
                    agent_version=model,
                ),
            },
        )

        yield from _stream_and_persist(
            client,
            stream_id,
            session_id,
            base_url,
            user_id,
            acceptance_criteria,
            model=model,
            max_turns=max_turns,
            auto_continue=auto_continue,
            auto_continue_limit=auto_continue_limit,
        )
    except Exception as exc:
        detail = "%s: %s" % (type(exc).__name__, exc)
        payload = {"detail": detail, "stage": stage}
        if session_id:
            payload["session_id"] = session_id
        yield _sse("error", payload)


def _message_response_stream(payload: dict[str, Any]) -> Iterable[str]:
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    user_id = str(payload.get("user_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    acceptance_criteria = str(payload.get("acceptance_criteria") or "").strip()
    token = str(payload.get("token") or "").strip() or None
    model = str(payload.get("model") or "").strip()
    timeout = _float_or_default(payload.get("timeout"), 60.0)
    max_turns = _int_or_default(payload.get("max_turns"), 20)
    auto_continue = _bool_or_default(payload.get("auto_continue"), False)
    auto_continue_limit = _int_or_default(payload.get("auto_continue_limit"), 8)

    if not base_url or not user_id or not session_id or not message:
        yield _sse("error", {"detail": "base_url, user_id, session_id, and message are required"})
        return

    client = AgentClient(base_url, user_id, token=token, timeout=timeout)
    stage = "start_chat"
    try:
        yield _sse("remote_step", {"stage": stage, "status": "started", "session_id": session_id})
        started = client.start_chat(session_id, message, model=model or None, max_turns=max_turns)
        stream_id = str(started.get("stream_id") or "")
        if not stream_id:
            raise RuntimeError("chat start response did not include stream_id")
        yield _sse("remote_step", {"stage": stage, "status": "ok", "stream_id": stream_id})
        yield _sse("run_started", {"session_id": session_id, "stream_id": stream_id, "run_id": session_id})
        yield from _stream_and_persist(
            client,
            stream_id,
            session_id,
            base_url,
            user_id,
            acceptance_criteria,
            model=model,
            max_turns=max_turns,
            auto_continue=auto_continue,
            auto_continue_limit=auto_continue_limit,
        )
    except Exception as exc:
        yield _sse("error", {"detail": "%s: %s" % (type(exc).__name__, exc), "stage": stage, "session_id": session_id})


def _stream_and_persist(
    client: AgentClient,
    stream_id: str,
    session_id: str,
    base_url: str,
    user_id: str,
    acceptance_criteria: str,
    *,
    model: str = "",
    max_turns: int = 20,
    auto_continue: bool = False,
    auto_continue_limit: int = 8,
) -> Iterable[str]:
    stage = "stream_events"
    continue_count = 0
    last_interaction = ""
    current_stream_id = stream_id
    try:
        while True:
            stage = "stream_events"
            terminal = yield from _consume_stream(client, current_stream_id, session_id, auto_continue)

            stage = "fetch_session"
            yield _sse("remote_step", {"stage": stage, "status": "started", "session_id": session_id})
            session_response = client.fetch_session(session_id)
            messages = messages_from_session_response(session_response)
            graph = build_runtime_graph(messages)
            session = session_response.get("session") if isinstance(session_response, dict) else {}
            session = session if isinstance(session, dict) else {}
            trace = build_runtime_trace(
                messages,
                trace_id=session_id,
                session_id=session_id,
                agent_name=str(session.get("title") or "Agent Runtime"),
                agent_version=str(session.get("model") or ""),
            )

            settings = get_settings()
            run_dir = _run_dir(settings.runs_dir, session_id)
            _write_run_outputs(run_dir, session_response, source=_source_meta(base_url, user_id))
            save_expectations(run_dir, acceptance_criteria)
            run = _enrich_run_item(_run_item(run_dir))

            audit = json.loads((run_dir / "audit.json").read_text(encoding="utf-8"))
            interaction_message = _detect_interaction_block(messages)
            yield _sse("graph_update", {"run_id": run_dir.name, "graph": graph})
            yield _sse("trace_update", {"run_id": run_dir.name, "trace": trace})
            yield _sse("audit_update", {"run_id": run_dir.name, "audit": audit})

            should_continue, recommended, reason = _auto_continue_decision(
                interaction_message,
                last_interaction,
                auto_continue=auto_continue,
                continue_count=continue_count,
                auto_continue_limit=auto_continue_limit,
                stream_ok=terminal == "stream_end",
            )
            if not should_continue:
                if interaction_message:
                    yield _sse(
                        "needs_interaction",
                        {
                            "session_id": session_id,
                            "message": interaction_message,
                            "recommended_response": recommended,
                            "reason": reason,
                        },
                    )
                yield _sse("run_finished", {"run": run, "batch_summary": get_batch_summary()})
                return

            continue_count += 1
            last_interaction = interaction_message
            stage = "start_chat"
            yield _sse(
                "auto_interaction",
                {
                    "kind": "continue",
                    "session_id": session_id,
                    "response": recommended,
                    "reason": reason,
                    "count": continue_count,
                    "limit": auto_continue_limit,
                },
            )
            yield _sse("remote_step", {"stage": stage, "status": "started", "session_id": session_id, "auto_continue": True})
            started = client.start_chat(session_id, recommended, model=model or None, max_turns=max_turns)
            current_stream_id = str(started.get("stream_id") or "")
            if not current_stream_id:
                raise RuntimeError("chat start response did not include stream_id")
            yield _sse("remote_step", {"stage": stage, "status": "ok", "stream_id": current_stream_id})
    except Exception as exc:
        yield _sse(
            "error",
            {
                "detail": "%s: %s" % (type(exc).__name__, exc),
                "stage": stage,
                "session_id": session_id,
            },
        )


def _consume_stream(
    client: AgentClient,
    stream_id: str,
    session_id: str,
    auto_continue: bool,
) -> Generator[str, None, str]:
    terminal = "stream_end"
    for event_name, event_payload in client.stream_events(stream_id):
        yield _sse("agent_event", {"name": event_name, "payload": event_payload})
        if auto_continue:
            yield from _auto_respond_stream_event(client, session_id, event_name, event_payload)
        if event_name in {"stream_end", "error", "cancelled"}:
            terminal = event_name
            break
    return terminal


def _auto_respond_stream_event(
    client: AgentClient,
    session_id: str,
    event_name: str,
    event_payload: Any,
) -> Iterable[str]:
    kind = _stream_interaction_kind(event_name, event_payload)
    if kind == "clarify":
        response = _recommended_clarify_response(_event_text(event_payload))
        client.respond_clarify(session_id, response)
        yield _sse(
            "auto_interaction",
            {"kind": "clarify", "session_id": session_id, "response": response},
        )
        return
    if kind == "approval":
        approval_id = _approval_id_from_payload(event_payload)
        client.respond_approval(session_id, "session", approval_id=approval_id)
        yield _sse(
            "auto_interaction",
            {
                "kind": "approval",
                "session_id": session_id,
                "choice": "session",
                "approval_id": approval_id,
            },
        )


def _auto_continue_decision(
    interaction_message: str,
    last_interaction: str,
    *,
    auto_continue: bool,
    continue_count: int,
    auto_continue_limit: int,
    stream_ok: bool,
) -> tuple[bool, str, str]:
    recommended = _recommended_interaction_response(interaction_message) if interaction_message else ""
    reason = _interaction_reason(interaction_message) if interaction_message else ""
    if not interaction_message or not stream_ok or not auto_continue:
        return False, recommended, reason
    if continue_count >= auto_continue_limit:
        return False, recommended, reason or "auto_continue_limit_reached"
    if interaction_message == last_interaction:
        return False, recommended, reason or "auto_continue_no_progress"
    return True, recommended, reason


def _stream_interaction_kind(event_name: str, payload: Any) -> str:
    name = str(event_name or "").strip().lower()
    if name in {"clarify", "approval"}:
        return name
    data = payload if isinstance(payload, dict) else {}
    nested = data.get("payload") if isinstance(data.get("payload"), dict) else {}
    for source in (data, nested):
        value = str(source.get("type") or source.get("event") or source.get("kind") or "").strip().lower()
        if value in {"clarify", "approval"}:
            return value
    return ""


def _event_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
        for source in (payload, nested):
            for key in ("question", "content", "message", "description", "text"):
                value = source.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return json.dumps(payload, ensure_ascii=False)
    return str(payload or "")


def _approval_id_from_payload(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    for source in (payload, nested):
        for key in ("approval_id", "id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return None


def _detect_interaction_block(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if _looks_like_interaction_block(content):
            return content
        return ""
    return ""


def _looks_like_interaction_block(content: str) -> bool:
    return bool(_INTERACTION_RE.search(content))


def _recommended_interaction_response(content: str) -> str:
    if _CONTINUE_RE.search(content):
        return "继续，补齐缺失文件并生成最终 PPT。"
    return "确认"


def _recommended_clarify_response(content: str) -> str:
    if _CONTINUE_RE.search(content):
        return "继续，补齐缺失文件并生成最终 PPT。"
    return "继续使用当前推荐方案。"


def _interaction_reason(content: str) -> str:
    if _TURN_LIMIT_RE.search(content):
        return "agent_paused_after_turn_limit"
    if _CONTINUE_RE.search(content):
        return "waiting_for_generation_confirmation"
    return "waiting_for_user_confirmation"


def _sse(event: str, payload: Any) -> str:
    return "event: %s\ndata: %s\n\n" % (event, json.dumps(payload, ensure_ascii=False, default=str))


def _client_from_payload(payload: dict[str, Any]) -> AgentClient:
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    user_id = str(payload.get("user_id") or "").strip()
    token = str(payload.get("token") or "").strip() or None
    timeout = _float_or_default(payload.get("timeout"), 60.0)
    return AgentClient(base_url, user_id, token=token, timeout=timeout)


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bool_or_default(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


_INTERACTION_RE = re.compile(
    r"请确认|回复确认|确认后|等待确认|提出修改|confirm ui|please confirm|waiting for confirmation|"
    r"是否继续|继续执行|生成\s*ppt|尚未执行最终|请回复|接着做|"
    r"回复[“\"”']?\*{0,2}继续|调用次数限制|调用次数上限|工具调用次数",
    re.I,
)
_CONTINUE_RE = re.compile(
    r"生成\s*ppt|尚未执行最终|请回复|接着做|回复[“\"”']?\*{0,2}继续|"
    r"调用次数限制|调用次数上限|工具调用次数",
    re.I,
)
_TURN_LIMIT_RE = re.compile(r"调用次数限制|调用次数上限|工具调用次数", re.I)
