from __future__ import annotations

import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

from .dimensions import assess_dimensions, verbatim_quote
from .evidence import validate_judge_evidence
from .schemas import DIMENSION_DEFINITIONS, missing_judge_dimensions, normalize_judge_report, render_judge_markdown
from ..judges import reports as judge_reports
from ..judges.proposals import JudgeProposalError
from ..judges.reports import publish_judge_report


def _runs_roots() -> List[Path]:
    roots = []
    env_root = os.getenv("AUDITRAIL_RUNS_DIR")
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
    """List exported Auditrail run directories under the given root."""
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
    """Load summary.json from an Auditrail run directory."""
    try:
        return _read_json(_required(_resolve_run_dir(run_path) / "summary.json"))
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}


def load_audit(run_path: str) -> Dict[str, Any]:
    """Load audit.json from an Auditrail run directory."""
    try:
        return _read_json(_required(_resolve_run_dir(run_path) / "audit.json"))
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}


def _int_or(value: Any, default: int) -> int:
    """Coerce LLM-supplied numeric args; models routinely pass null for optional parameters."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def load_trajectory_slice(run_path: str, offset: int = 0, limit: int = 50) -> Any:
    """Load a bounded slice of trajectory.jsonl rows from an Auditrail run directory."""
    try:
        rows = _read_jsonl(_required(_resolve_run_dir(run_path) / "trajectory.jsonl"))
    except (FileNotFoundError, ValueError) as exc:
        return {"error": str(exc)}
    start = max(0, _int_or(offset, 0))
    stop = start + max(0, _int_or(limit, 50))
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
    """Assess completion, deliverables, and evidence chain from run artifacts.

    Deterministic quality, findings and ``score_cap`` are computed here only.
    When a valid ``evaluation.json`` (EvaluationEnvelope v1) exists, its
    blocking checks, required expectation failures and required artifact
    failures add caps (60/60/40 respectively; several caps combine by
    minimum). Runs without an envelope keep the legacy summary/audit/
    trajectory inference path unchanged; an ``invalid`` envelope is reported
    but never used for gating.
    """
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

    # Inferred deliverable requests come from regexing user text; the agent may
    # have written the file in its own workspace (the common case for imported
    # DSH runs), which this gate must not scan — it is untrusted, mutable, and
    # usually out of reach. Absence from the run bundle therefore means
    # "unverifiable", not "missing": no cap, no hard failure. Only an
    # authoritative source (evaluation.json artifact checks) may declare a
    # deliverable missing and cap at 40.
    if missing:
        deliverable_verification = "unverifiable"
        findings.append(
            {
                "severity": "medium",
                "category": "delivery",
                "title": "Requested deliverable cannot be verified from the run bundle",
                "type": "fact",
                "evidence": evidence_chain,
                "recommendation": "Verify the deliverable in the agent workspace, or archive it into the run bundle for deterministic verification.",
            }
        )
    elif requested:
        deliverable_verification = "verified"
        status = "success"
    else:
        deliverable_verification = "none"

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

    envelope_status, envelope_note, envelope_caps, envelope_findings = _envelope_quality(run_dir)
    for cap in envelope_caps:
        score_cap = min(score_cap, cap)
    if envelope_caps:
        status = "failed"
    findings.extend(envelope_findings)

    return {
        "completion": {"status": status, "score_cap": score_cap},
        "deliverables": {
            "requested": requested,
            "produced": produced,
            "missing": missing,
            "verification": deliverable_verification,
        },
        "evidence_chain": evidence_chain,
        "findings": findings,
        "evaluation": {"status": envelope_status, "note": envelope_note},
    }


def _envelope_quality(run_dir: Path) -> "tuple[str, str, List[int], List[Dict[str, Any]]]":
    """Read evaluation.json and derive caps/findings; never gate on invalid data."""
    from ..evaluation import (
        CAP_BLOCKING_CHECK,
        CAP_MISSING_ARTIFACT,
        CAP_REQUIRED_EXPECTATION,
        load_evaluation,
    )

    envelope = load_evaluation(run_dir)
    if envelope is None:
        return "missing", "", [], []
    if envelope.get("status") != "completed":
        note = "evaluation.json is %s: %s" % (envelope.get("status"), envelope.get("error") or "not usable for gating")
        return str(envelope.get("status") or "unknown"), note, [], []

    caps: List[int] = []
    findings: List[Dict[str, Any]] = []
    for check in envelope.get("deterministic_checks") or []:
        if check.get("blocking") and check.get("status") == "fail":
            caps.append(CAP_BLOCKING_CHECK)
            findings.append(
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "Blocking deterministic check failed: %s" % check.get("id"),
                    "type": "fact",
                    "evidence": [{"source": name, "quote": str(check.get("message") or check.get("kind") or "")} for name in (check.get("evidence_refs") or ["audit.json"])],
                    "recommendation": "Resolve the blocking audit finding before trusting the run.",
                }
            )
    for result in envelope.get("expectation_results") or []:
        if result.get("required") and result.get("status") == "fail":
            caps.append(CAP_REQUIRED_EXPECTATION)
            findings.append(
                {
                    "severity": "high",
                    "category": "completion",
                    "title": "Required expectation failed: %s" % result.get("id"),
                    "type": "fact",
                    "evidence": [{"source": name, "quote": str(result.get("message") or result.get("id") or "")} for name in (result.get("evidence_refs") or ["evaluation.json"])],
                    "recommendation": "Treat required expectations as hard gates; an unknown result is not a failure.",
                }
            )
    for check in envelope.get("artifact_checks") or []:
        if check.get("required") and check.get("status") == "fail":
            caps.append(CAP_MISSING_ARTIFACT)
            findings.append(
                {
                    "severity": "critical",
                    "category": "delivery",
                    "title": "Required artifact missing: %s" % check.get("path"),
                    "type": "fact",
                    # The quote must be verbatim content of evaluation.json:
                    # the artifact path itself, never a synthesized sentence.
                    "evidence": [{"source": "evaluation.json", "quote": _envelope_quote(str(check.get("path") or ""))}],
                    "recommendation": "Produce the required artifact before reporting success.",
                }
            )
    return "completed", "", caps, findings


def _envelope_quote(value: str) -> str:
    """Return a quote that survives verbatim JSON matching, or a safe marker."""
    text = str(value or "").strip()
    if text and not re.search(r'["\\\x00-\x1f]', text):
        return text[:500]
    return '"status": "fail"'


def _downgrade_unsupported_facts(normalized: Dict[str, Any], reliability: Dict[str, Any]) -> int:
    """Enforce the evidence contract: a fact finding whose evidence cannot be
    traced back to the run artifacts is an inference, not a fact."""
    findings = normalized.get("findings") or []
    count = 0
    for location in reliability.get("fact_findings_without_valid_evidence") or []:
        if not isinstance(location, str) or not location.startswith("findings."):
            continue
        try:
            index = int(location.split(".", 1)[1])
        except ValueError:
            continue
        if 0 <= index < len(findings) and findings[index].get("type") == "fact":
            findings[index]["type"] = "inference"
            count += 1
    return count


def _downgrade_unsupported_dimensions(normalized: Dict[str, Any], reliability: Dict[str, Any]) -> List[str]:
    """Normalize judge-scored dimensions without valid evidence to unknown.

    A judge that reports pass/partial/fail with a score but no mechanically
    valid evidence has not produced a trustworthy verdict: the dimension is
    rewritten to ``score=null, status=unknown, source=judge`` (never 0/fail,
    which would fabricate a negative evaluation), and the gap is recorded in
    the dimension's evidence chain. Deterministic dimensions are exempt — they
    generate their own verbatim evidence when they assign a score.
    """
    dimensions = normalized.get("dimensions") or {}
    downgraded: List[str] = []
    for key in reliability.get("dimensions_without_valid_evidence") or []:
        dimension = dimensions.get(key)
        if not isinstance(dimension, dict):
            continue
        if dimension.get("source") != "judge" or dimension.get("status") == "unknown":
            continue
        dimension["score"] = None
        dimension["status"] = "unknown"
        chain = dict(dimension.get("evidence_chain") or {})
        gaps = list(chain.get("gaps") or [])
        gaps.append("裁判给出了分数但缺少有效证据支撑，已归一化为 unknown（score=null）。")
        chain["gaps"] = gaps
        dimension["evidence_chain"] = chain
        downgraded.append(key)
    return downgraded


def write_judge_report(
    run_path: str,
    report: object,
    *,
    judge_metadata: Optional[Dict[str, Any]] = None,
    judge_engine: Optional[str] = None,
    published_at: Optional[str] = None,
) -> Dict[str, str]:
    """Normalize and write agent_judge.json, agent_judge.md, and chart_data.json.

    ``judge_metadata`` records which engine produced the proposal (engine id,
    runtime, model, profile). It is additive provenance only: normalization,
    evidence validation, deterministic caps, and quality assessment below are
    the same for every engine.

    ``judge_engine`` additionally archives the same normalized report under
    ``judges/<engine>/`` and records it in ``judges/manifest.json``. The root
    files stay the active, backward-compatible report. When it is omitted the
    engine is taken from the report's own ``judge.engine`` metadata, so a run
    can hold one report per judge engine without a second normalization path.
    """
    run_dir = _resolve_run_dir(run_path)
    normalized = normalize_judge_report(report)
    if judge_metadata:
        existing = dict(normalized.get("judge") or {})
        merged_metadata = {key: value for key, value in judge_metadata.items() if value is not None}
        existing.update(merged_metadata)
        normalized["judge"] = existing
        normalized = normalize_judge_report(normalized)
    # Publish gate, before any file is written and before assess_dimensions
    # could mask gaps with deterministic scores: the judge must have submitted
    # every dimension itself. source=missing is a structural gap (the proposal
    # omitted the dimension), not an honest "insufficient evidence" verdict
    # (status=unknown, score=null, source=judge), which stays publishable.
    missing_dimensions = missing_judge_dimensions(normalized)
    if missing_dimensions:
        raise JudgeProposalError(
            "judge proposal is structurally incomplete: %s dimension(s) were not submitted by the judge: %s"
            % (len(missing_dimensions), ", ".join(missing_dimensions))
        )
    # Stage 1 reliability covers the raw judge proposal only; it drives the
    # deterministic evidence_citation scoring below.
    base_reliability = validate_judge_evidence(str(run_dir), normalized)
    # Quality findings are merged BEFORE the final evidence validation so they
    # can never bypass it, and deterministic dimensions run before it so their
    # generated evidence chains are part of the validated evidence set.
    quality = assess_run_quality(str(run_dir))
    normalized = _merge_quality_report(normalized, quality)
    normalized["dimensions"] = assess_dimensions(str(run_dir), normalized, base_reliability)
    normalized = normalize_judge_report(normalized)
    # Stage 2 reliability is computed over the complete publishable report
    # (judge + quality findings + deterministic dimension chains) and is what
    # gets persisted.
    reliability = validate_judge_evidence(str(run_dir), normalized)
    existing_notes = list((normalized.get("judge_reliability") or {}).get("notes") or [])
    merged_notes = existing_notes + [note for note in reliability.get("notes") or [] if note not in existing_notes]
    downgraded = _downgrade_unsupported_facts(normalized, reliability)
    if downgraded:
        merged_notes.append(
            "downgraded %s fact findings to inference: no valid evidence in run artifacts" % downgraded
        )
    downgraded_dimensions = _downgrade_unsupported_dimensions(normalized, reliability)
    if downgraded_dimensions:
        merged_notes.append(
            "normalized %s scored dimension(s) to unknown: no valid evidence (%s)"
            % (len(downgraded_dimensions), ", ".join(downgraded_dimensions))
        )
    reliability["notes"] = merged_notes
    normalized["judge_reliability"] = reliability
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
        "unsupported_dimension_count": normalized["judge_reliability"].get("unsupported_dimension_count", 0),
        "dimension_evidence_mismatch_count": normalized["judge_reliability"].get("dimension_evidence_mismatch_count", 0),
    }
    normalized["charts"] = charts

    engine_id = judge_engine or (normalized.get("judge") or {}).get("engine")
    markdown = render_judge_markdown(normalized)
    if engine_id:
        pub = publish_judge_report(
            run_dir,
            normalized,
            engine=engine_id,
            markdown=markdown,
            charts=normalized["charts"],
            published_at=published_at,
        )
        return {
            "json_path": pub["json_path"],
            "markdown_path": pub["markdown_path"],
            "chart_path": pub["chart_path"],
        }

    # No engine provenance: keep the single-report layout, still atomically.
    root_files = judge_reports.REPORT_FILENAMES
    judge_reports.replace_report_file(run_dir / root_files["json_path"], json.dumps(normalized, ensure_ascii=False, indent=2))
    judge_reports.replace_report_file(run_dir / root_files["markdown_path"], markdown)
    judge_reports.replace_report_file(run_dir / root_files["chart_path"], json.dumps(normalized["charts"], ensure_ascii=False, indent=2))
    return {
        "json_path": str(run_dir / root_files["json_path"]),
        "markdown_path": str(run_dir / root_files["markdown_path"]),
        "chart_path": str(run_dir / root_files["chart_path"]),
    }


def _count_by(items: List[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    counts = Counter(str(item.get(key) or "") for item in items if isinstance(item, dict) and item.get(key))
    return [{key: name, "count": count} for name, count in sorted(counts.items())]


def _merge_quality_report(report: Dict[str, Any], quality: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(report)
    completion = quality.get("completion") or {}
    score_cap = _int_or(completion.get("score_cap"), 100)
    proposed_score = merged.get("completion_score")
    # The cap only constrains a numeric proposal score; with no proposal (null)
    # there is nothing to cap and no score is fabricated.
    if isinstance(proposed_score, (int, float)) and not isinstance(proposed_score, bool):
        if score_cap < int(proposed_score):
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


# Harnesses inject skill catalogs and runtime context into user-role rows as
# <system-reminder> blocks; those mentions (".pptx", "markdown", ...) are not
# the user's request and must not create deliverable expectations.
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.S)

# Auditrail's own run artifacts. ``_produced_deliverables`` scans the run
# directory; without this exclusion every run would "produce" .md/.json
# deliverables simply because auditrail wrote its reports there. Per-engine
# judge archives live under ``judges/`` and are run artifacts too.
_RUN_ARTIFACT_NAMES = {
    "session.json",
    "summary.json",
    "trajectory.jsonl",
    "audit.json",
    "tool_audit.md",
    "source.json",
    "expectations.json",
    "llm_calls.json",
    "agent_judge.json",
    "agent_judge.md",
    "chart_data.json",
}
_RUN_ARTIFACT_DIRS = {"judges"}


def _requested_deliverables(summary: Dict[str, Any], trajectory: Any) -> List[str]:
    text_parts = [str(summary.get("title") or "")]
    for row in trajectory if isinstance(trajectory, list) else []:
        if isinstance(row, dict) and row.get("role") == "user":
            text_parts.append(str(row.get("content") or ""))
    text = _SYSTEM_REMINDER_RE.sub(" ", "\n".join(text_parts)).lower()
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
        if not path.is_file() or path.name in _RUN_ARTIFACT_NAMES:
            continue
        relative_parts = path.relative_to(run_dir).parts[:-1]
        if any(part in _RUN_ARTIFACT_DIRS for part in relative_parts):
            continue
        if path.suffix.lower() in requested:
            produced.add(path.suffix.lower())
    return sorted(produced)


def _last_assistant_message(trajectory: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(trajectory, list):
        return None
    for row in reversed(trajectory):
        if isinstance(row, dict) and row.get("role") == "assistant":
            quote = verbatim_quote(row.get("content"), json.dumps(row, ensure_ascii=False, default=str))
            if not quote:
                return None
            return {
                "source": "trajectory.jsonl",
                "message_index": row.get("index"),
                "quote": quote,
            }
    return None


def _first_tool_failure(audit: Dict[str, Any], trajectory: Any) -> Optional[Dict[str, Any]]:
    """Return the first structured tool failure, or None.

    Only the deterministic audit's structured status is trusted. Scanning
    tool-result text for error keywords fabricated failures out of ordinary
    content — e.g. the MIT LICENSE text "Permission is hereby granted"
    matched a bare ``permission`` pattern and was reported as a tool failure.
    Shell non-zero exits are a separate semantic, handled by the command-level
    channel rather than keyword guessing.
    """
    audit_dump = json.dumps(audit, ensure_ascii=False, default=str)
    for call in audit.get("tool_calls") or []:
        if not isinstance(call, dict) or call.get("status") not in {"error", "blocked", "timeout"}:
            continue
        quote = verbatim_quote(call.get("result_preview") or call.get("args_preview") or call.get("message"), audit_dump)
        if not quote:
            quote = '"status": "%s"' % call.get("status")
        return {"source": "audit.json", "message_index": call.get("index"), "quote": quote}
    return None


def _quality_evidence(
    summary: Dict[str, Any],
    requested: List[str],
    tool_failure: Optional[Dict[str, Any]],
    final_message: Optional[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    evidence = []
    if requested:
        # Quote the session title verbatim — a synthesized sentence like
        # "requested deliverables: ..." is not artifact content and would fail
        # the mechanical quote validation.
        title = str(summary.get("title") or "").strip()
        if title:
            quote = verbatim_quote(title, json.dumps(summary, ensure_ascii=False, default=str), limit=500)
            if quote:
                evidence.append({"source": "summary.json", "quote": quote})
    if tool_failure:
        evidence.append(tool_failure)
    if final_message:
        evidence.append(final_message)
    return evidence


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
