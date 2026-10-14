from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional

from .audit import _looks_like_tool_error


def build_runtime_graph(messages: Iterable[Dict]) -> Dict:
    nodes: List[Dict] = []
    edges: List[Dict] = []
    call_nodes: Dict[str, str] = {}
    previous_id: Optional[str] = None

    for index, message in enumerate(m for m in messages if isinstance(m, dict)):
        role = message.get("role")
        if role == "user":
            previous_id = _append_node(nodes, edges, previous_id, _node("msg-%s" % index, "user", "User Prompt", message))
            continue

        if role == "assistant":
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                previous_id = _append_node(
                    nodes,
                    edges,
                    previous_id,
                    _node("msg-%s" % index, "assistant", "Assistant Tool Request", message),
                )
                for call_index, call in enumerate(tool_calls):
                    call_id = _tool_call_id(call) or "msg-%s-call-%s" % (index, call_index)
                    tool_name = _tool_call_name(call) or "unknown"
                    node_type = "skill" if tool_name == "skill_view" else "tool"
                    tool_node_id = "call-%s" % call_id
                    previous_id = _append_node(
                        nodes,
                        edges,
                        previous_id,
                        {
                            "id": tool_node_id,
                            "type": node_type,
                            "label": _tool_label(tool_name, call),
                            "status": "running",
                            "detail": _tool_call_args(call),
                        },
                    )
                    call_nodes[call_id] = tool_node_id
            elif str(message.get("content") or "").strip():
                previous_id = _append_node(nodes, edges, previous_id, _node("msg-%s" % index, "final", "Final Answer", message))
            continue

        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            source_id = call_nodes.get(call_id)
            result_id = "result-%s" % (call_id or index)
            status = (
                "error"
                if message.get("status") == "error" or _looks_like_tool_error(str(message.get("content") or ""))
                else "ok"
            )
            if source_id:
                source_node = next((node for node in nodes if node.get("id") == source_id), None)
                if source_node:
                    source_node["status"] = status
            result_node = _node(result_id, "tool_result", "%s result" % (message.get("tool_name") or "tool"), message, status)
            nodes.append(result_node)
            if source_id:
                edges.append({"from": source_id, "to": result_id})
            elif previous_id:
                edges.append({"from": previous_id, "to": result_id})
            previous_id = result_id

    return {
        "nodes": nodes,
        "edges": edges,
        "current_node": nodes[-1]["id"] if nodes else None,
    }


