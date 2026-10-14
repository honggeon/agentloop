"""Strict parsing for a judge engine's JSON proposal.

The DeepAgent judge may answer through a tool call, message envelope, or
Markdown fence, so it keeps using ``schemas.resolve_judge_report``. Engine
adapters that must return strict JSON (the DSH judge) use this module instead:
a fenced or non-object body is an explicit failure, never a silently degraded
report.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

# A judge proposal is normally a few dozen kilobytes. Anything larger is not a
# report, and bounding it keeps model-bound evidence predictable.
MAX_PROPOSAL_BYTES = 512 * 1024

_JSON_FENCE_RE = re.compile(r"```", re.IGNORECASE)
_REPORT_SHAPE_KEYS = ("overall_status", "findings", "dimensions", "completion_score")


class JudgeProposalError(ValueError):
    """The engine output is not a strict, report-shaped JSON object."""


def parse_judge_proposal(raw: object, *, max_bytes: int = MAX_PROPOSAL_BYTES) -> Dict[str, Any]:
    """Parse one strict JSON judge proposal.

    Accepts a JSON object or a JSON string containing exactly one object. It
    rejects empty output, oversized output, Markdown fences, non-object JSON,
    and report-shaped objects that omit ``overall_status``.
    """
    text = _as_text(raw)
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        raise JudgeProposalError("judge proposal exceeded %s bytes" % max_bytes)
    if _JSON_FENCE_RE.search(text):
        raise JudgeProposalError("judge proposal must be raw JSON without a Markdown code fence")
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise JudgeProposalError("judge proposal was not valid JSON") from exc
    if not isinstance(payload, dict):
        raise JudgeProposalError("judge proposal must be a JSON object")
    if not str(payload.get("overall_status") or "").strip():
        raise JudgeProposalError("judge proposal omitted overall_status")
    if not any(key in payload for key in _REPORT_SHAPE_KEYS[1:]):
        raise JudgeProposalError("judge proposal was not report-shaped")
    return dict(payload)


def _as_text(raw: object) -> str:
    if raw is None:
        raise JudgeProposalError("judge engine returned no proposal")
    if isinstance(raw, str):
        text = raw.strip()
    elif isinstance(raw, dict):
        text = json.dumps(raw, ensure_ascii=False)
    else:
        raise JudgeProposalError("judge proposal must be a JSON object or JSON text")
    if not text:
        raise JudgeProposalError("judge engine returned no proposal")
    return text
