from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import EVIDENCE_SOURCE_ALIASES


def validate_judge_evidence(run_path: str | Path, report: dict[str, Any]) -> dict[str, Any]:
    artifacts = _load_artifacts(Path(run_path))
    evidence_rows = _collect_evidence(report)
    invalid = []
    valid_count = 0
    valid_by_location: dict[str, int] = {}

    for location, evidence in evidence_rows:
        result = _validate_one_evidence(evidence, artifacts)
        if result is None:
            valid_count += 1
            valid_by_location[location] = valid_by_location.get(location, 0) + 1
        else:
            invalid.append({"location": location, **evidence, "reason": result})

    fact_rows = _collect_fact_claims(report)
    unsupported = 0
    mismatch = 0
    for location, item in fact_rows:
        has_evidence = bool(item.get("evidence"))
        if not has_evidence:
            unsupported += 1
            mismatch += 1
            continue
        if valid_by_location.get(location, 0) == 0:
            mismatch += 1

    total = len(evidence_rows)
    notes = []
    if unsupported:
        notes.append("%s fact claims have no evidence" % unsupported)
    if invalid:
        notes.append("%s evidence references are invalid" % len(invalid))
    if mismatch:
        notes.append("%s fact claims have no valid evidence" % mismatch)

    return {
        "evidence_coverage": round(valid_count / total, 4) if total else 0,
        "unsupported_claim_count": unsupported,
        "invalid_evidence_count": len(invalid),
        "fact_inference_mismatch_count": mismatch,
        "valid_evidence_count": valid_count,
        "total_evidence_count": total,
        "notes": notes,
        "invalid_evidence": invalid,
    }


def _load_artifacts(run_dir: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for name in ("summary.json", "audit.json", "session.json"):
        path = run_dir / name
        if path.exists():
            artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
    trajectory_path = run_dir / "trajectory.jsonl"
    if trajectory_path.exists():
        artifacts["trajectory.jsonl"] = [
            json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    return artifacts


def _collect_evidence(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for index, finding in enumerate(report.get("findings") or []):
        if isinstance(finding, dict):
            for evidence in finding.get("evidence") or []:
                if isinstance(evidence, dict):
                    rows.append(("findings.%s" % index, evidence))
    for key, dimension in (report.get("dimensions") or {}).items():
        if isinstance(dimension, dict):
            for evidence in dimension.get("evidence") or []:
                if isinstance(evidence, dict):
                    rows.append(("dimensions.%s" % key, evidence))
    return rows


def _collect_fact_claims(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for index, finding in enumerate(report.get("findings") or []):
        if isinstance(finding, dict) and finding.get("type") == "fact":
            rows.append(("findings.%s" % index, finding))
    return rows


def _canonical_source(source: str) -> str:
    return EVIDENCE_SOURCE_ALIASES.get(str(source or "").strip().lower(), str(source or ""))


def _validate_one_evidence(evidence: dict[str, Any], artifacts: dict[str, Any]) -> str | None:
    source = _canonical_source(str(evidence.get("source") or ""))
    quote = str(evidence.get("quote") or "").strip()
    if not source:
        return "missing_source"
    if source not in artifacts:
        return "source_not_found"
    if not quote:
        return "missing_quote"
    artifact = artifacts[source]
    if source == "trajectory.jsonl" and evidence.get("message_index") is not None:
        row = _find_trajectory_row(artifact, int(evidence["message_index"]))
        if row is None:
            return "message_index_not_found"
        haystack = json.dumps(row, ensure_ascii=False, default=str)
    else:
        haystack = json.dumps(artifact, ensure_ascii=False, default=str)
    if quote not in haystack:
        return "quote_not_found"
    return None


def _find_trajectory_row(rows: Any, message_index: int) -> dict[str, Any] | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("index") == message_index:
            return row
    if 0 <= message_index < len(rows) and isinstance(rows[message_index], dict):
        return rows[message_index]
    return None
