from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .schemas import EVIDENCE_SOURCE_ALIASES


def validate_judge_evidence(run_path: str | Path, report: dict[str, Any]) -> dict[str, Any]:
    """Mechanically validate every evidence reference in a report.

    References are collected from findings, dimension top-level evidence and
    every dimension evidence chain (checks, counter evidence and score rules).
    Validation is verbatim-only: a quote must appear character-for-character
    in the referenced artifact. A valid quote proves the reference exists; it
    never proves the conclusion the judge attached to it.
    """
    artifacts = _load_artifacts(Path(run_path))
    evidence_rows = _collect_evidence(report)
    invalid = []
    index_corrections: list[dict[str, Any]] = []
    valid_count = 0
    valid_by_location: dict[str, int] = {}
    dimension_support_total: dict[str, int] = {}
    dimension_support_valid: dict[str, int] = {}

    for location, evidence, dimension_key, is_support in evidence_rows:
        result, corrected_index = _validate_one_evidence(evidence, artifacts)
        if result is None:
            valid_count += 1
            valid_by_location[location] = valid_by_location.get(location, 0) + 1
            if dimension_key and is_support:
                dimension_support_valid[dimension_key] = dimension_support_valid.get(dimension_key, 0) + 1
            if corrected_index is not None:
                index_corrections.append(
                    {
                        "location": location,
                        "message_index": evidence.get("message_index"),
                        "corrected_message_index": corrected_index,
                    }
                )
        else:
            invalid.append({"location": location, **evidence, "reason": result})
        if dimension_key and is_support:
            dimension_support_total[dimension_key] = dimension_support_total.get(dimension_key, 0) + 1

    fact_rows = _collect_fact_claims(report)
    unsupported = 0
    mismatch = 0
    mismatch_locations: list[str] = []
    for location, item in fact_rows:
        has_evidence = bool(item.get("evidence"))
        if not has_evidence:
            unsupported += 1
            mismatch += 1
            mismatch_locations.append(location)
            continue
        if valid_by_location.get(location, 0) == 0:
            mismatch += 1
            mismatch_locations.append(location)

    unsupported_dimensions: list[str] = []
    mismatch_dimensions: list[str] = []
    for key, dimension in (report.get("dimensions") or {}).items():
        if not isinstance(dimension, dict):
            continue
        # unknown is an honest "assessed but insufficient evidence" verdict and
        # source=missing is a structural gap handled by the publish gate;
        # neither is an unsupported conclusion.
        if str(dimension.get("status") or "unknown") == "unknown":
            continue
        if str(dimension.get("source") or "missing") == "missing":
            continue
        if dimension_support_total.get(key, 0) == 0:
            unsupported_dimensions.append(key)
        elif dimension_support_valid.get(key, 0) == 0:
            mismatch_dimensions.append(key)

    total = len(evidence_rows)
    notes = []
    if unsupported:
        notes.append("%s fact claims have no evidence" % unsupported)
    if invalid:
        notes.append("%s evidence references are invalid" % len(invalid))
    if mismatch:
        notes.append("%s fact claims have no valid evidence" % mismatch)
    if unsupported_dimensions:
        notes.append("%s scored dimensions have no evidence" % len(unsupported_dimensions))
    if mismatch_dimensions:
        notes.append("%s scored dimensions have no valid evidence" % len(mismatch_dimensions))
    if index_corrections:
        notes.append("%s evidence references had a drifted message_index (accepted verbatim)" % len(index_corrections))

    return {
        "evidence_coverage": round(valid_count / total, 4) if total else 0,
        "unsupported_claim_count": unsupported,
        "invalid_evidence_count": len(invalid),
        "index_corrected_count": len(index_corrections),
        "index_corrections": index_corrections,
        "fact_inference_mismatch_count": mismatch,
        "unsupported_dimension_count": len(unsupported_dimensions),
        "dimension_evidence_mismatch_count": len(mismatch_dimensions),
        "valid_evidence_count": valid_count,
        "total_evidence_count": total,
        "notes": notes,
        "invalid_evidence": invalid,
        "fact_findings_without_valid_evidence": mismatch_locations,
        "dimensions_without_valid_evidence": unsupported_dimensions + mismatch_dimensions,
    }


