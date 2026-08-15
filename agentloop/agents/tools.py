from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dimensions import assess_dimensions
from .evidence import validate_judge_evidence
from .schemas import DIMENSION_DEFINITIONS, normalize_judge_report, render_judge_markdown


def _runs_roots() -> List[Path]:
    roots = []
    env_root = os.getenv("AGENTLOOP_RUNS_DIR")
    if env_root:
        roots.append(Path(env_root).expanduser())
    roots.append(Path("runs"))
    return roots


def _resolve_run_dir(run_path: str) -> Path:
    """Resolve a run directory, tolerating relative, partial or mangled paths."""
    candidate = Path(run_path).expanduser()
    if candidate.is_dir():
        return candidate
    fragment = candidate.name or str(run_path).strip("/")
    if fragment and fragment not in {".", ".."}:
        for root in _runs_roots():
            if not root.is_dir():
                continue
            matches = sorted(
                path for path in root.iterdir() if path.is_dir() and fragment in path.name
            )
            if matches:
                return matches[0]
    raise FileNotFoundError(
        "cannot resolve run_path %r; call list_runs() and use an exact run path" % run_path
    )


def list_runs(root: str = "runs") -> List[Dict[str, Any]]:
    """List exported Agentloop run directories under the given root."""
    root_path = Path(root)
    if not root_path.exists():
        return []
    runs = []
    for path in sorted(root_path.iterdir()):
        if not path.is_dir():
            continue
        summary_path = path / "summary.json"
        audit_path = path / "audit.json"
        runs.append(
            {
                "name": path.name,
                "path": str(path),
                "has_summary": summary_path.exists(),
                "has_audit": audit_path.exists(),
                "has_trajectory": (path / "trajectory.jsonl").exists(),
                "has_judge": (path / "agent_judge.json").exists(),
            }
        )
    return runs


def load_run_summary(run_path: str) -> Dict[str, Any]:
    """Load summary.json from an Agentloop run directory."""
    try:
        return _read_json(_required(_resolve_run_dir(run_path) / "summary.json"))
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}


def load_audit(run_path: str) -> Dict[str, Any]:
    """Load audit.json from an Agentloop run directory."""
    try:
        return _read_json(_required(_resolve_run_dir(run_path) / "audit.json"))
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}


def load_trajectory_slice(run_path: str, offset: int = 0, limit: int = 50) -> Any:
    """Load a bounded slice of trajectory.jsonl rows from an Agentloop run directory."""
    try:
        rows = _read_jsonl(_required(_resolve_run_dir(run_path) / "trajectory.jsonl"))
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    start = max(0, int(offset))
    stop = start + max(0, int(limit))
    return rows[start:stop]


def search_trajectory(run_path: str, keyword: str) -> Any:
    """Search trajectory.jsonl rows by keyword and return matching rows."""
    needle = str(keyword).lower()
    if not needle:
        return []
    try:
        rows = _read_jsonl(_required(_resolve_run_dir(run_path) / "trajectory.jsonl"))
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    matches = []
    for row in rows:
        text = json.dumps(row, ensure_ascii=False, default=str).lower()
        if needle in text:
            matches.append(row)
    return matches


def get_tool_calls(run_path: str, tool_name: Optional[str] = None, status: Optional[str] = None) -> Any:
    """Return tool call audit rows, optionally filtered by tool name and status."""
    audit = load_audit(run_path)
    if "error" in audit:
        return audit
    calls = audit.get("tool_calls") or []
    result = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        if tool_name is not None and call.get("tool") != tool_name:
            continue
        if status is not None and call.get("status") != status:
            continue
        result.append(dict(call))
    return result


def build_chart_data(run_path: str) -> Dict[str, Any]:
    """Build chart-ready metrics from audit.json for a run directory."""
    audit = load_audit(run_path)
    if "error" in audit:
        return audit
    tool_counts = Counter(
        str(call.get("tool") or "")
        for call in audit.get("tool_calls") or []
        if isinstance(call, dict) and call.get("tool")
    )
    finding_counts = Counter(
        str(item.get("severity") or "")
        for item in audit.get("findings") or []
        if isinstance(item, dict) and item.get("severity")
    )
    return {
        "tool_distribution": [{"tool": name, "count": count} for name, count in sorted(tool_counts.items())],
        "phase_timeline": audit.get("phases") or [],
        "deterministic_finding_severity": [
            {"severity": name, "count": count} for name, count in sorted(finding_counts.items())
        ],
    }


