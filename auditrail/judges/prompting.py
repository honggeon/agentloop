"""Deterministic judge input and prompt construction.

The DSH judge profile has read-only access to the run directory, so the engine
passes bounded evidence in the prompt instead of granting the judge tools. The
same builder serves any future engine that needs embedded evidence.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from ..runtime import build_agent_judge_prompt

MAX_EVIDENCE_BYTES = 256 * 1024
MAX_TRAJECTORY_ROWS = 400
MAX_TRAJECTORY_ROW_BYTES = 4096
MAX_AUDIT_FINDINGS = 50
MAX_AUDIT_TOOL_CALLS = 200
_MAX_STRING = 2000


class JudgeEvidenceError(ValueError):
    """A run directory cannot supply bounded judge evidence."""


def build_dimension_repair_note(missing: List[str]) -> str:
    """Repair instruction appended to the judge prompt after an incomplete proposal.

    Names the omitted dimension keys and restates the contract: every one of
    the 13 dimensions must be present; a dimension without evidence stays
    ``status=unknown``/``score=null``/``source=judge`` instead of being
    dropped. The judge must return the full report again, not a patch.
    """
    from ..agents.schemas import DIMENSION_DEFINITIONS

    required = ", ".join(DIMENSION_DEFINITIONS)
    return (
        "\n\n上一次输出的裁判报告结构不完整：缺少维度 %s。\n"
        "必须重新返回完整报告 JSON：dimensions 必须包含全部 13 个维度 key（%s），不得省略任何维度。\n"
        "缺少证据的维度也必须保留条目，并填 status=unknown、score=null、source=judge，"
        "在 summary 中说明证据不足；不得用 0 分或 fail 冒充已评估。\n"
        "返回完整报告，不要只返回缺失维度的补丁。\n"
    ) % (", ".join(missing), required)


def build_judge_evidence(run_dir: str | Path) -> Dict[str, Any]:
    """Read bounded, deterministic evidence from one run directory."""
    run = Path(run_dir)
    if not run.is_dir():
        raise JudgeEvidenceError("run directory does not exist")

    summary = _read_json(run / "summary.json")
    audit = _read_json(run / "audit.json")
    source = _read_json(run / "source.json")
    expectations = _read_json(run / "expectations.json")
    trajectory, trajectory_truncated = _read_trajectory(run / "trajectory.jsonl")

    return {
        "run_id": run.name,
        "acceptance_criteria": _trim(expectations.get("acceptance_criteria")),
        "summary": _project_summary(summary),
        "source": _project_source(source),
        "audit": {
            "tool_calls": [
                _project_tool_call(call) for call in _bounded_list(audit.get("tool_calls"), MAX_AUDIT_TOOL_CALLS)
            ],
            "findings": [_project_audit_finding(item) for item in _bounded_list(audit.get("findings"), MAX_AUDIT_FINDINGS)],
            "phases": _project_phases(audit.get("phases")),
        },
        "trajectory": {
            "rows": trajectory,
            "truncated": trajectory_truncated or len(trajectory) >= MAX_TRAJECTORY_ROWS,
        },
    }


def build_dsh_judge_prompt(run_dir: str | Path) -> str:
    """Assemble the DSH judge instruction with bounded embedded evidence."""
    evidence = build_judge_evidence(run_dir)
    payload = json.dumps(evidence, ensure_ascii=False, default=str)
    if len(payload.encode("utf-8")) > MAX_EVIDENCE_BYTES:
        payload = json.dumps(
            {
                "run_id": evidence["run_id"],
                "acceptance_criteria": evidence["acceptance_criteria"],
                "summary": evidence["summary"],
                "source": evidence["source"],
                "audit": evidence["audit"],
                "trajectory": {"rows": evidence["trajectory"]["rows"][:50], "truncated": True},
                "evidence_truncated": True,
            },
            ensure_ascii=False,
            default=str,
        )
    base = build_agent_judge_prompt(run_dir)
    return "\n".join(
        [
            base,
            "",
            "You are the DSH semantic judge. You have no shell, file, or report-writing tools;",
            "the run artifacts below are the only evidence you may use.",
            "Judge only what this evidence supports. When evidence is missing, mark the dimension",
            'status "unknown" instead of guessing or reporting a failure.',
            "Deterministic audit findings and deliverable checks are authoritative and may not be",
            "overridden by semantic reasoning.",
            "",
            "Run evidence (JSON):",
            payload,
            "",
            "Return exactly one raw JSON object with overall_status, completion_score, confidence,",
            "summary, findings, metrics, charts, dimensions, and judge_reliability.",
            "Do not wrap the JSON in a Markdown code fence and do not add any other text.",
        ]
    )


# Run artifacts are untrusted input for prompt assembly: they can contain
# secrets, personal data, or very large payloads. Only allowlisted fields with
# bounded string sizes are forwarded to the model.

_SUMMARY_FIELDS = (
    "session_id",
    "title",
    "workspace",
    "model",
    "server_host",
    "source_user_id",
    "source_type",
    "message_count",
    "tool_call_count",
    "tool_error_count",
    "created_at",
)
_SOURCE_FIELDS = (
    "source_type",
    "source_base_url",
    "server_host",
    "interaction_mode",
    "dsh_session_id",
    "dsh_profile",
    "dsh_version",
    "session_role",
)


def _project_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    projected = {key: _scalar(summary.get(key)) for key in _SUMMARY_FIELDS if key in summary}
    if not projected:
        projected = {"note": "summary.json was missing or empty"}
    return projected


def _project_source(source: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _scalar(source.get(key)) for key in _SOURCE_FIELDS if key in source}


def _project_audit_finding(item: Any) -> Dict[str, Any]:
    if not isinstance(item, dict):
        return {"value": _trim(item)}
    return {
        key: _scalar(item.get(key))
        for key in ("code", "severity", "message", "message_index", "tool")
        if key in item
    }


def _project_tool_call(call: Any) -> Dict[str, Any]:
    if not isinstance(call, dict):
        return {"value": _trim(call)}
    return {
        key: _scalar(call.get(key))
        for key in ("index", "tool", "status", "args_preview", "result_preview", "message")
        if key in call
    }


def _project_phases(phases: Any) -> List[Any]:
    rows = phases if isinstance(phases, list) else []
    return [_scalar(row) if not isinstance(row, dict) else {key: _scalar(row.get(key)) for key in row} for row in rows[:50]]


def _read_trajectory(path: Path) -> tuple[List[Dict[str, Any]], bool]:
    if not path.exists():
        return [], False
    rows: List[Dict[str, Any]] = []
    truncated = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        if len(rows) >= MAX_TRAJECTORY_ROWS:
            truncated = True
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        rows.append(_project_trajectory_row(payload))
    return rows, truncated


def _project_trajectory_row(row: Dict[str, Any]) -> Dict[str, Any]:
    projected = {
        key: _scalar(row.get(key))
        for key in ("index", "role", "timestamp", "tool_name", "tool_call_id", "status", "is_error")
        if key in row
    }
    if "content" in row:
        projected["content"] = _trim(row.get("content"), MAX_TRAJECTORY_ROW_BYTES)
    tool_calls = row.get("tool_calls")
    if isinstance(tool_calls, list):
        projected["tool_calls"] = [
            {
                key: _scalar(call.get(key))
                for key in ("id", "name", "arguments")
                if isinstance(call, dict) and key in call
            }
            for call in tool_calls[:20]
        ]
    if row.get("reasoning"):
        projected["reasoning"] = _trim(row.get("reasoning"))
    return projected


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _bounded_list(value: Any, limit: int) -> List[Any]:
    return list(value[:limit]) if isinstance(value, list) else []


def _scalar(value: Any) -> Any:
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _trim(value)
    return _trim(json.dumps(value, ensure_ascii=False, default=str))


def _trim(value: Any, limit: int = _MAX_STRING) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