def _load_artifacts(run_dir: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    # Whitelist only: evidence sources may never resolve to arbitrary paths.
    for name in ("summary.json", "audit.json", "session.json", "expectations.json", "evaluation.json"):
        path = run_dir / name
        if not path.exists():
            continue
        try:
            artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # An unparseable artifact is unavailable as an evidence source;
            # references to it fail as source_not_found instead of crashing.
            continue
    trajectory_path = run_dir / "trajectory.jsonl"
    if trajectory_path.exists():
        artifacts["trajectory.jsonl"] = [
            json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    return artifacts


def _collect_evidence(report: dict[str, Any]) -> list[tuple[str, dict[str, Any], str | None, bool]]:
    """Collect ``(location, evidence, dimension_key, is_support)`` rows.

    ``is_support`` marks references that can back a dimension's conclusion
    (top-level evidence, check evidence, score-rule evidence); counter
    evidence rebuts a conclusion and never counts as support.
    """
    rows: list[tuple[str, dict[str, Any], str | None, bool]] = []
    for index, finding in enumerate(report.get("findings") or []):
        if isinstance(finding, dict):
            for evidence in finding.get("evidence") or []:
                if isinstance(evidence, dict):
                    rows.append(("findings.%s" % index, evidence, None, False))
    for key, dimension in (report.get("dimensions") or {}).items():
        if not isinstance(dimension, dict):
            continue
        for evidence in dimension.get("evidence") or []:
            if isinstance(evidence, dict):
                rows.append(("dimensions.%s" % key, evidence, key, True))
        chain = dimension.get("evidence_chain")
        if not isinstance(chain, dict):
            continue
        for check_index, check in enumerate(chain.get("checks") or []):
            if not isinstance(check, dict):
                continue
            location = "dimensions.%s.checks.%s" % (key, check_index)
            for evidence in check.get("evidence") or []:
                if isinstance(evidence, dict):
                    rows.append((location, evidence, key, True))
            for evidence in check.get("counter_evidence") or []:
                if isinstance(evidence, dict):
                    rows.append((location + ".counter", evidence, key, False))
        trace = chain.get("score_trace") or {}
        if not isinstance(trace, dict):
            continue
        for rule_index, rule in enumerate(trace.get("rules") or []):
            if not isinstance(rule, dict):
                continue
            for evidence in rule.get("evidence") or []:
                if isinstance(evidence, dict):
                    rows.append(("dimensions.%s.rules.%s" % (key, rule_index), evidence, key, True))
    return rows


def _collect_fact_claims(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for index, finding in enumerate(report.get("findings") or []):
        if isinstance(finding, dict) and finding.get("type") == "fact":
            rows.append(("findings.%s" % index, finding))
    return rows


def _canonical_source(source: str) -> str:
    return EVIDENCE_SOURCE_ALIASES.get(str(source or "").strip().lower(), str(source or ""))


def _validate_one_evidence(evidence: dict[str, Any], artifacts: dict[str, Any]) -> tuple[str | None, int | None]:
    """Validate one reference; return ``(failure_reason, corrected_index)``.

    ``corrected_index`` is set when the quote missed its declared
    ``message_index`` row but appears verbatim in another trajectory row:
    model row-number drift must not fail a real quote.
    """
    source = _canonical_source(str(evidence.get("source") or ""))
    quote = str(evidence.get("quote") or "").strip()
    if not source:
        return "missing_source", None
    if source not in artifacts:
        return "source_not_found", None
    if not quote:
        return "missing_quote", None
    artifact = artifacts[source]
    declared_index = evidence.get("message_index")
    if source == "trajectory.jsonl" and declared_index is not None:
        row = _find_trajectory_row(artifact, int(declared_index))
        if row is None:
            return "message_index_not_found", None
        target: Any = row
    else:
        target = artifact
    # 匹配阶梯见 _quote_in_target：JSON 序列化精确匹配 → 原始字符串匹配
    # （json.dumps 转义误伤）→ 空白折叠匹配（裁判把多行原文展平成单行）。
    # 只对真正非逐字的引用保持判负。
    if _quote_in_target(target, quote):
        return None, None
    # message_index 漂移容错：quote 未命中声明的行，但逐字存在于轨迹其它行
    # 时按有效计并记录校正行号（模型 off-by-one 行号漂移不应判负真实引用）。
    if source == "trajectory.jsonl" and declared_index is not None:
        corrected = _find_quote_row_index(artifact, quote)
        if corrected is not None:
            return None, corrected
    return "quote_not_found", None


def _find_quote_row_index(rows: Any, quote: str) -> int | None:
    """Return the ``index`` of the trajectory row containing ``quote``."""
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _quote_in_target(row, quote):
            index = row.get("index")
            return int(index) if isinstance(index, int) else None
    return None


def _quote_in_target(target: Any, quote: str) -> bool:
    """Match ``quote`` against ``target`` with the full tolerance ladder.

    Exact JSON-dump match, then raw-string match (json.dumps escaping),
    then whitespace-collapsed match (judge flattening multi-line output).
    """
    if quote in json.dumps(target, ensure_ascii=False, default=str):
        return True
    if any(quote in text for text in _iter_strings(target)):
        return True
    normalized_quote = _normalize_whitespace(quote)
    return bool(normalized_quote) and any(
        normalized_quote in _normalize_whitespace(text) for text in _iter_strings(target)
    )


def _iter_strings(value: Any):
    """Yield every string value contained in a JSON-like structure."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _normalize_whitespace(text: str) -> str:
    """Collapse every whitespace run to a single space (both ends trimmed)."""
    return re.sub(r"\s+", " ", text).strip()


def _find_trajectory_row(rows: Any, message_index: int) -> dict[str, Any] | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("index") == message_index:
            return row
    if 0 <= message_index < len(rows) and isinstance(rows[message_index], dict):
        return rows[message_index]
    return None
