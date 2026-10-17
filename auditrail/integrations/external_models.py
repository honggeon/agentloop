"""Unified external evaluation / external trace evidence model (Phase 3).

External systems (Langfuse-style observability exports, promptfoo reports,
human feedback feeds) produce *additional* evidence only. The records defined
here never become quality authority:

- they live in a dedicated store under ``platform/external/`` — formal run
  artifacts (``evaluation.json``, ``audit.json``, ``agent_judge.json``) are
  never modified by an external import;
- they never change deterministic checks, judge ``overall_status`` or
  ``score_cap``;
- their scores carry an explicit ``score_min``/``score_max`` range and are
  **never** reinterpreted as a 0–100 ``completion_score``;
- association with platform runs/agents is explicit and carries a confidence
  (``exact``/``strong``/``weak``/``unlinked``); traces that cannot be linked
  are kept as ``unlinked_external_trace`` records, never dropped.

Storage (file based, atomic rewrites, no database)::

    platform/
      external/
        evaluations.jsonl      # ExternalEvaluation records
        traces.jsonl           # ExternalTrace records (linked + unlinked)
        imports/<import_id>.json

Idempotency: ``(source_system, source_id)`` identifies one external record.
Re-importing the identical record is a duplicate; importing a *different*
record under the same key keeps the original and appends a warning.

This module is core logic: it never imports ``backend`` and needs no model
credentials or external SDKs.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..costs import redact_metadata
from ..run_contract import utc_now
from ..trajectory import write_json, write_text

SCHEMA_VERSION = 1

EXTERNAL_DIR = "external"
EVALUATIONS_FILE = "evaluations.jsonl"
TRACES_FILE = "traces.jsonl"
IMPORTS_DIR = "imports"

#: Allowed per-record import states.
IMPORT_STATUSES = ("pending", "imported", "partial", "failed", "unlinked", "skipped")

#: Allowed association confidence values (ordered strongest to weakest).
LINK_CONFIDENCES = ("exact", "strong", "weak", "unlinked")

#: External evaluation row-level status (the external system's own verdict,
#: never mapped onto internal judge/dimension statuses).
EXTERNAL_EVAL_STATUSES = ("pass", "partial", "fail", "unknown")

#: Import safety limits (aligned with the cost import pipeline).
MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_IMPORT_LINES = 100_000
MAX_LINE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 10
MAX_FIELD_LENGTH = 256
MAX_COMMENT_LENGTH = 500
MAX_IMPORT_ERRORS = 50
MAX_GENERATIONS = 200

#: Text fragments that mark content which must never be persisted in an
#: external record comment (full prompts, completions, chain-of-thought).
_COMMENT_FORBIDDEN_FRAGMENTS = (
    "chain-of-thought",
    "chain_of_thought",
    "<thinking",
    "system prompt:",
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _opt_float(value: Any) -> Optional[float]:
    if not _is_number(value):
        return None
    return round(float(value), 8)


def _opt_int(value: Any) -> Optional[int]:
    if not _is_number(value):
        return None
    return max(int(value), 0)


def _opt_str(value: Any, *, limit: int = MAX_FIELD_LENGTH) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def _json_depth(value: Any, depth: int = 0) -> int:
    if isinstance(value, dict):
        return max([depth] + [_json_depth(item, depth + 1) for item in value.values()])
    if isinstance(value, list):
        return max([depth] + [_json_depth(item, depth + 1) for item in value])
    return depth


def _sanitize_comment(raw: Any) -> Optional[str]:
    """Keep a bounded, non-sensitive comment; drop anything that looks like a
    full prompt, completion or chain-of-thought."""
    if not isinstance(raw, str):
        return None
    text = " ".join(raw.split())
    if not text:
        return None
    lowered = text.lower()
    if any(fragment in lowered for fragment in _COMMENT_FORBIDDEN_FRAGMENTS):
        return None
    if len(text) > MAX_COMMENT_LENGTH * 4:
        # Overly long free text risks carrying whole prompts; keep a short
        # excerpt only.
        text = text[: MAX_COMMENT_LENGTH * 4]
    return text[:MAX_COMMENT_LENGTH]


def external_id_for(kind: str, source_system: str, source_id: str) -> str:
    """Deterministic external record id: re-importing the same external object
    targets the same id, which makes imports idempotent."""
    text = "%s:%s" % (source_system, source_id)
    return "%s-%s" % (kind, hashlib.sha256(text.encode("utf-8")).hexdigest()[:16])


# ---------------------------------------------------------------------------
# ExternalEvaluation
# ---------------------------------------------------------------------------


def normalize_external_evaluation(
    raw: Any,
    *,
    source_system: str,
    imported_at: Optional[str] = None,
    default_run_id: Optional[str] = None,
    default_agent_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and normalize one external evaluation record. Raises
    ``ValueError`` on unusable input; missing optional fields become ``None``
    (``unknown``), never fabricated values.
    """
    if not isinstance(raw, dict):
        raise ValueError("external evaluation must be an object")
    if _json_depth(raw) > MAX_JSON_DEPTH:
        raise ValueError("external evaluation exceeds the maximum nesting depth")
    source_system = str(source_system or "").strip()[:64]
    if not source_system:
        raise ValueError("source_system is required")

    source_id = _opt_str(raw.get("source_id") or raw.get("id"), limit=128)
    if not source_id:
        raise ValueError("external evaluation is missing source_id")

    run_id = _opt_str(raw.get("run_id"), limit=128) or default_run_id
    agent_id = _opt_str(raw.get("agent_id"), limit=128) or default_agent_id

    status = _opt_str(raw.get("status"), limit=32)
    if status is not None and status not in EXTERNAL_EVAL_STATUSES:
        status = "unknown"

    score = _opt_float(raw.get("score") if raw.get("score") is not None else raw.get("value"))
    score_min = _opt_float(raw.get("score_min"))
    score_max = _opt_float(raw.get("score_max"))
    if score is not None and score_min is not None and score_max is not None:
        if not (score_min <= score <= score_max):
            # Out-of-range scores are suspicious; keep the value but flag via
            # unknown status rather than clamping silently.
            status = "unknown"

    provenance_raw = raw.get("provenance") if isinstance(raw.get("provenance"), dict) else {}
    confidence = _opt_str(provenance_raw.get("confidence"), limit=16)
    if confidence not in LINK_CONFIDENCES:
        confidence = "exact" if run_id else "unlinked"

    now = imported_at or utc_now()
    record = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_id": external_id_for("ext-eval", source_system, source_id),
        "source_system": source_system,
        "source_id": source_id,
        "source_version": _opt_str(raw.get("source_version") or raw.get("version"), limit=64),
        "run_id": run_id,
        "agent_id": agent_id,
        "suite_id": _opt_str(raw.get("suite_id"), limit=128),
        "case_id": _opt_str(raw.get("case_id"), limit=128),
        "metric_name": _opt_str(raw.get("metric_name") or raw.get("name"), limit=128) or "unknown",
        "status": status or "unknown",
        "score": score,
        "score_min": score_min,
        "score_max": score_max,
        "label": _opt_str(raw.get("label"), limit=128),
        "comment": _sanitize_comment(raw.get("comment")),
        "provenance": {
            "source_system": source_system,
            "source_id": source_id,
            "imported_at": _opt_str(provenance_raw.get("imported_at"), limit=64) or now,
            "association": _opt_str(provenance_raw.get("association"), limit=64)
            or ("run_id" if run_id else "none"),
            "confidence": confidence,
        },
        "metadata": redact_metadata(raw.get("metadata")),
        "warnings": [],
    }
    if run_id is None and confidence != "unlinked":
        record["provenance"]["confidence"] = "unlinked"
        record["warnings"].append("evaluation has no run association; marked unlinked")
    return record


