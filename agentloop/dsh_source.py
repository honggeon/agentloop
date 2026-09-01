"""DeepSeek Harness (DSH) session source.

DSH records every agent session on local disk as
``$DSH_HOME/sessions/<workspace-dir>/<session-id>/session.jsonl.zstd`` — a
zstd-compressed JSONL event log with ``user/message``, ``assistant/message``,
``tool/call`` and ``tool/result`` records. This module scans, decompresses and
converts those recordings into agentloop's run contract
(``session.json`` / ``trajectory.jsonl`` / ``summary.json``), so the
deterministic audit and judge pipeline can evaluate DSH agents unchanged.

The module stays black-box: it only reads files, never talks to a running DSH.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import zstandard

SESSION_FILENAME = "session.jsonl.zstd"

# Event types that carry the conversation itself; chunk streams duplicate the
# consolidated messages and are dropped from the stored raw events.
_CHUNK_SUFFIXES = ("-chunks", "/chunk")

_SESSION_ID_RE = re.compile(r"^session-[0-9a-fA-F-]{36}$")
_WORKSPACE_DIR_RE = re.compile(r"^[^./\\][^/\\]*$")


def default_dsh_home() -> Path:
    """DSH home directory, overridable with the ``DSH_HOME`` env var."""
    return Path(os.getenv("DSH_HOME", "~/.dsh")).expanduser()


def read_events(path: Path) -> List[Dict]:
    """Decompress and parse a ``session.jsonl.zstd`` file into event dicts."""
    decompressor = zstandard.ZstdDecompressor()
    with open(path, "rb") as handle:
        data = decompressor.stream_reader(handle).read().decode("utf-8")
    events: List[Dict] = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


def iter_session_files(dsh_home: Path) -> Iterable[Path]:
    """Yield every session recording under ``<dsh_home>/sessions``."""
    sessions_root = Path(dsh_home) / "sessions"
    if not sessions_root.is_dir():
        return
    for workspace_dir in sorted(sessions_root.iterdir()):
        if not workspace_dir.is_dir():
            continue
        for session_dir in sorted(workspace_dir.iterdir()):
            candidate = session_dir / SESSION_FILENAME
            if candidate.is_file():
                yield candidate


def validate_session_id(session_id: str) -> str:
    """Reject anything that is not a plain ``session-<uuid>`` directory name."""
    value = str(session_id or "").strip()
    if not _SESSION_ID_RE.match(value):
        raise ValueError("invalid session_id: %r" % session_id)
    return value


def validate_workspace_dir(workspace_dir: str) -> str:
    """Reject workspace directory names with separators or dot segments."""
    value = str(workspace_dir or "").strip()
    if not _WORKSPACE_DIR_RE.match(value) or value in {".", ".."} or ".." in value:
        raise ValueError("invalid workspace dir: %r" % workspace_dir)
    return value


def find_session_file(dsh_home: Path, session_id: str, workspace_dir: Optional[str] = None) -> Path:
    """Locate one session recording by id, optionally pinned to a workspace dir.

    Raises ``ValueError`` for unsafe input and ``FileNotFoundError`` when no
    (or, without a workspace hint, several) recordings match.
    """
    session_id = validate_session_id(session_id)
    sessions_root = Path(dsh_home) / "sessions"
    if workspace_dir:
        workspace_dir = validate_workspace_dir(workspace_dir)
        candidate = sessions_root / workspace_dir / session_id / SESSION_FILENAME
        if not candidate.is_file():
            raise FileNotFoundError("no DSH session recording at %s" % candidate)
        return candidate
    matches = [
        path
        for path in iter_session_files(dsh_home)
        if path.parent.name == session_id
    ]
    if not matches:
        raise FileNotFoundError("no DSH session recording for %s" % session_id)
    if len(matches) > 1:
        raise FileNotFoundError(
            "multiple DSH session recordings for %s; pass workspace dir" % session_id
        )
    return matches[0]


def list_sessions(dsh_home: Path, limit: Optional[int] = None) -> List[Dict]:
    """List recorded DSH sessions, newest first, with lightweight stats."""
    items: List[Dict] = []
    for path in iter_session_files(Path(dsh_home)):
        try:
            events = read_events(path)
        except Exception:
            continue
        info = session_info(events)
        info["path"] = str(path)
        info["workspace_dir"] = path.parent.parent.name
        info["updated_at"] = int(path.stat().st_mtime * 1000)
        info["size_bytes"] = path.stat().st_size
        items.append(info)
    items.sort(key=lambda item: item.get("updated_at") or 0, reverse=True)
    if limit is not None:
        items = items[: max(0, int(limit))]
    return items


def session_info(events: List[Dict]) -> Dict:
    """Lightweight identity and counts for one parsed event log."""
    info: Dict = {
        "session_id": "",
        "title": "",
        "workspace": "",
        "agent_preset": "",
        "created_at": None,
        "user_message_count": 0,
        "assistant_message_count": 0,
        "tool_call_count": 0,
        "tool_result_count": 0,
    }
    for event in events:
        event_type = event.get("type")
        data = event.get("data") or {}
        if event_type == "session":
            info["session_id"] = event.get("id") or ""
            info["workspace"] = event.get("cwd") or ""
            info["agent_preset"] = event.get("agentPreset") or ""
            info["created_at"] = event.get("createdAt")
        elif event_type == "session/title":
            title = data.get("title")
            if title:
                info["title"] = title
        elif event_type == "user/message":
            info["user_message_count"] += 1
        elif event_type == "assistant/message":
            info["assistant_message_count"] += 1
        elif event_type == "tool/call":
            info["tool_call_count"] += 1
        elif event_type == "tool/result":
            info["tool_result_count"] += 1
    return info


def events_to_messages(events: List[Dict]) -> List[Dict]:
    """Map DSH events to xuanjian-shape raw messages.

    The output feeds ``trajectory.normalize_messages`` and
    ``audit.audit_messages`` unchanged: assistant messages carry
    ``tool_calls`` entries with ``id`` / ``name`` / ``arguments``, and tool
    results are ``role="tool"`` messages with ``tool_call_id``/``tool_name``.
    """
    tool_names: Dict[str, str] = {}
    for event in events:
        if event.get("type") == "tool/call":
            data = event.get("data") or {}
            if data.get("callId"):
                tool_names[str(data["callId"])] = str(data.get("name") or "")

    messages: List[Dict] = []
    for event in events:
        event_type = event.get("type")
        data = event.get("data") or {}
        timestamp = event.get("time")
        if event_type == "user/message":
            messages.append(
                {
                    "id": data.get("id"),
                    "role": "user",
                    "timestamp": timestamp,
                    "content": _text_of_blocks(data.get("content")),
                }
            )
        elif event_type == "assistant/message":
            message = data.get("message") or {}
            blocks = message.get("content") or []
            tool_calls = [
                {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": block.get("arguments"),
                }
                for block in blocks
                if isinstance(block, dict) and block.get("type") == "tool-call"
            ]
            item: Dict = {
                "id": message.get("id"),
                "role": "assistant",
                "timestamp": timestamp,
                "content": _text_of_blocks(blocks, block_type="text"),
            }
            reasoning = _text_of_blocks(blocks, block_type="reasoning")
            if reasoning:
                item["reasoning"] = reasoning
            if tool_calls:
                item["tool_calls"] = tool_calls
            messages.append(item)
        elif event_type == "tool/result":
            message = data.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool-result":
                    continue
                call_id = str(block.get("toolCallId") or "")
                item = {
                    "id": message.get("id"),
                    "role": "tool",
                    "timestamp": timestamp,
                    "tool_call_id": call_id,
                    "tool_name": tool_names.get(call_id) or None,
                    "content": _text_of_blocks(block.get("content")),
                }
                if block.get("isError"):
                    item["is_error"] = True
                messages.append(item)
    return messages


def build_session_response(events: List[Dict], path: Optional[Path] = None) -> Dict:
    """Build the ``session.json`` payload for one parsed DSH event log.

    ``session.messages`` follows the shape ``trajectory`` already consumes;
    non-chunk raw events are preserved under ``dsh_events`` as evidence.
    """
    info = session_info(events)
    session = {
        "session_id": info["session_id"],
        "title": info["title"],
        "workspace": info["workspace"],
        "model": _model_of(events),
        "messages": events_to_messages(events),
    }
    response: Dict = {"session": session}
    response["dsh"] = {
        "agent_preset": info["agent_preset"],
        "created_at": info["created_at"],
        "source_path": str(path) if path else "",
    }
    response["dsh_events"] = [
        event
        for event in events
        if not str(event.get("type") or "").endswith(_CHUNK_SUFFIXES)
    ]
    return response


def source_meta(session_response: Dict) -> Dict:
    """Source metadata written to ``source.json`` / merged into the summary."""
    dsh = session_response.get("dsh") or {}
    session = session_response.get("session") or {}
    return {
        "source_base_url": "dsh://local",
        "server_host": "deepseek-harness",
        "server_netloc": "local",
        "source_user_id": "",
        "source_type": "dsh",
        "dsh_session_id": session.get("session_id") or "",
        "dsh_workspace": session.get("workspace") or "",
        "dsh_agent_preset": dsh.get("agent_preset") or "",
    }


def load_session_response(path: Path) -> Dict:
    """Read one recording and build its ``session.json`` payload."""
    return build_session_response(read_events(Path(path)), Path(path))


def _text_of_blocks(blocks, block_type: str = "text") -> str:
    parts: List[str] = []
    for block in blocks or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") != block_type:
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            parts.append(text)
    return "\n".join(parts)


def _model_of(events: List[Dict]) -> Optional[str]:
    """Return the model recorded by ``request/header`` events.

    DSH stores the model config at ``data.header.config.model``; older
    recordings carried it directly on ``data``. A session resumed with a
    different model logs another event, so the last match wins.
    """
    model: Optional[str] = None
    for event in events:
        if event.get("type") != "request/header":
            continue
        data = event.get("data") or {}
        sources: List[Dict] = []
        header = data.get("header")
        if isinstance(header, dict) and isinstance(header.get("config"), dict):
            sources.append(header["config"])
        sources.append(data)
        for source in sources:
            value = next(
                (
                    source[key]
                    for key in ("model", "modelId", "model_id")
                    if isinstance(source.get(key), str) and source.get(key)
                ),
                None,
            )
            if value:
                model = value
                break
    return model
