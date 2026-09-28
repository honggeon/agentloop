from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set, Tuple


DANGEROUS_TOOLS = {"terminal", "shell", "exec", "write_file", "patch", "delete_file"}


@dataclass
class Finding:
    severity: str
    code: str
    message: str

    def to_dict(self) -> Dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }


def audit_messages(messages: Iterable[Dict]) -> Dict:
    rows = [m for m in messages if isinstance(m, dict)]
    requested_ids: Set[str] = set()
    result_ids: Set[str] = set()
    tool_names: List[str] = []
    tool_details: List[Dict] = []
    findings: List[Finding] = []

    assistant_messages = [m for m in rows if m.get("role") == "assistant"]
    tool_messages = [m for m in rows if m.get("role") == "tool"]

    tool_results = _tool_results_by_id(rows)
    for index, msg in enumerate(rows):
        for call in msg.get("tool_calls") or []:
            call_id = _tool_call_id(call)
            name = _tool_call_name(call)
            args = _tool_call_args(call)
            if call_id:
                requested_ids.add(call_id)
            if name:
                tool_names.append(name)
                result = tool_results.get(call_id or "")
                status, result_preview = _tool_result_status(result)
                tool_details.append(
                    {
                        "index": index,
                        "tool_call_id": call_id,
                        "tool": name,
                        "status": status,
                        "args_preview": _preview(args),
                        "result_preview": result_preview,
                    }
                )
                if name in DANGEROUS_TOOLS:
                    findings.append(
                        Finding(
                            "warning",
                            "dangerous_tool_used",
                            "Potentially sensitive tool used: %s" % name,
                        )
                    )
        if msg.get("role") == "tool":
            call_id = msg.get("tool_call_id")
            if call_id:
                result_ids.add(call_id)
            content = str(msg.get("content") or "")
            if _is_tool_error(msg, content):
                findings.append(
                    Finding(
                        "error",
                        "tool_error",
                        "Tool result reports an error for %s: %s"
                        % (msg.get("tool_name") or call_id or "unknown tool", content[:240]),
                    )
                )

    missing_results = sorted(requested_ids - result_ids)
    orphan_results = sorted(result_ids - requested_ids)
    for call_id in missing_results:
        findings.append(Finding("error", "missing_tool_result", "Missing tool result for %s" % call_id))
    for call_id in orphan_results:
        findings.append(Finding("warning", "orphan_tool_result", "Tool result has no matching request: %s" % call_id))

    final_answer = _last_nonempty_assistant_content(assistant_messages)
    if not final_answer:
        findings.append(Finding("error", "missing_final_answer", "No non-empty assistant final answer found."))

    return {
        "message_count": len(rows),
        "assistant_count": len(assistant_messages),
        "tool_result_count": len(tool_messages),
        "tool_call_count": len(requested_ids),
        "tool_names": sorted(set(tool_names)),
        "missing_tool_results": missing_results,
        "orphan_tool_results": orphan_results,
        "final_answer_chars": len(final_answer or ""),
        "tool_calls": tool_details,
        "phases": infer_phases(rows, tool_details),
        "findings": [f.to_dict() for f in findings],
    }


def infer_phases(messages: Iterable[Dict], tool_details: Optional[List[Dict]] = None) -> List[Dict]:
    details = tool_details if tool_details is not None else _tool_details_from_messages(list(messages))
    phases: List[Tuple[str, Set[str], int]] = []
    current_name = ""
    current_tools: Set[str] = set()
    current_count = 0

    for detail in details:
        name = _phase_for_tool(detail.get("tool") or "")
        if not current_name:
            current_name = name
        if name != current_name:
            phases.append((current_name, set(current_tools), current_count))
            current_name = name
            current_tools = set()
            current_count = 0
        current_tools.add(detail.get("tool") or "unknown")
        current_count += 1

    if current_name:
        phases.append((current_name, set(current_tools), current_count))

    return [
        {"phase": name, "tool_count": count, "tools": sorted(tools)}
        for name, tools, count in phases
    ]


