"""Phase 4 append-only audit log.

Every privileged local action (agent config writes, price version lifecycle,
import create/cancel/retry/resume, promptfoo draft review, index rebuild,
watcher enable/disable) appends one JSON object per line to
``platform/audit/audit_events.jsonl``.

Honesty rules:

- the log is **append-only**: there is no update/delete API and no code path
  rewrites existing lines;
- events never contain secrets, tokens, full prompts or raw exception text —
  ``reason`` is bounded and pre-redacted by callers;
- a failed write raises :class:`AuditLogError`; callers surface it as an
  explicit API error instead of silently pretending the action was audited;
- reads are paginated and filterable (resource_type / resource_id / action).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from auditrail.run_contract import utc_now

from backend.app.config import Settings

AUDIT_DIR = "audit"
AUDIT_FILE = "audit_events.jsonl"
SCHEMA_VERSION = 1

MAX_REASON_LENGTH = 300
MAX_QUERY_LIMIT = 200
DEFAULT_QUERY_LIMIT = 50

#: Actions this deployment records. Callers must use these verbs; unknown
#: actions are rejected so the log stays queryable and honest.
AUDIT_ACTIONS = frozenset(
    {
        "agent_create",
        "agent_update",
        "agent_delete",
        "price_version_create",
        "price_version_activate",
        "price_version_delete",
        "import_create",
        "import_cancel",
        "import_retry",
        "import_resume",
        "promptfoo_draft_approve",
        "promptfoo_draft_reject",
        "promptfoo_draft_archive",
        "index_rebuild",
        "index_pause",
        "index_resume",
        "watcher_enable",
        "watcher_disable",
    }
)

_RESULTS = ("success", "denied", "failed")

#: Fragments that must never appear in a stored audit field.
_SECRET_FRAGMENTS = ("authorization", "bearer ", "sk-", "cookie", "token=", "api_key", "password")


class AuditLogError(RuntimeError):
    """The audit write failed — the action must not report silent success."""


def _audit_path(settings: Settings) -> Path:
    return Path(settings.platform_dir) / AUDIT_DIR / AUDIT_FILE


def _clean(value: Any, *, limit: int) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())[:limit]
    lowered = text.lower()
    if any(fragment in lowered for fragment in _SECRET_FRAGMENTS):
        return "[redacted]"
    return text or None


def append_event(
    settings: Settings,
    *,
    actor: str,
    role: str,
    action: str,
    resource_type: str,
    resource_id: Optional[str] = None,
    result: str = "success",
    reason: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Append one audit event; raises ``AuditLogError`` on write failure."""
    action = str(action or "").strip()
    if action not in AUDIT_ACTIONS:
        raise ValueError("unknown audit action %r" % action)
    result = str(result or "success")
    if result not in _RESULTS:
        raise ValueError("audit result must be one of %s" % (list(_RESULTS),))
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "audit-%s" % uuid.uuid4().hex[:12],
        "actor": _clean(actor, limit=64) or "unknown",
        "role": _clean(role, limit=32) or "unknown",
        "action": action,
        "resource_type": _clean(resource_type, limit=64) or "unknown",
        "resource_id": _clean(resource_id, limit=128),
        "result": result,
        "reason": _clean(reason, limit=MAX_REASON_LENGTH),
        "timestamp": utc_now(),
        "request_id": _clean(request_id, limit=64),
    }
    path = _audit_path(settings)
    line = json.dumps(event, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise AuditLogError("audit log write failed: %s" % type(exc).__name__) from exc
    return event


def query_events(
    settings: Settings,
    *,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    action: Optional[str] = None,
    limit: int = DEFAULT_QUERY_LIMIT,
    offset: int = 0,
) -> Dict[str, Any]:
    """Paginated, filtered read (newest first). Never raises on a corrupt
    line — it is skipped; the file itself missing means an empty log."""
    path = _audit_path(settings)
    events: List[Dict[str, Any]] = []
    if path.is_file():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            lines = []
        for line in lines:
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                events.append(item)
    if resource_type:
        events = [e for e in events if str(e.get("resource_type") or "") == resource_type]
    if resource_id:
        events = [e for e in events if str(e.get("resource_id") or "") == resource_id]
    if action:
        events = [e for e in events if str(e.get("action") or "") == action]
    events.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
    total = len(events)
    limit = min(max(int(limit), 1), MAX_QUERY_LIMIT)
    offset = max(int(offset), 0)
    page = events[offset : offset + limit]
    return {
        "audit_events": page,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
        "authority": "local",
        "note": "append-only 本地审计日志：不支持修改或删除",
    }
