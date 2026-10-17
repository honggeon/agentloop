"""EvaluationEnvelope v1: the versioned ``evaluation.json`` platform envelope.

The envelope carries deterministic check results, expectation results,
artifact checks and the deterministic ``final_quality`` verdict for a run. It
never replaces ``agent_judge.json`` — judge reports are still written only by
``write_judge_report`` — and external evaluator results only ever land in
``external_evaluations`` for display.

Validation is strict: unknown enum values, missing fields, wrong types or
unparseable JSON make the envelope ``invalid``. An invalid envelope is kept on
disk for diagnosis but is never used for quality gating, and it never changes
the Run lifecycle status by itself.

Status enums (do not mix with the Run lifecycle or judge enums):

- ``evaluation.json.status``: ``pending|completed|failed|invalid``
- check statuses: ``pass|partial|fail|unknown``
- ``blocking_source``: ``rule_config|structured_audit|structured_expectation|none``
  (``blocking=true`` requires a non-``none`` source)
- ``final_quality.status``: ``success|partial|failed|unknown``
- ``score_cap`` / dimension-style ``score``: ``null`` or a 0–100 number; a
  missing judge proposal or missing evidence must yield ``null``, never a
  fabricated 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .trajectory import write_json

SCHEMA_VERSION = 1
EVALUATION_JSON = "evaluation.json"

ENVELOPE_STATUSES = ("pending", "completed", "failed", "invalid")
CHECK_STATUSES = ("pass", "partial", "fail", "unknown")
BLOCKING_SOURCES = ("rule_config", "structured_audit", "structured_expectation", "none")
FINAL_QUALITY_STATUSES = ("success", "partial", "failed", "unknown")

#: Deterministic check kinds derived from ``audit.json`` that are blocking by
#: default (design: dangerous tools, missing/orphan tool results, explicit
#: tool errors). Ordinary tool warnings stay non-blocking.
DEFAULT_BLOCKING_KINDS = frozenset(
    {"dangerous_tools", "missing_tool_results", "orphan_tool_results", "tool_errors"}
)

#: Score caps applied by deterministic failures; several caps combine by min.
CAP_MISSING_ARTIFACT = 40
CAP_BLOCKING_CHECK = 60
CAP_REQUIRED_EXPECTATION = 60


def new_envelope(status: str = "pending") -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "deterministic_checks": [],
        "expectation_results": [],
        "artifact_checks": [],
        "external_evaluations": [],
        "runtime_metrics": None,
        "final_quality": {"status": "unknown", "score_cap": None, "source": "none"},
    }


def make_check(
    check_id: str,
    kind: str,
    status: str,
    *,
    blocking: Optional[bool] = None,
    blocking_source: Optional[str] = None,
    evidence_refs: Optional[List[str]] = None,
    message: str = "",
) -> Dict[str, Any]:
    if blocking is None:
        blocking = status == "fail" and kind in DEFAULT_BLOCKING_KINDS
    if blocking_source is None:
        blocking_source = "structured_audit" if blocking else "none"
    return {
        "id": str(check_id),
        "kind": str(kind),
        "status": status,
        "blocking": bool(blocking),
        "blocking_source": blocking_source,
        "evidence_refs": list(evidence_refs or []),
        "message": str(message or ""),
    }


def make_expectation_result(
    expectation_id: str,
    *,
    required: bool,
    status: str,
    evidence_refs: Optional[List[str]] = None,
    message: str = "",
) -> Dict[str, Any]:
    return {
        "id": str(expectation_id),
        "required": bool(required),
        "status": status,
        "evidence_refs": list(evidence_refs or []),
        "message": str(message or ""),
    }


def make_artifact_check(
    path: str,
    *,
    required: bool,
    status: str,
    exists: bool,
    valid: bool,
    evidence_refs: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return {
        "path": str(path),
        "required": bool(required),
        "status": status,
        "exists": bool(exists),
        "valid": bool(valid),
        "evidence_refs": list(evidence_refs or []),
    }


def compute_final_quality(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Compute the deterministic final quality from validated envelope parts.

    Caps: missing required artifact -> 40, blocking check failure -> 60,
    required expectation failure -> 60; several caps combine by minimum.
    ``unknown`` results never turn into failures and never produce a cap.
    """
    caps: List[int] = []
    sources: List[str] = []
    for check in envelope.get("artifact_checks") or []:
        if check.get("required") and check.get("status") == "fail":
            caps.append(CAP_MISSING_ARTIFACT)
            sources.append("artifact:%s" % check.get("path"))
    for check in envelope.get("deterministic_checks") or []:
        if check.get("blocking") and check.get("status") == "fail":
            caps.append(CAP_BLOCKING_CHECK)
            sources.append("check:%s" % check.get("id"))
    for result in envelope.get("expectation_results") or []:
        if result.get("required") and result.get("status") == "fail":
            caps.append(CAP_REQUIRED_EXPECTATION)
            sources.append("expectation:%s" % result.get("id"))

    evaluated = [
        item.get("status")
        for item in (envelope.get("deterministic_checks") or [])
        + (envelope.get("expectation_results") or [])
        + (envelope.get("artifact_checks") or [])
    ]
    if caps:
        status = "failed"
    elif evaluated and any(status != "unknown" for status in evaluated):
        status = "success" if all(status == "pass" for status in evaluated if status != "unknown") else "partial"
    else:
        status = "unknown"
    return {
        "status": status,
        "score_cap": min(caps) if caps else None,
        "source": "deterministic" if caps or status != "unknown" else "none",
        "cap_sources": sources,
    }