def assess_run_quality(run_path: str) -> Dict[str, Any]:
    """Assess completion, deliverables, and evidence chain from run artifacts."""
    run_dir = _resolve_run_dir(run_path)
    summary = load_run_summary(str(run_dir))
    audit = load_audit(str(run_dir))
    trajectory = load_trajectory_slice(str(run_dir), 0, 10000)
    if "error" in summary:
        summary = {}
    if "error" in audit:
        audit = {}
    if isinstance(trajectory, dict) and "error" in trajectory:
        trajectory = []

    requested = _requested_deliverables(summary, trajectory)
    produced = _produced_deliverables(run_dir, requested)
    missing = [suffix for suffix in requested if suffix not in produced]
    final_message = _last_assistant_message(trajectory)
    tool_failure = _first_tool_failure(audit, trajectory)

    evidence_chain = _quality_evidence(summary, requested, tool_failure, final_message)
    findings = []
    status = "unknown"
    score_cap = 100

    if missing:
        status = "failed"
        score_cap = 40
        findings.append(
            {
                "severity": "critical",
                "category": "delivery",
                "title": "Requested deliverable was not produced",
                "type": "fact",
                "evidence": evidence_chain,
                "recommendation": "Treat requested files as hard completion gates and verify they exist before reporting success.",
            }
        )
    elif requested:
        status = "success"

    if tool_failure:
        findings.append(
            {
                "severity": "high" if missing else "medium",
                "category": "tool",
                "title": "Tool failure affected completion evidence",
                "type": "fact",
                "evidence": [tool_failure],
                "recommendation": "Resolve or explicitly account for failed tool steps before judging the run complete.",
            }
        )

    return {
        "completion": {"status": status, "score_cap": score_cap},
        "deliverables": {"requested": requested, "produced": produced, "missing": missing},
        "evidence_chain": evidence_chain,
        "findings": findings,
    }