def build_runtime_trace(
    messages: Iterable[Dict],
    *,
    trace_id: str = "",
    session_id: str = "",
    agent_name: str = "Agent Runtime",
    agent_version: str = "",
) -> Dict:
    rows = [m for m in messages if isinstance(m, dict)]
    spans: List[Dict[str, Any]] = []
    call_spans: Dict[str, str] = {}
    previous_span_id: Optional[str] = None
    base_time = datetime.now(timezone.utc)
    failed = False
    total_tokens = 0
    llm_calls = 0
    tool_calls = 0
    skill_calls = 0

    for index, message in enumerate(rows):
        role = message.get("role")
        start_time = _span_time(base_time, index)

        if role == "user":
            span = _span(
                "span-%s-user" % index,
                previous_span_id,
                "User Request",
                "Agent",
                start_time,
                input_value=message.get("content") or "",
                output_value=None,
                status="success",
                attributes={"role": "user"},
            )
            spans.append(span)
            previous_span_id = span["spanId"]
            continue

        if role == "assistant":
            tool_call_items = [call for call in message.get("tool_calls") or [] if isinstance(call, dict)]
            content = str(message.get("content") or "").strip()
            usage = _token_usage(message)
            total_tokens += int(usage.get("total") or 0)

            if tool_call_items or content:
                llm_calls += 1 if tool_call_items else 0
                llm_span = _span(
                    "span-%s-llm" % index,
                    previous_span_id,
                    "LLM Call" if tool_call_items else "Final Response",
                    "LLM" if tool_call_items else "Agent",
                    start_time,
                    input_value=_message_input(message),
                    output_value=content or _tool_call_summary(tool_call_items),
                    status="success",
                    token_usage=usage,
                    attributes={
                        "model": message.get("model") or message.get("model_name") or "",
                        "role": "assistant",
                    },
                )
                spans.append(llm_span)
                previous_span_id = llm_span["spanId"]

            for call_index, call in enumerate(tool_call_items):
                call_id = _tool_call_id(call) or "msg-%s-call-%s" % (index, call_index)
                tool_name = _tool_call_name(call) or "unknown"
                span_type = "Skill" if tool_name == "skill_view" else "Tool"
                if span_type == "Skill":
                    skill_calls += 1
                else:
                    tool_calls += 1
                call_span = _span(
                    "span-%s-call-%s" % (index, call_index),
                    previous_span_id,
                    _tool_label(tool_name, call),
                    span_type,
                    _span_time(base_time, index, call_index + 1),
                    input_value=_parse_json_or_text(_tool_call_args(call)),
                    output_value=None,
                    status="running",
                    attributes={
                        "toolName": tool_name,
                        "decision": "Tool Decision",
                    },
                )
                spans.append(call_span)
                call_spans[call_id] = call_span["spanId"]
                previous_span_id = call_span["spanId"]
            continue

        if role == "tool":
            call_id = str(message.get("tool_call_id") or "")
            tool_name = str(message.get("tool_name") or "tool")
            status = (
                "failed"
                if message.get("status") == "error" or _looks_like_span_error(str(message.get("content") or ""))
                else "success"
            )
            failed = failed or status == "failed"
            parent_span_id = call_spans.get(call_id) or previous_span_id
            if parent_span_id:
                parent_span = next((span for span in spans if span.get("spanId") == parent_span_id), None)
                if parent_span and parent_span.get("status") == "running":
                    parent_span["status"] = status
            span_type = "Skill" if tool_name == "skill_view" else "Tool"
            result_span = _span(
                "span-%s-result" % index,
                parent_span_id,
                "%s result" % tool_name,
                span_type,
                start_time,
                input_value=None,
                output_value=_parse_json_or_text(str(message.get("content") or "")),
                status=status,
                attributes={"toolName": tool_name, "toolCallId": call_id},
                error=_tool_error(message) if status == "failed" else None,
            )
            spans.append(result_span)
            previous_span_id = result_span["spanId"]

    trace_start = spans[0]["startTime"] if spans else _iso(base_time)
    trace_end = spans[-1]["endTime"] if spans else trace_start
    total_latency = _latency_seconds(trace_start, trace_end)
    has_final = _has_final_assistant(rows)
    has_open_tools = _has_open_tool_calls(rows)
    trace_status = "success" if has_final else "running" if has_open_tools else "failed" if failed else "success"
    return {
        "traceId": trace_id or session_id or "runtime-trace",
        "sessionId": session_id,
        "agentName": agent_name,
        "agentVersion": agent_version,
        "startTime": trace_start,
        "endTime": trace_end,
        "status": trace_status,
        "totalLatency": total_latency,
        "totalTokens": total_tokens,
        "cost": _estimate_cost(total_tokens),
        "metrics": {
            "steps": len(spans),
            "llmCalls": llm_calls,
            "toolCalls": tool_calls,
            "skillCalls": skill_calls,
        },
        "spans": spans,
    }