def validate_envelope(payload: Any) -> List[str]:
    """Return a list of validation errors; empty means the envelope is usable."""
    errors: List[str] = []
    if not isinstance(payload, dict):
        return ["envelope must be a JSON object"]
    if payload.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version must be %s" % SCHEMA_VERSION)
    _enum(payload.get("status"), ENVELOPE_STATUSES, "status", errors)
    if payload.get("status") == "invalid":
        # An invalidated envelope only needs to be self-describing.
        return errors

    for index, check in enumerate(_list(payload.get("deterministic_checks"), "deterministic_checks", errors)):
        where = "deterministic_checks[%d]" % index
        if not isinstance(check, dict):
            errors.append("%s must be an object" % where)
            continue
        if not str(check.get("id") or ""):
            errors.append("%s.id is required" % where)
        if not str(check.get("kind") or ""):
            errors.append("%s.kind is required" % where)
        _enum(check.get("status"), CHECK_STATUSES, "%s.status" % where, errors)
        blocking = check.get("blocking")
        if not isinstance(blocking, bool):
            errors.append("%s.blocking must be a boolean" % where)
            blocking = False
        source = _enum(check.get("blocking_source"), BLOCKING_SOURCES, "%s.blocking_source" % where, errors)
        if blocking and source == "none":
            errors.append("%s.blocking=true requires a non-none blocking_source" % where)
        _string_list(check.get("evidence_refs"), "%s.evidence_refs" % where, errors)

    for index, result in enumerate(_list(payload.get("expectation_results"), "expectation_results", errors)):
        where = "expectation_results[%d]" % index
        if not isinstance(result, dict):
            errors.append("%s must be an object" % where)
            continue
        if not str(result.get("id") or ""):
            errors.append("%s.id is required" % where)
        if not isinstance(result.get("required"), bool):
            errors.append("%s.required must be a boolean" % where)
        _enum(result.get("status"), CHECK_STATUSES, "%s.status" % where, errors)
        _string_list(result.get("evidence_refs"), "%s.evidence_refs" % where, errors)

    for index, check in enumerate(_list(payload.get("artifact_checks"), "artifact_checks", errors)):
        where = "artifact_checks[%d]" % index
        if not isinstance(check, dict):
            errors.append("%s must be an object" % where)
            continue
        if not str(check.get("path") or ""):
            errors.append("%s.path is required" % where)
        if not isinstance(check.get("required"), bool):
            errors.append("%s.required must be a boolean" % where)
        _enum(check.get("status"), CHECK_STATUSES, "%s.status" % where, errors)
        for flag in ("exists", "valid"):
            if not isinstance(check.get(flag), bool):
                errors.append("%s.%s must be a boolean" % (where, flag))
        _string_list(check.get("evidence_refs"), "%s.evidence_refs" % where, errors)

    external = payload.get("external_evaluations") or []
    if not isinstance(external, list):
        errors.append("external_evaluations must be a list")

    final_quality = payload.get("final_quality")
    if not isinstance(final_quality, dict):
        errors.append("final_quality must be an object")
    else:
        _enum(final_quality.get("status"), FINAL_QUALITY_STATUSES, "final_quality.status", errors)
        _nullable_score(final_quality.get("score_cap"), "final_quality.score_cap", errors)
    return errors


def invalid_envelope(error: str) -> Dict[str, Any]:
    envelope = new_envelope("invalid")
    envelope["error"] = str(error or "invalid envelope")[:500]
    return envelope


def write_evaluation(run_dir: Path, envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and atomically write ``evaluation.json``.

    Invalid content is still written, but forced to ``status=invalid`` with a
    structured ``error`` so readers never gate on partial data.
    """
    errors = validate_envelope(envelope)
    if errors:
        envelope = invalid_envelope("; ".join(errors))
    write_json(Path(run_dir) / EVALUATION_JSON, envelope)
    return envelope


def load_evaluation(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Load ``evaluation.json``; ``None`` when absent (legacy run).

    A malformed or schema-violating file is returned as an ``invalid``
    envelope with a structured ``error`` — callers must not use it for gating.
    """
    path = Path(run_dir) / EVALUATION_JSON
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return invalid_envelope("evaluation.json is not parseable JSON: %s" % type(exc).__name__)
    errors = validate_envelope(payload)
    if errors:
        return invalid_envelope("; ".join(errors))
    return payload


def gating_envelope(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Return the envelope only when it may participate in quality gating."""
    envelope = load_evaluation(run_dir)
    if not envelope or envelope.get("status") != "completed":
        return None
    return envelope


def _enum(value: Any, allowed: tuple, field: str, errors: List[str]) -> Optional[str]:
    text = value if isinstance(value, str) else None
    if text not in allowed:
        errors.append("%s must be one of %s, got %r" % (field, list(allowed), value))
        return None
    return text


def _nullable_score(value: Any, field: str, errors: List[str]) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append("%s must be null or a 0-100 number" % field)
        return
    if not (0 <= value <= 100):
        errors.append("%s must be within 0-100" % field)


def _list(value: Any, field: str, errors: List[str]) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append("%s must be a list" % field)
        return []
    return value


def _string_list(value: Any, field: str, errors: List[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        errors.append("%s must be a list of strings" % field)
