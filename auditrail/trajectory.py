from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def messages_from_session_response(session_response: Dict) -> List[Dict]:
    session = session_response.get("session") or session_response
    messages = session.get("messages") or session_response.get("messages") or []
    return [m for m in messages if isinstance(m, dict)]


def llm_calls_from_session_response(session_response: Dict) -> List[Dict]:
    session = session_response.get("session") or session_response
    calls: List[Dict] = []
    for source in (session, session_response):
        for key in ("llm_calls", "llmCalls"):
            value = source.get(key) if isinstance(source, dict) else None
            if isinstance(value, list):
                calls.extend(item for item in value if isinstance(item, dict))
    return calls


def normalize_message(message: Dict, index: int) -> Dict:
    role = message.get("role") or "unknown"
    item = {
        "index": index,
        "message_id": message.get("id"),
        "message_uuid": message.get("message_uuid"),
        "session_id": message.get("session_id"),
        "role": role,
        "timestamp": message.get("timestamp"),
        "content": message.get("content") or "",
    }
    for key in (
        "tool_call_id",
        "tool_name",
        "tool_calls",
        "finish_reason",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
        "codex_reasoning_items",
        "codex_message_items",
    ):
        value = message.get(key)
        if value not in (None, "", [], {}):
            item[key] = value
    if role == "assistant":
        item["kind"] = "assistant_tool_request" if item.get("tool_calls") else "assistant_message"
    elif role == "tool":
        item["kind"] = "tool_result"
    elif role == "user":
        item["kind"] = "user_message"
    else:
        item["kind"] = "message"
    return item


def normalize_messages(messages: Iterable[Dict]) -> List[Dict]:
    return [normalize_message(message, index) for index, message in enumerate(messages)]


def write_json(path: Path, payload: Dict) -> None:
    write_text(path, json.dumps(payload, ensure_ascii=False, indent=2))


def write_jsonl(path: Path, rows: Iterable[Dict]) -> None:
    lines = [json.dumps(row, ensure_ascii=False, default=str) for row in rows]
    write_text(path, "".join(line + "\n" for line in lines))


def write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically: same-dir temp file + fsync + os.replace.

    This is the single write entry for run artifacts (JSON, JSONL and
    Markdown): a reader only ever observes the previous complete file or the
    new complete file, never a truncated one, and an interrupted process
    leaves at most a ``.<name>.*.tmp`` staging file that readers and the
    artifact manifest ignore.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / (".%s.%d.%d.tmp" % (path.name, os.getpid(), int(time.time() * 1e6)))
    try:
        with open(staging, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(staging), str(path))
    finally:
        try:
            staging.unlink()
        except FileNotFoundError:
            pass


def session_summary(session_response: Dict) -> Dict:
    session = session_response.get("session") or session_response
    messages = messages_from_session_response(session_response)
    tool_names = []
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            name = _tool_call_name(call)
            if name:
                tool_names.append(name)
    return {
        "session_id": session.get("session_id"),
        "title": session.get("title"),
        "model": session.get("model"),
        "workspace": session.get("workspace"),
        "message_count": len(messages),
        "assistant_count": sum(1 for m in messages if m.get("role") == "assistant"),
        "tool_result_count": sum(1 for m in messages if m.get("role") == "tool"),
        "tool_call_count": len(tool_names),
        "tool_names": sorted(set(tool_names)),
    }


def _tool_call_name(call: Dict) -> Optional[str]:
    if not isinstance(call, dict):
        return None
    function = call.get("function") or {}
    if isinstance(function, dict):
        return function.get("name") or call.get("name")
    return call.get("name")
