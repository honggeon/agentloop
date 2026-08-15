from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import normalize_judge_report


def assess_dimensions(run_path: str | Path, report: dict[str, Any], reliability: dict[str, Any]) -> dict[str, dict[str, Any]]:
    run_dir = Path(run_path)
    normalized = normalize_judge_report(report)
    dimensions = {key: dict(value) for key, value in normalized["dimensions"].items()}
    audit = _read_json(run_dir / "audit.json")
    trajectory = _read_jsonl(run_dir / "trajectory.jsonl")

    _cap_dimension(dimensions, "tool_selection", *_tool_selection_cap(audit))
    _set_dimension(dimensions, "execution_closure", *_execution_closure_score(trajectory, normalized))
    _cap_dimension(dimensions, "error_recovery", *_error_recovery_score(audit, trajectory))
    _set_dimension(dimensions, "evidence_citation", *_evidence_citation_score(reliability))
    return dimensions


def _set_dimension(dimensions: dict[str, dict[str, Any]], key: str, score: int, status: str, summary: str, recommendation: str) -> None:
    current = dimensions.get(key) or {}
    current_source = current.get("source")
    unevaluated = current.get("status") == "unknown" or current_source in {"missing", None, ""}
    if not unevaluated:
        judge_score = int(current.get("score") or 0)
        if judge_score < score:
            return
    source = "deterministic" if current_source in {"missing", None, ""} else "deterministic+judge"
    dimensions[key] = {
        "score": score,
        "status": status,
        "summary": summary,
        "evidence": current.get("evidence") or [],
        "recommendation": recommendation,
        "source": source,
    }


def _cap_dimension(dimensions: dict[str, dict[str, Any]], key: str, score: int | None, status: str, summary: str, recommendation: str) -> None:
    if score is None:
        return
    current = dimensions.get(key) or {}
    current_source = current.get("source")
    unevaluated = current.get("status") == "unknown" or current_source in {"missing", None, ""}
    if unevaluated:
        dimensions[key] = {
            "score": score,
            "status": status,
            "summary": summary,
            "evidence": current.get("evidence") or [],
            "recommendation": recommendation,
            "source": "deterministic" if current_source in {"missing", None, ""} else "deterministic+judge",
        }
        return
    current_score = int(current.get("score") or 0)
    if current_score <= score:
        return
    dimensions[key] = {
        "score": score,
        "status": status,
        "summary": summary,
        "evidence": current.get("evidence") or [],
        "recommendation": recommendation,
        "source": "deterministic+judge" if current_source == "judge" else "deterministic",
    }


def _status(score: int) -> str:
    if score >= 80:
        return "pass"
    if score >= 50:
        return "partial"
    return "fail"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _tool_selection_cap(audit: dict[str, Any]) -> tuple[int | None, str, str, str]:
    calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    if not calls:
        return None, "unknown", "No tool calls were captured.", "Leave tool selection to the judge when no tools ran."
    failed = sum(1 for call in calls if str(call.get("status") or "").lower() in {"error", "failed", "missing"})
    if not failed:
        return None, "unknown", "Tool calls succeeded; selection quality is not determined by success rate.", "Keep judge evidence for whether the chosen tools were appropriate."
    score = round(100 * (1 - failed / len(calls)))
    return score, _status(score), "%s/%s tool calls failed." % (failed, len(calls)), "Investigate failed or unnecessary tool calls."


def _execution_closure_score(trajectory: list[dict[str, Any]], report: dict[str, Any]) -> tuple[int, str, str, str]:
    has_final = any(str(row.get("role") or "") == "assistant" and str(row.get("content") or "").strip() for row in trajectory[-3:])
    if report.get("overall_status") == "success" and has_final:
        return 90, "pass", "Run has a final assistant response and success status.", "Verify claimed deliverables when present."
    if has_final:
        return 65, "partial", "Run has a final assistant response but did not fully succeed.", "Tie final response to explicit completion checks."
    return 20, "fail", "No final assistant response was captured.", "Require final response before closing a run."


def _error_recovery_score(audit: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[int | None, str, str, str]:
    calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    failed = [call for call in calls if str(call.get("status") or "").lower() in {"error", "failed", "missing"}]
    if not failed:
        return None, "unknown", "No tool failures were captured.", "Error recovery is not applicable for this run."
    retry_text = json.dumps(trajectory, ensure_ascii=False).lower()
    recovered = "retry" in retry_text or "重试" in retry_text or len(calls) > len(failed)
    if recovered:
        return 60, "partial", "Failures were followed by retry or additional action.", "Make recovery outcome explicit in the final response."
    return 30, "fail", "Failures were captured without clear recovery.", "Add retry, fallback, or explicit blocked state handling."


def _evidence_citation_score(reliability: dict[str, Any]) -> tuple[int, str, str, str]:
    if reliability.get("total_evidence_count", 0) == 0:
        return 0, "fail", "No judge evidence was provided.", "Require evidence for findings and semantic dimension scores."
    if reliability.get("invalid_evidence_count", 0):
        return 0, "fail", "Invalid evidence references were found.", "Fix source, message_index, or quote references."
    score = round(float(reliability.get("evidence_coverage") or 0) * 100)
    return score, _status(score), "%s/%s evidence references are valid." % (reliability.get("valid_evidence_count", 0), reliability.get("total_evidence_count", 0)), "Maintain quote-level evidence validation."