def write_judge_report(run_path: str, report: object) -> Dict[str, str]:
    """Normalize and write agent_judge.json, agent_judge.md, and chart_data.json."""
    run_dir = _resolve_run_dir(run_path)
    normalized = normalize_judge_report(report)
    reliability = validate_judge_evidence(str(run_dir), normalized)
    existing_notes = list((normalized.get("judge_reliability") or {}).get("notes") or [])
    merged_notes = existing_notes + [note for note in reliability.get("notes") or [] if note not in existing_notes]
    reliability["notes"] = merged_notes
    quality = assess_run_quality(str(run_dir))
    normalized = _merge_quality_report(normalized, quality)
    normalized["judge_reliability"] = reliability
    normalized["dimensions"] = assess_dimensions(str(run_dir), normalized, reliability)
    normalized = normalize_judge_report(normalized)
    chart_data = build_chart_data(str(run_dir))
    charts = dict(chart_data)
    charts.update(normalized.get("charts") or {})
    charts["finding_severity"] = _count_by(normalized.get("findings") or [], "severity")
    charts["quality_summary"] = {
        "requested_deliverables": len(quality["deliverables"]["requested"]),
        "produced_deliverables": len(quality["deliverables"]["produced"]),
        "missing_deliverables": len(quality["deliverables"]["missing"]),
    }
    charts["dimension_scores"] = [
        {
            "dimension": key,
            "label": DIMENSION_DEFINITIONS[key],
            "score": value["score"],
            "status": value["status"],
            "source": value["source"],
        }
        for key, value in normalized.get("dimensions", {}).items()
    ]
    charts["judge_reliability"] = {
        "evidence_coverage": normalized["judge_reliability"]["evidence_coverage"],
        "unsupported_claim_count": normalized["judge_reliability"]["unsupported_claim_count"],
        "invalid_evidence_count": normalized["judge_reliability"]["invalid_evidence_count"],
        "fact_inference_mismatch_count": normalized["judge_reliability"]["fact_inference_mismatch_count"],
    }
    normalized["charts"] = charts

    json_path = run_dir / "agent_judge.json"
    markdown_path = run_dir / "agent_judge.md"
    chart_path = run_dir / "chart_data.json"
    json_path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_path.write_text(render_judge_markdown(normalized), encoding="utf-8")
    chart_path.write_text(json.dumps(normalized["charts"], ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
        "chart_path": str(chart_path),
    }


def _count_by(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    counts = Counter(str(item.get(key) or "") for item in items if isinstance(item, dict) and item.get(key))
    return [{key: name, "count": count} for name, count in sorted(counts.items())]


def _merge_quality_report(report: Dict[str, Any], quality: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(report)
    completion = quality.get("completion") or {}
    score_cap = int(completion.get("score_cap", 100))
    if score_cap < int(merged.get("completion_score", 0)):
        merged["completion_score"] = score_cap
    quality_status = completion.get("status")
    if quality_status in {"success", "partial", "failed"}:
        merged["overall_status"] = _worst_status(str(merged.get("overall_status") or "unknown"), quality_status)

    metrics = dict(merged.get("metrics") or {})
    deliverables = quality.get("deliverables") or {}
    metrics["requested_deliverables"] = list(deliverables.get("requested") or [])
    metrics["produced_deliverables"] = list(deliverables.get("produced") or [])
    metrics["missing_deliverables"] = list(deliverables.get("missing") or [])
    metrics["quality_score_cap"] = score_cap
    merged["metrics"] = metrics

    findings = list(merged.get("findings") or [])
    seen = {(item.get("category"), item.get("title")) for item in findings if isinstance(item, dict)}
    for finding in quality.get("findings") or []:
        key = (finding.get("category"), finding.get("title"))
        if key not in seen:
            findings.append(finding)
            seen.add(key)
    merged["findings"] = findings
    return normalize_judge_report(merged)


def _worst_status(current: str, quality: str) -> str:
    order = {"unknown": 0, "success": 1, "partial": 2, "failed": 3}
    return quality if order.get(quality, 0) > order.get(current, 0) else current


def _requested_deliverables(summary: Dict[str, Any], trajectory: Any) -> List[str]:
    text_parts = [str(summary.get("title") or "")]
    for row in trajectory if isinstance(trajectory, list) else []:
        if isinstance(row, dict) and row.get("role") == "user":
            text_parts.append(str(row.get("content") or ""))
    text = "\n".join(text_parts).lower()
    requested = []
    rules = [
        (".pptx", (".pptx", "pptx", "ppt", "演示文稿")),
        (".pdf", (".pdf", "pdf")),
        (".docx", (".docx", "word", "docx")),
        (".xlsx", (".xlsx", "excel", "xlsx")),
        (".md", (".md", "markdown")),
    ]
    for suffix, needles in rules:
        if any(needle in text for needle in needles):
            requested.append(suffix)
    return requested


def _produced_deliverables(run_dir: Path, requested: List[str]) -> List[str]:
    if not requested:
        return []
    produced = set()
    for path in run_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in requested:
            produced.add(path.suffix.lower())
    return sorted(produced)


def _last_assistant_message(trajectory: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(trajectory, list):
        return None
    for row in reversed(trajectory):
        if isinstance(row, dict) and row.get("role") == "assistant":
            return {
                "source": "trajectory.jsonl",
                "message_index": row.get("index"),
                "quote": _trim(row.get("content") or ""),
            }
    return None


def _first_tool_failure(audit: Dict[str, Any], trajectory: Any) -> Optional[Dict[str, Any]]:
    for call in audit.get("tool_calls") or []:
        if not isinstance(call, dict) or call.get("status") not in {"error", "blocked", "timeout"}:
            continue
        quote = call.get("result_preview") or call.get("args_preview") or call.get("message") or ""
        return {"source": "audit.json", "message_index": call.get("index"), "quote": _trim(quote)}
    if isinstance(trajectory, list):
        pattern = re.compile(r"(error|exception|modulenotfounderror|timeout|permission|blocked|失败|错误|缺失)", re.I)
        for row in trajectory:
            if isinstance(row, dict) and row.get("role") == "tool" and pattern.search(str(row.get("content") or "")):
                return {
                    "source": "trajectory.jsonl",
                    "message_index": row.get("index"),
                    "quote": _trim(row.get("content") or ""),
                }
    return None


def _quality_evidence(
    summary: Dict[str, Any],
    requested: List[str],
    tool_failure: Optional[Dict[str, Any]],
    final_message: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    evidence = []
    if requested:
        evidence.append(
            {
                "source": "summary.json",
                "quote": _trim("requested deliverables: %s; title: %s" % (", ".join(requested), summary.get("title") or "")),
            }
        )
    if tool_failure:
        evidence.append(tool_failure)
    if final_message:
        evidence.append(final_message)
    return evidence


def _trim(value: object, limit: int = 500) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _required(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError("missing %s" % path)
    return path


def _read_json(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("%s must contain a JSON object" % path)
    return data


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict):
            rows.append(item)
    return rows