def render_markdown_report(summary: Dict, audit: Dict) -> str:
    lines = [
        "# Agentloop Tool Audit",
        "",
        "## Session",
        "",
        "- session_id: `%s`" % (summary.get("session_id") or ""),
        "- model: `%s`" % (summary.get("model") or ""),
        "- messages: `%s`" % audit.get("message_count", 0),
        "- tool calls: `%s`" % audit.get("tool_call_count", 0),
        "- tool results: `%s`" % audit.get("tool_result_count", 0),
        "- final answer chars: `%s`" % audit.get("final_answer_chars", 0),
        "",
        "## Tools",
        "",
    ]
    tools = audit.get("tool_names") or []
    if tools:
        lines.extend("- `%s`" % tool for tool in tools)
    else:
        lines.append("- No tools observed.")

    lines.extend(["", "## Phases", ""])
    phases = audit.get("phases") or []
    if phases:
        for idx, phase in enumerate(phases, 1):
            lines.append(
                "%s. **%s**: %s tool call(s), tools: %s"
                % (
                    idx,
                    phase.get("phase"),
                    phase.get("tool_count", 0),
                    ", ".join("`%s`" % tool for tool in (phase.get("tools") or [])),
                )
            )
    else:
        lines.append("- No tool phases observed.")

    lines.extend(["", "## Tool Calls", ""])
    details = audit.get("tool_calls") or []
    if details:
        lines.append("| # | tool | status | args | result |")
        lines.append("|---:|---|---|---|---|")
        for detail in details:
            lines.append(
                "| %s | `%s` | `%s` | %s | %s |"
                % (
                    detail.get("index", ""),
                    _md_escape(detail.get("tool") or ""),
                    _md_escape(detail.get("status") or ""),
                    _md_escape(detail.get("args_preview") or ""),
                    _md_escape(detail.get("result_preview") or ""),
                )
            )
    else:
        lines.append("- No tool calls observed.")

    lines.extend(["", "## Findings", ""])
    findings = audit.get("findings") or []
    if findings:
        for finding in findings:
            lines.append(
                "- **%s** `%s`: %s"
                % (finding.get("severity"), finding.get("code"), finding.get("message"))
            )
    else:
        lines.append("- No deterministic audit findings.")
    lines.append("")
    return "\n".join(lines)


def _tool_call_id(call: Dict) -> Optional[str]:
    if not isinstance(call, dict):
        return None
    return call.get("id") or call.get("call_id") or call.get("tool_call_id")


def _tool_call_name(call: Dict) -> Optional[str]:
    if not isinstance(call, dict):
        return None
    function = call.get("function") or {}
    if isinstance(function, dict):
        return function.get("name") or call.get("name")
    return call.get("name")


def _tool_call_args(call: Dict) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    args = function.get("arguments")
    if args is None:
        args = call.get("arguments")
    if isinstance(args, str):
        return args
    if args is None:
        return ""
    return json.dumps(args, ensure_ascii=False, default=str)


def _is_tool_error(message: Dict, content: str) -> bool:
    """Prefer the structured ``is_error`` flag when the source provides one.

    DSH tool-result blocks carry an explicit ``isError`` marker (mapped to
    ``is_error`` by ``dsh_source``); the content heuristic misfires on text
    that merely *mentions* errors (stack traces in test output, agentloop's
    own source code being read, PASS logs). Sources without the flag (e.g.
    xuanjian sessions) keep the heuristic.
    """
    flag = message.get("is_error")
    if isinstance(flag, bool):
        return flag
    return _looks_like_tool_error(content)


def _tool_results_by_id(messages: Iterable[Dict]) -> Dict[str, Dict]:
    results = {}
    for msg in messages:
        if msg.get("role") == "tool" and msg.get("tool_call_id"):
            results[str(msg.get("tool_call_id"))] = msg
    return results


def _tool_result_status(result: Optional[Dict]) -> Tuple[str, str]:
    if not result:
        return "missing", ""
    content = str(result.get("content") or "")
    if _is_tool_error(result, content):
        return "error", _preview(content)
    return "ok", _preview(content)


def _tool_details_from_messages(messages: List[Dict]) -> List[Dict]:
    tool_results = _tool_results_by_id(messages)
    details = []
    for index, msg in enumerate(messages):
        for call in msg.get("tool_calls") or []:
            call_id = _tool_call_id(call)
            status, result_preview = _tool_result_status(tool_results.get(call_id or ""))
            details.append(
                {
                    "index": index,
                    "tool_call_id": call_id,
                    "tool": _tool_call_name(call) or "unknown",
                    "status": status,
                    "args_preview": _preview(_tool_call_args(call)),
                    "result_preview": result_preview,
                }
            )
    return details


def _phase_for_tool(tool_name: str) -> str:
    if tool_name == "skill_view":
        return "load skills"
    if tool_name in {"read_file", "web_extract", "search", "grep"}:
        return "collect context"
    if tool_name in {"write_file", "terminal", "delegate_task"}:
        return "produce artifacts"
    if tool_name in {"clarify", "request_user_input"}:
        return "clarify with user"
    return "use tools"


def _preview(value: str, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _md_escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _looks_like_tool_error(content: str) -> bool:
    if not content:
        return False
    lowered = content.lower()
    if "error executing tool" in lowered or "traceback" in lowered:
        return True
    try:
        payload = json.loads(content)
    except Exception:
        return False
    if isinstance(payload, dict):
        if payload.get("error") or payload.get("status") in {"error", "failed", "cancelled"}:
            return True
        if payload.get("exit_code") not in (None, 0):
            return True
        if _json_contains_failure(payload):
            return True
    return False


def _json_contains_failure(value) -> bool:
    if isinstance(value, dict):
        status = str(value.get("status") or "").lower()
        if status in {"error", "failed", "failure", "cancelled", "timeout", "timed_out"}:
            return True
        if value.get("error"):
            return True
        return any(_json_contains_failure(child) for child in value.values())
    if isinstance(value, list):
        return any(_json_contains_failure(child) for child in value)
    return False


def _last_nonempty_assistant_content(messages: List[Dict]) -> str:
    for msg in reversed(messages):
        content = str(msg.get("content") or "").strip()
        if content:
            return content
    return ""