def save_expectations(run_dir: Path, acceptance_criteria: str) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {"acceptance_criteria": str(acceptance_criteria or "").strip()}
    (run_dir / "expectations.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _span(
    span_id: str,
    parent_span_id: Optional[str],
    name: str,
    span_type: str,
    start_time: datetime,
    *,
    input_value: Any,
    output_value: Any,
    status: str,
    token_usage: Optional[Dict[str, int]] = None,
    attributes: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    latency = float((attributes or {}).get("latency") or 0)
    end_time = start_time + timedelta(seconds=latency)
    return {
        "spanId": span_id,
        "parentSpanId": parent_span_id,
        "name": name,
        "type": span_type,
        "startTime": _iso(start_time),
        "endTime": _iso(end_time),
        "latency": latency,
        "input": input_value,
        "output": output_value,
        "status": status,
        "tokenUsage": token_usage or {"input": 0, "output": 0, "total": 0},
        "error": error,
        "attributes": attributes or {},
    }


def _span_time(base_time: datetime, index: int, offset: int = 0) -> datetime:
    return base_time + timedelta(seconds=index, milliseconds=offset * 100)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _latency_seconds(start: str, end: str) -> float:
    try:
        start_time = datetime.fromisoformat(start.replace("Z", "+00:00"))
        end_time = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return round(max((end_time - start_time).total_seconds(), 0.0), 3)


def _message_input(message: Dict) -> Any:
    if message.get("prompt"):
        return message.get("prompt")
    if message.get("messages"):
        return message.get("messages")
    return message.get("content") or ""


def _tool_call_summary(tool_calls: List[Dict]) -> List[Dict[str, Any]]:
    return [
        {
            "name": _tool_call_name(call) or "unknown",
            "arguments": _parse_json_or_text(_tool_call_args(call)),
        }
        for call in tool_calls
    ]


def _token_usage(message: Dict) -> Dict[str, int]:
    usage = message.get("usage") or message.get("token_usage") or {}
    if not isinstance(usage, dict):
        return {"input": 0, "output": 0, "total": 0}
    input_tokens = _int_value(usage.get("prompt_tokens") or usage.get("input_tokens") or usage.get("input") or 0)
    output_tokens = _int_value(
        usage.get("completion_tokens") or usage.get("output_tokens") or usage.get("output") or 0
    )
    total_tokens = _int_value(usage.get("total_tokens") or usage.get("total") or input_tokens + output_tokens)
    return {"input": input_tokens, "output": output_tokens, "total": total_tokens}


def _int_value(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_json_or_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _tool_error(message: Dict) -> Dict[str, str]:
    content = str(message.get("content") or "")
    first_line = content.splitlines()[0] if content else "Tool execution failed"
    error_type = first_line.split(":", 1)[0].strip() or "ToolError"
    return {"type": error_type if "Error" in error_type else "ToolError", "message": _preview(content, 500)}


def _looks_like_span_error(content: str) -> bool:
    if _looks_like_tool_error(content):
        return True
    first_line = str(content or "").splitlines()[0] if content else ""
    lowered = first_line.lower()
    return "error" in lowered or "exception" in lowered or "failed" in lowered


def _has_final_assistant(messages: Iterable[Dict]) -> bool:
    for message in reversed([m for m in messages if isinstance(m, dict)]):
        if message.get("role") == "assistant" and str(message.get("content") or "").strip():
            return True
    return False


def _has_open_tool_calls(messages: Iterable[Dict]) -> bool:
    requested = set()
    completed = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        for call in message.get("tool_calls") or []:
            call_id = _tool_call_id(call)
            if call_id:
                requested.add(str(call_id))
        if message.get("role") == "tool" and message.get("tool_call_id"):
            completed.add(str(message.get("tool_call_id")))
    return bool(requested - completed)


def _estimate_cost(total_tokens: int) -> float:
    return round((total_tokens / 1000.0) * 0.005, 4)


def load_expectations(run_path: str | Path) -> Dict:
    path = Path(run_path) / "expectations.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def build_agent_judge_prompt(run_path: str | Path) -> str:
    prompt = (
        "Analyze this Auditrail run and write an evidence-backed judge report.\n"
        "run_path: %s\n"
    ) % Path(run_path)
    criteria = str(load_expectations(run_path).get("acceptance_criteria") or "").strip()
    if criteria:
        prompt += (
            "\nAgent Judge acceptance criteria:\n"
            "%s\n"
            "\nJudge the final outcome against these criteria. Treat deterministic audit data as factual evidence, "
            "but use Agent Judge reasoning for the natural-language acceptance criteria.\n"
        ) % criteria
    prompt += "Return only the final JSON object matching the Auditrail judge schema."
    return prompt


def _append_node(nodes: List[Dict], edges: List[Dict], previous_id: Optional[str], node: Dict) -> str:
    nodes.append(node)
    if previous_id:
        edges.append({"from": previous_id, "to": node["id"]})
    return str(node["id"])


def _node(node_id: str, node_type: str, label: str, message: Dict, status: str = "ok") -> Dict:
    return {
        "id": node_id,
        "type": node_type,
        "label": label,
        "status": status,
        "detail": _preview(message.get("content") or ""),
    }


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


def _tool_label(tool_name: str, call: Dict) -> str:
    if tool_name != "skill_view":
        return tool_name
    args = _parse_json_object(_tool_call_args(call))
    skill_name = str(args.get("name") or "").strip()
    return "skill_view: %s" % skill_name if skill_name else "skill_view"


def _parse_json_object(value: str) -> Dict:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _preview(value: str, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."