# ---------------------------------------------------------------------------
# ExternalTrace
# ---------------------------------------------------------------------------


def normalize_external_trace(
    raw: Any,
    *,
    source_system: str,
    imported_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and normalize one external trace record. Raises ``ValueError``.

    The trace id from the external system is stored as ``source_id`` only —
    it is never presented as a platform ``run_id``.
    """
    if not isinstance(raw, dict):
        raise ValueError("external trace must be an object")
    if _json_depth(raw) > MAX_JSON_DEPTH:
        raise ValueError("external trace exceeds the maximum nesting depth")
    source_system = str(source_system or "").strip()[:64]
    if not source_system:
        raise ValueError("source_system is required")

    source_id = _opt_str(raw.get("source_id") or raw.get("trace_id") or raw.get("id"), limit=128)
    if not source_id:
        raise ValueError("external trace is missing source_id")

    tokens_raw = raw.get("tokens") if isinstance(raw.get("tokens"), dict) else {}
    cost_raw = raw.get("cost") if isinstance(raw.get("cost"), dict) else {}
    link_raw = raw.get("link") if isinstance(raw.get("link"), dict) else {}

    run_id = _opt_str(raw.get("run_id"), limit=128)
    agent_id = _opt_str(raw.get("agent_id"), limit=128)
    confidence = _opt_str(link_raw.get("confidence"), limit=16)
    if confidence not in LINK_CONFIDENCES:
        confidence = "exact" if run_id else "unlinked"
    if run_id is None:
        confidence = "unlinked"

    cost_value = _opt_float(cost_raw.get("value"))
    cost_source = _opt_str(cost_raw.get("source"), limit=16)
    if cost_value is None:
        cost_source = "missing"
    elif cost_source not in ("observed", "imported", "estimated"):
        cost_source = "imported"

    generations = raw.get("generations")
    if not isinstance(generations, list):
        generations = []
    generations = generations[:MAX_GENERATIONS]

    record = {
        "schema_version": SCHEMA_VERSION,
        "trace_id": external_id_for("ext-trace", source_system, source_id),
        "source_system": source_system,
        "source_id": source_id,
        "run_id": run_id,
        "agent_id": agent_id,
        "name": _opt_str(raw.get("name"), limit=128),
        "started_at": _opt_str(raw.get("started_at"), limit=64),
        "ended_at": _opt_str(raw.get("ended_at"), limit=64),
        "latency_ms": _opt_int(raw.get("latency_ms")),
        "status": _opt_str(raw.get("status"), limit=32) or "unknown",
        "error": _sanitize_comment(raw.get("error")),
        "generations": generations,
        "tokens": {
            "input": _opt_int(tokens_raw.get("input")),
            "output": _opt_int(tokens_raw.get("output")),
            "total": _opt_int(tokens_raw.get("total")),
        },
        "cost": {
            "value": cost_value,
            "currency": _opt_str(cost_raw.get("currency"), limit=8) or ("USD" if cost_value is not None else None),
            "source": cost_source,
        },
        "link": {
            "method": _opt_str(link_raw.get("method"), limit=64) or ("run_id" if run_id else "none"),
            "confidence": confidence,
            "run_ids": [run_id] if run_id else [],
        },
        "import_status": "unlinked" if run_id is None else "imported",
        "imported_at": imported_at or utc_now(),
        "warnings": [],
        "metadata": redact_metadata(raw.get("metadata")),
    }
    return record


def mark_trace_unlinked(trace: Dict[str, Any], *, reason: Optional[str] = None) -> Dict[str, Any]:
    """Mark a trace as ``unlinked_external_trace`` evidence (never dropped)."""
    trace["run_id"] = None
    trace["link"] = {"method": "none", "confidence": "unlinked", "run_ids": []}
    trace["import_status"] = "unlinked"
    if reason:
        warnings = trace.setdefault("warnings", [])
        if len(warnings) < 10:
            warnings.append(str(reason)[:200])
    return trace


# ---------------------------------------------------------------------------
# persistent store (atomic JSONL rewrites)
# ---------------------------------------------------------------------------


def _external_dir(platform_dir: Path) -> Path:
    return Path(platform_dir) / EXTERNAL_DIR


def _evaluations_path(platform_dir: Path) -> Path:
    return _external_dir(platform_dir) / EVALUATIONS_FILE


def _traces_path(platform_dir: Path) -> Path:
    return _external_dir(platform_dir) / TRACES_FILE


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    records: List[Dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records


def _save_jsonl(path: Path, records: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=False, default=str) for record in records]
    write_text(path, "".join(line + "\n" for line in lines))


def load_external_evaluations(platform_dir: Path) -> List[Dict[str, Any]]:
    return _load_jsonl(_evaluations_path(platform_dir))


def load_external_traces(platform_dir: Path) -> List[Dict[str, Any]]:
    return _load_jsonl(_traces_path(platform_dir))


def save_external_evaluations(platform_dir: Path, records: List[Dict[str, Any]]) -> None:
    _save_jsonl(_evaluations_path(platform_dir), records)


def save_external_traces(platform_dir: Path, records: List[Dict[str, Any]]) -> None:
    _save_jsonl(_traces_path(platform_dir), records)


def merge_external_records(
    existing: List[Dict[str, Any]],
    incoming: List[Dict[str, Any]],
    *,
    id_field: str,
) -> Tuple[List[Dict[str, Any]], int, List[str]]:
    """Merge ``incoming`` into ``existing`` idempotently.

    Keyed by ``(source_system, source_id)``; an identical re-import is a
    duplicate (skipped), a conflicting re-import keeps the original record and
    appends a bounded warning. Returns ``(merged, duplicate_count, warnings)``.
    """
    by_key: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for record in existing:
        key = (str(record.get("source_system") or ""), str(record.get("source_id") or ""))
        by_key[key] = record
    duplicates = 0
    warnings: List[str] = []
    for record in incoming:
        key = (str(record.get("source_system") or ""), str(record.get("source_id") or ""))
        current = by_key.get(key)
        if current is None:
            by_key[key] = record
            continue
        comparable_current = {k: v for k, v in current.items() if k != "imported_at"}
        comparable_new = {k: v for k, v in record.items() if k != "imported_at"}
        if comparable_current == comparable_new:
            duplicates += 1
            continue
        if len(warnings) < MAX_IMPORT_ERRORS:
            warnings.append(
                "conflicting %s %s:%s re-import kept the original record"
                % (id_field, key[0] or "unknown", key[1] or "unknown")
            )
        duplicates += 1
    return list(by_key.values()), duplicates, warnings


# ---------------------------------------------------------------------------
# import reports
# ---------------------------------------------------------------------------


def new_import_report(source_system: str, *, dry_run: bool = False) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "import_id": "import-%s" % uuid.uuid4().hex[:12],
        "source_system": str(source_system or "")[:64],
        "status": "pending",
        "dry_run": bool(dry_run),
        "total_records": 0,
        "imported_records": 0,
        "duplicate_records": 0,
        "unlinked_records": 0,
        "invalid_records": 0,
        "skipped_records": 0,
        "warnings": [],
        "errors": [],
        "imported_at": utc_now(),
    }


def record_import_error(report: Dict[str, Any], line_no: Optional[int], message: str) -> None:
    report["invalid_records"] = int(report.get("invalid_records") or 0) + 1
    errors = report.setdefault("errors", [])
    if len(errors) < MAX_IMPORT_ERRORS:
        entry: Dict[str, Any] = {"error": str(message)[:200]}
        if line_no is not None:
            entry["line"] = int(line_no)
        errors.append(entry)


def record_import_warning(report: Dict[str, Any], message: str) -> None:
    warnings = report.setdefault("warnings", [])
    if len(warnings) < MAX_IMPORT_ERRORS:
        warnings.append(str(message)[:200])


def finalize_import_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Derive the final import status from the counters."""
    if report.get("dry_run"):
        report["status"] = "dry_run"
        return report
    imported = int(report.get("imported_records") or 0)
    invalid = int(report.get("invalid_records") or 0)
    total = int(report.get("total_records") or 0)
    if invalid and imported:
        report["status"] = "partial"
    elif invalid and not imported:
        report["status"] = "failed" if total else "imported"
    else:
        report["status"] = "imported"
    return report


def _import_report_path(platform_dir: Path, import_id: str) -> Path:
    if not import_id or "/" in import_id or "\\" in import_id or import_id in {".", ".."}:
        raise ValueError("invalid import_id")
    return _external_dir(platform_dir) / IMPORTS_DIR / ("%s.json" % import_id)


def save_external_import_report(platform_dir: Path, report: Dict[str, Any]) -> None:
    path = _import_report_path(platform_dir, str(report.get("import_id") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, report)


def load_external_import_report(platform_dir: Path, import_id: str) -> Optional[Dict[str, Any]]:
    try:
        path = _import_report_path(platform_dir, import_id)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_external_import_reports(platform_dir: Path) -> List[Dict[str, Any]]:
    directory = _external_dir(platform_dir) / IMPORTS_DIR
    if not directory.is_dir():
        return []
    reports: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("import_id"):
            reports.append(data)
    reports.sort(key=lambda item: str(item.get("imported_at") or ""), reverse=True)
    return reports


# ---------------------------------------------------------------------------
# queries and summary
# ---------------------------------------------------------------------------


def evaluations_for_run(records: List[Dict[str, Any]], run_id: str) -> List[Dict[str, Any]]:
    return [record for record in records if str(record.get("run_id") or "") == run_id]


def evaluations_for_agent(records: List[Dict[str, Any]], agent_id: str) -> List[Dict[str, Any]]:
    return [record for record in records if str(record.get("agent_id") or "") == agent_id]


def traces_for_run(records: List[Dict[str, Any]], run_id: str) -> List[Dict[str, Any]]:
    result = []
    for record in records:
        run_ids = (record.get("link") or {}).get("run_ids") or []
        if str(record.get("run_id") or "") == run_id or run_id in [str(item) for item in run_ids]:
            result.append(record)
    return result


def traces_for_agent(records: List[Dict[str, Any]], agent_id: str) -> List[Dict[str, Any]]:
    return [record for record in records if str(record.get("agent_id") or "") == agent_id]


def external_summary(
    evaluations: List[Dict[str, Any]],
    traces: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Display-only summary; never a quality signal."""
    source_systems: List[str] = []
    for record in list(evaluations) + list(traces):
        name = str(record.get("source_system") or "")
        if name and name not in source_systems:
            source_systems.append(name)
    linked = sum(1 for trace in traces if trace.get("run_id"))
    return {
        "count": len(evaluations) + len(traces),
        "evaluation_count": len(evaluations),
        "trace_count": len(traces),
        "linked_count": linked,
        "unlinked_count": len(traces) - linked,
        "source_systems": sorted(source_systems),
    }
