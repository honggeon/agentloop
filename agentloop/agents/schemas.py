from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, Iterable, List


ALLOWED_STATUS = {"success", "partial", "failed", "unknown"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_SEVERITY = {"critical", "high", "medium", "low"}
ALLOWED_CATEGORY = {"completion", "tool", "latency", "context", "delivery", "safety"}
ALLOWED_TYPE = {"fact", "inference"}
DIMENSION_DEFINITIONS = {
    "intent_recognition": "意图识别",
    "task_planning": "任务规划",
    "tool_selection": "工具选择",
    "parameter_generation": "参数生成",
    "evidence_citation": "证据引用",
    "state_tracking": "状态更新",
    "final_response": "最终表达",
    "context_management": "上下文管理",
    "execution_closure": "执行闭环",
    "error_recovery": "错误恢复",
    "verification": "验证能力",
    "safety_permission": "安全与权限",
    "efficiency": "成本与效率",
}
ALLOWED_DIMENSION_STATUS = {"pass", "partial", "fail", "unknown"}
ALLOWED_DIMENSION_SOURCE = {"deterministic", "judge", "deterministic+judge", "missing"}
EVIDENCE_SOURCE_ALIASES = {
    "trajectory": "trajectory.jsonl",
    "trajectory.jsonl": "trajectory.jsonl",
    "agent": "trajectory.jsonl",
    "messages": "trajectory.jsonl",
    "audit": "audit.json",
    "audit.json": "audit.json",
    "tools": "audit.json",
    "summary": "summary.json",
    "summary.json": "summary.json",
    "session": "session.json",
    "session.json": "session.json",
}


def extract_json_object(value: object) -> Dict[str, Any]:
    for candidate in _iter_json_dicts(value, depth=0):
        if "overall_status" in candidate:
            return dict(candidate)
    preview = _response_preview(value)
    if preview:
        raise ValueError("agent response did not contain a JSON object; last content preview: %s" % preview)
    raise ValueError("agent response did not contain a JSON object")


def resolve_judge_report(raw: object, run_path: object = None) -> Dict[str, Any]:
    try:
        return normalize_judge_report(raw)
    except ValueError as exc:
        if "did not contain a JSON object" not in str(exc) or run_path is None:
            raise
        from pathlib import Path

        path = Path(str(run_path)) / "agent_judge.json"
        if not path.exists():
            raise
        return normalize_judge_report(json.loads(path.read_text(encoding="utf-8")))


def _iter_json_dicts(value: object, depth: int) -> Iterable[Dict[str, Any]]:
    if depth > 8 or value is None:
        return
    if isinstance(value, dict):
        if "overall_status" in value:
            yield dict(value)
        messages = value.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                for part in _iter_message_parts(message):
                    yield from _iter_json_dicts(part, depth + 1)
        for key in ("output", "report", "result", "content", "text", "args", "arguments", "input"):
            if key in value and key != "messages":
                yield from _iter_json_dicts(value.get(key), depth + 1)
        yield from _iter_tool_call_payloads(value.get("tool_calls") or [])
        additional = value.get("additional_kwargs")
        if isinstance(additional, dict):
            yield from _iter_tool_call_payloads(additional.get("tool_calls") or [])
        return

    if isinstance(value, list):
        for item in reversed(value):
            if isinstance(item, dict) and item.get("type") in {"tool_use", "tool_call", "function_call"}:
                yield from _iter_json_dicts(
                    item.get("input") or item.get("arguments") or item.get("args") or item.get("report"),
                    depth + 1,
                )
            yield from _iter_json_dicts(item, depth + 1)
        text = _coerce_text(value)
        if text:
            yield from _iter_json_dicts(text, depth + 1)
        return

    text = _coerce_text(value)
    if not text:
        return
    for blob in _json_object_strings(text):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield parsed


def _iter_message_parts(message: object) -> Iterable[object]:
    if isinstance(message, dict):
        yield message.get("content")
        yield message.get("report")
        yield from _iter_tool_call_payloads(message.get("tool_calls") or [])
        additional = message.get("additional_kwargs") or {}
        if isinstance(additional, dict):
            yield from _iter_tool_call_payloads(additional.get("tool_calls") or [])
        return
    yield getattr(message, "content", None)
    yield getattr(message, "report", None)
    yield from _iter_tool_call_payloads(getattr(message, "tool_calls", None) or [])
    additional = getattr(message, "additional_kwargs", None) or {}
    if isinstance(additional, dict):
        yield from _iter_tool_call_payloads(additional.get("tool_calls") or [])


def _iter_tool_call_payloads(tool_calls: object) -> Iterable[object]:
    if not isinstance(tool_calls, list):
        return
    for call in reversed(tool_calls):
        if not isinstance(call, dict):
            args = getattr(call, "args", None)
            if args is not None:
                yield args
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        args = call.get("args")
        if args is None:
            args = call.get("arguments")
        if args is None:
            args = call.get("input")
        if args is None:
            args = function.get("arguments")
        if isinstance(args, str):
            stripped = args.strip()
            if stripped:
                yield stripped
            continue
        if isinstance(args, dict):
            if args.get("report") is not None:
                yield args.get("report")
            yield args
            continue
        if args is not None:
            yield args


def _response_preview(value: object, limit: int = 240) -> str:
    texts: List[str] = []
    if isinstance(value, dict) and isinstance(value.get("messages"), list):
        for message in reversed(value.get("messages") or []):
            content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
            text = _coerce_text(content)
            if text:
                texts.append(text)
                break
    else:
        text = _coerce_text(value)
        if text:
            texts.append(text)
    preview = " | ".join(texts).replace("\n", " ").strip()
    if len(preview) > limit:
        return preview[: limit - 3] + "..."
    return preview


def _coerce_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: List[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
                continue
            if isinstance(item, dict):
                for key in ("text", "content", "output"):
                    if item.get(key):
                        parts.append(str(item[key]))
                        break
                continue
            text = getattr(item, "text", None) or getattr(item, "content", None)
            if text:
                parts.append(str(text))
        return "\n".join(part.strip() for part in parts if str(part).strip())
    return str(value).strip()


def _json_object_strings(text: str) -> List[str]:
    blobs: List[str] = []
    for match in re.finditer(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE):
        block = match.group(1).strip()
        if block.startswith("{"):
            blobs.append(block)
    blobs.extend(_balanced_json_objects(text))
    # Prefer later blobs (closer to the final answer) while keeping uniqueness.
    ordered: List[str] = []
    seen = set()
    for blob in reversed(blobs):
        if blob in seen:
            continue
        seen.add(blob)
        ordered.append(blob)
    return ordered


def _balanced_json_objects(text: str) -> List[str]:
    blobs: List[str] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            parsed, offset = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            index = start + 1
            continue
        if isinstance(parsed, dict):
            blobs.append(text[start : start + offset])
        index = start + max(offset, 1)
    return blobs


def normalize_judge_report(raw: object) -> Dict[str, Any]:
    report = extract_json_object(raw)
    status = _choice(report.get("overall_status"), ALLOWED_STATUS, "unknown", "overall_status")
    confidence = _choice(report.get("confidence"), ALLOWED_CONFIDENCE, "low", "confidence")
    score = _score(report.get("completion_score", 0))
    findings = [_normalize_finding(item) for item in report.get("findings") or []]
    metrics = dict(report.get("metrics") or {})
    metrics["completion_score"] = score
    metrics.setdefault("message_count", 0)
    metrics.setdefault("tool_call_count", 0)
    metrics.setdefault("tool_error_count", 0)
    charts = dict(report.get("charts") or {})
    charts["finding_severity"] = _count_by(findings, "severity")
    charts.setdefault("tool_distribution", [])
    charts.setdefault("phase_timeline", [])
    dimensions = _normalize_dimensions(report.get("dimensions"))
    judge_reliability = _normalize_judge_reliability(report.get("judge_reliability"))
    charts["dimension_scores"] = [
        {
            "dimension": key,
            "label": DIMENSION_DEFINITIONS[key],
            "score": row["score"],
            "status": row["status"],
            "source": row["source"],
        }
        for key, row in dimensions.items()
    ]
    return {
        "overall_status": status,
        "completion_score": score,
        "confidence": confidence,
        "summary": str(report.get("summary") or ""),
        "findings": findings,
        "metrics": metrics,
        "charts": charts,
        "dimensions": dimensions,
        "judge_reliability": judge_reliability,
    }


def render_judge_markdown(report: Dict[str, Any]) -> str:
    normalized = normalize_judge_report(report)
    lines = [
        "# Agentloop Agent Judge",
        "",
        "## Summary",
        "",
        "- status: `%s`" % normalized["overall_status"],
        "- completion_score: `%s`" % normalized["completion_score"],
        "- confidence: `%s`" % normalized["confidence"],
        "",
        normalized["summary"] or "No summary provided.",
        "",
        "## Findings",
        "",
    ]
    findings = normalized.get("findings") or []
    if not findings:
        lines.append("- No findings.")
    for item in findings:
        lines.append(
            "- **%s** `%s/%s/%s`: %s"
            % (item["severity"], item["category"], item["type"], item["title"], item["recommendation"])
        )
        for evidence in item.get("evidence") or []:
            source = evidence.get("source", "")
            index = evidence.get("message_index")
            quote = evidence.get("quote", "")
            label = "%s#%s" % (source, index) if index is not None else source
            lines.append("  - evidence `%s`: %s" % (label, quote))
    lines.append("")
    return "\n".join(lines)


def _normalize_finding(item: object) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("finding must be a dict")
    finding_type = _choice(item.get("type"), ALLOWED_TYPE, "", "type", strict=True)
    evidence = item.get("evidence") or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    elif isinstance(evidence, str):
        evidence = [{"source": "agent", "quote": evidence}]
    if not isinstance(evidence, list):
        raise ValueError("finding evidence must be a list")
    normalized_evidence: List[Dict[str, Any]] = []
    for row in evidence:
        normalized_evidence.extend(_normalize_evidence(row))
    return {
        "severity": _choice(item.get("severity"), ALLOWED_SEVERITY, "low", "severity"),
        "category": _choice(item.get("category"), ALLOWED_CATEGORY, "tool", "category"),
        "title": str(item.get("title") or ""),
        "type": finding_type,
        "evidence": normalized_evidence,
        "recommendation": str(item.get("recommendation") or ""),
    }


def _normalize_dimensions(value: object) -> Dict[str, Dict[str, Any]]:
    raw = value if isinstance(value, dict) else {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for key in DIMENSION_DEFINITIONS:
        item = raw.get(key)
        if item is None:
            item = raw.get(DIMENSION_DEFINITIONS[key])
        normalized[key] = _normalize_dimension(item)
    return normalized


def _dimension_status(value: object) -> str:
    text = str(value or "unknown").strip().lower()
    text = {
        "通过": "pass",
        "合格": "pass",
        "部分通过": "partial",
        "失败": "fail",
        "不通过": "fail",
        "未知": "unknown",
    }.get(text, text)
    return _choice(text, ALLOWED_DIMENSION_STATUS, "unknown", "dimension.status")


def _dimension_source(value: object) -> str:
    text = str(value or "missing").strip().lower()
    text = {"规则": "deterministic", "确定性": "deterministic", "模型": "judge", "评审": "judge"}.get(text, text)
    return _choice(text, ALLOWED_DIMENSION_SOURCE, "missing", "dimension.source")


def _normalize_dimension(value: object) -> Dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    evidence = item.get("evidence") or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    elif isinstance(evidence, str):
        evidence = [{"source": "agent", "quote": evidence}]
    if not isinstance(evidence, list):
        evidence = []
    normalized_evidence: List[Dict[str, Any]] = []
    for row in evidence:
        normalized_evidence.extend(_normalize_evidence(row))
    return {
        "score": _score(item.get("score", 0)),
        "status": _dimension_status(item.get("status")),
        "summary": str(item.get("summary") or ""),
        "evidence": normalized_evidence,
        "recommendation": str(item.get("recommendation") or ""),
        "source": _dimension_source(item.get("source")),
    }


def _normalize_judge_reliability(value: object) -> Dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    total = max(0, int(item.get("total_evidence_count") or 0))
    valid = max(0, int(item.get("valid_evidence_count") or 0))
    coverage = item.get("evidence_coverage")
    if coverage is None:
        coverage = round(valid / total, 4) if total else 0
    notes = item.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list):
        notes = []
    invalid = item.get("invalid_evidence") or []
    if not isinstance(invalid, list):
        invalid = []
    return {
        "evidence_coverage": max(0, min(1, float(coverage))),
        "unsupported_claim_count": max(0, int(item.get("unsupported_claim_count") or 0)),
        "invalid_evidence_count": max(0, int(item.get("invalid_evidence_count") or 0)),
        "fact_inference_mismatch_count": max(0, int(item.get("fact_inference_mismatch_count") or 0)),
        "valid_evidence_count": valid,
        "total_evidence_count": total,
        "notes": [str(note) for note in notes if str(note).strip()],
        "invalid_evidence": [row for row in invalid if isinstance(row, dict)],
    }


def _normalize_evidence(item: object) -> List[Dict[str, Any]]:
    if isinstance(item, str):
        return [{"source": "agent", "quote": item[:500]}]
    if not isinstance(item, dict):
        raise ValueError("evidence must be a dict")
    source = str(item.get("source") or "")
    aliased = EVIDENCE_SOURCE_ALIASES.get(source.strip().lower(), source)
    base = {"source": aliased, "quote": str(item.get("quote") or "")[:500]}
    if item.get("message_index") is None:
        return [base]
    indexes = _parse_message_indexes(item["message_index"])
    if not indexes:
        return [base]
    return [{**base, "message_index": index} for index in indexes]


def _parse_message_indexes(value: object) -> List[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, float):
        if not value.is_integer():
            return []
        return [int(value)]
    if isinstance(value, (list, tuple)):
        indexes: List[int] = []
        for item in value:
            indexes.extend(_parse_message_indexes(item))
        return indexes
    if isinstance(value, str):
        return _parse_message_index_text(value)
    try:
        return [int(value)]
    except (TypeError, ValueError):
        return []


def _parse_message_index_text(value: str) -> List[int]:
    text = value.strip()
    if not text:
        return []
    if "," in text:
        indexes: List[int] = []
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            indexes.extend(_parse_message_index_text(part))
        return indexes
    try:
        return [int(text)]
    except ValueError:
        pass
    # Models often emit ranges/annotations like "5-26 (web_search×16)".
    range_match = re.match(r"^\s*(\d+)\s*-\s*(\d+)\b", text)
    if range_match:
        start = int(range_match.group(1))
        end = int(range_match.group(2))
        return [start, end] if start <= end else [end, start]
    match = re.search(r"-?\d+", text)
    if match:
        return [int(match.group(0))]
    return []


def _choice(value: object, allowed: set[str], default: str, field: str, strict: bool = False) -> str:
    text = str(value or default).strip().lower()
    text = _CHOICE_SYNONYMS.get(text, text)
    if text in allowed:
        return text
    if not strict and default in allowed:
        return default
    raise ValueError("%s must be one of %s, got %r" % (field, sorted(allowed), value))


_CHOICE_SYNONYMS = {
    # severity / confidence
    "严重": "critical",
    "致命": "critical",
    "高": "high",
    "中": "medium",
    "中等": "medium",
    "低": "low",
    # type
    "事实": "fact",
    "推断": "inference",
    "推测": "inference",
    # overall_status
    "成功": "success",
    "部分成功": "partial",
    "部分": "partial",
    "失败": "failed",
    "未知": "unknown",
    # category
    "完成": "completion",
    "工具": "tool",
    "延迟": "latency",
    "上下文": "context",
    "交付": "delivery",
    "安全": "safety",
}


def _score(value: object) -> int:
    return max(0, min(100, int(value)))


def _count_by(rows: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    counts = Counter(str(row.get(key) or "") for row in rows)
    return [{key: name, "count": count} for name, count in sorted(counts.items()) if name]
