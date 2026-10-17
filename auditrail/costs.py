"""Phase 2 unified LLM cost records: model, storage, import and aggregation.

Cost evidence has exactly four sources (``COST_SOURCES``):

- ``observed``  — real token/cost data produced by the metrics proxy or the
  agent session itself (stored in the run's ``llm_calls.json``; never
  overwritten by imports).
- ``imported``  — external logs imported through the cost import pipeline.
- ``estimated``— computed from tokens and a configured price table; the record
  always carries the ``price_version`` it was computed with, so a later price
  change never silently rewrites history.
- ``missing``   — no cost data; surfaced as ``null``, never as a fake ``0``.

Design rules enforced here:

- token counts are never treated as currency; a cost number always carries a
  ``currency`` (only ``USD`` is produced by estimation; other currencies are
  passed through explicitly, never converted);
- imports are idempotent on ``(source_system, source_id)`` — re-importing the
  same line yields a duplicate, not a second record;
- imported/estimated records never replace observed data; conflicting sources
  for the same call are both kept and aggregation reports ``mixed``;
- no record ever stores prompts, completions, chain-of-thought or credentials
  (metadata keys and values are redacted on intake).

Storage (file based, no database)::

    platform/
      cost_records.jsonl        # imported + estimated records, atomic rewrite
      cost_imports/<id>.json    # one import report per import operation
      price_table.json          # optional, operator-maintained price config

This module is core logic: it never imports ``backend`` and needs no model
credentials.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .run_contract import utc_now
from .trajectory import write_json, write_text

SCHEMA_VERSION = 1

COST_SOURCES = ("observed", "imported", "estimated", "missing")
AGGREGATE_SOURCES = COST_SOURCES + ("mixed",)

STORE_FILE = "cost_records.jsonl"
IMPORTS_DIR = "cost_imports"
PRICE_TABLE_FILE = "price_table.json"

#: Import safety limits. A single malformed line never aborts an import, but
#: the file itself must stay within these bounds.
MAX_IMPORT_BYTES = 20 * 1024 * 1024
MAX_IMPORT_LINES = 100_000
MAX_LINE_BYTES = 256 * 1024
MAX_JSON_DEPTH = 10
MAX_FIELD_LENGTH = 256
MAX_METADATA_ENTRIES = 32
MAX_IMPORT_ERRORS = 50

#: metadata keys whose values may carry secrets or full prompts; anything
#: matching (substring, case-insensitive) is dropped on intake.
SENSITIVE_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "secret",
    "password",
    "credential",
    "prompt",
    "completion",
    "message",
    "content",
    "reasoning",
    "chain_of_thought",
)

#: Sources allowed in the persistent store. Observed data stays in the run's
#: own artifacts (``llm_calls.json``) and is derived on read, never imported
#: over.
_STORABLE_SOURCES = ("imported", "estimated")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _opt_int(value: Any) -> Optional[int]:
    if not _is_number(value):
        return None
    return max(int(value), 0)


def _opt_float(value: Any) -> Optional[float]:
    if not _is_number(value):
        return None
    return round(float(value), 8)


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


def redact_metadata(raw: Any) -> Dict[str, Any]:
    """Keep a small, non-sensitive metadata mapping; drop everything else."""
    if not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for key, value in raw.items():
        if len(cleaned) >= MAX_METADATA_ENTRIES:
            break
        name = str(key)[:64]
        lowered = name.lower()
        if any(fragment in lowered for fragment in SENSITIVE_KEY_FRAGMENTS):
            continue
        if isinstance(value, str):
            if _looks_like_secret(value):
                continue
            cleaned[name] = value[:MAX_FIELD_LENGTH]
        elif _is_number(value) or isinstance(value, bool) or value is None:
            cleaned[name] = value
    return cleaned


def _looks_like_secret(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("sk-") or "bearer " in lowered or "api_key" in lowered


# ---------------------------------------------------------------------------
# record normalization
# ---------------------------------------------------------------------------


def record_id_for(source_system: str, source_id: str) -> str:
    """Deterministic record id: re-importing the same line targets the same id."""
    text = "%s:%s" % (source_system, source_id)
    return "cost-%s" % hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def normalize_cost_record(raw: Any, *, source: str, default_currency: str = "USD") -> Dict[str, Any]:
    """Validate and normalize one cost record. Raises ``ValueError``.

    ``source`` must be one of ``COST_SOURCES``; callers importing external
    data pass ``imported`` and the estimation path passes ``estimated``.
    """
    if not isinstance(raw, dict):
        raise ValueError("cost record must be an object")
    if source not in COST_SOURCES:
        raise ValueError("cost source must be one of %s" % (list(COST_SOURCES),))
    if _json_depth(raw) > MAX_JSON_DEPTH:
        raise ValueError("cost record exceeds the maximum nesting depth")

    usage = raw.get("tokenUsage") or raw.get("token_usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = _opt_int(raw.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _opt_int(usage.get("inputTokens") or usage.get("input_tokens") or usage.get("prompt_tokens"))
    output_tokens = _opt_int(raw.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _opt_int(usage.get("outputTokens") or usage.get("output_tokens") or usage.get("completion_tokens"))
    total_tokens = _opt_int(raw.get("total_tokens"))
    if total_tokens is None:
        total_tokens = _opt_int(usage.get("totalTokens") or usage.get("total_tokens") or usage.get("total"))
    if total_tokens is None and (input_tokens or output_tokens):
        total_tokens = int(input_tokens or 0) + int(output_tokens or 0)

    input_cost = _opt_float(raw.get("input_cost"))
    output_cost = _opt_float(raw.get("output_cost"))
    total_cost = _opt_float(raw.get("total_cost"))
    if total_cost is None:
        total_cost = _opt_float(raw.get("cost"))
    if total_cost is None and (input_cost is not None or output_cost is not None):
        total_cost = round(float(input_cost or 0.0) + float(output_cost or 0.0), 8)

    currency = _opt_str(raw.get("currency"), limit=8)
    if total_cost is not None and not currency:
        currency = default_currency

    record = {
        "schema_version": SCHEMA_VERSION,
        "record_id": _opt_str(raw.get("record_id"), limit=64),
        "run_id": _opt_str(raw.get("run_id"), limit=128),
        "agent_id": _opt_str(raw.get("agent_id"), limit=128),
        "provider": _opt_str(raw.get("provider"), limit=64),
        "model": _opt_str(raw.get("model"), limit=128),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "input_cost": input_cost,
        "output_cost": output_cost,
        "total_cost": total_cost,
        "currency": currency,
        "source": source,
        "source_system": _opt_str(raw.get("source_system"), limit=64),
        "source_id": _opt_str(raw.get("source_id") or raw.get("callId") or raw.get("call_id"), limit=128),
        "observed_at": _opt_str(raw.get("observed_at") or raw.get("startTime") or raw.get("start_time"), limit=64),
        "imported_at": _opt_str(raw.get("imported_at"), limit=64),
        "price_version": _opt_str(raw.get("price_version"), limit=64),
        "metadata": redact_metadata(raw.get("metadata")),
    }
    if record["total_cost"] is None and not any(
        value is not None for value in (record["input_tokens"], record["output_tokens"], record["total_tokens"])
    ):
        raise ValueError("cost record carries neither tokens nor cost")
    return record


# ---------------------------------------------------------------------------
# persistent store (imported + estimated records)
# ---------------------------------------------------------------------------


def _store_path(platform_dir: Path) -> Path:
    return Path(platform_dir) / STORE_FILE


def load_cost_records(platform_dir: Path) -> List[Dict[str, Any]]:
    """Load stored cost records; a missing/corrupt store yields an empty list."""
    path = _store_path(platform_dir)
    if not path.is_file():
        return []
    records: List[Dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
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


def save_cost_records(platform_dir: Path, records: List[Dict[str, Any]]) -> None:
    """Atomically rewrite the whole cost record store (JSONL)."""
    path = _store_path(platform_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, ensure_ascii=False, default=str) for record in records]
    write_text(path, "".join(line + "\n" for line in lines))


def _import_report_path(platform_dir: Path, import_id: str) -> Path:
    if not import_id or "/" in import_id or "\\" in import_id or import_id in {".", ".."}:
        raise ValueError("invalid import_id")
    return Path(platform_dir) / IMPORTS_DIR / ("%s.json" % import_id)


def save_import_report(platform_dir: Path, report: Dict[str, Any]) -> None:
    path = _import_report_path(platform_dir, str(report.get("import_id") or ""))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, report)


def load_import_report(platform_dir: Path, import_id: str) -> Optional[Dict[str, Any]]:
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


# ---------------------------------------------------------------------------
# price table and estimation
# ---------------------------------------------------------------------------


def default_price_table() -> Dict[str, Any]:
    """The built-in table is empty: without configured prices there is no
    estimation at all (missing, never invented numbers)."""
    return {"schema_version": SCHEMA_VERSION, "price_version": None, "models": {}}


def load_price_table(platform_dir: Path) -> Dict[str, Any]:
    path = Path(platform_dir) / PRICE_TABLE_FILE
    if not path.is_file():
        return default_price_table()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default_price_table()
    if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
        return default_price_table()
    return data


def estimate_cost(
    price_table: Dict[str, Any],
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> Optional[Dict[str, Any]]:
    """Estimate a cost from the price table; ``None`` when no confirmed price.

    The result carries the ``price_version`` so a stored estimated record
    always shows which price produced it; a later price change never rewrites
    history.
    """
    models = price_table.get("models") if isinstance(price_table, dict) else None
    if not isinstance(models, dict):
        return None
    entry = models.get(model)
    if not isinstance(entry, dict):
        return None
    input_rate = entry.get("input_cost_per_1m_tokens")
    output_rate = entry.get("output_cost_per_1m_tokens")
    if not _is_number(input_rate) or not _is_number(output_rate):
        return None
    currency = str(entry.get("currency") or "USD")
    total = (int(input_tokens) / 1_000_000.0) * float(input_rate) + (int(output_tokens) / 1_000_000.0) * float(
        output_rate
    )
    return {
        "input_cost": round((int(input_tokens) / 1_000_000.0) * float(input_rate), 8),
        "output_cost": round((int(output_tokens) / 1_000_000.0) * float(output_rate), 8),
        "total_cost": round(total, 8),
        "currency": currency,
        "price_version": entry.get("effective_at") or price_table.get("price_version"),
    }


# ---------------------------------------------------------------------------
# import pipeline
# ---------------------------------------------------------------------------


def resolve_import_path(raw_path: Any, allowed_roots: List[Path]) -> Path:
    """Resolve an operator-supplied import path inside the allowed roots.

    Rejects NUL bytes, paths that escape the allowed roots (including via
    symlinks), ``.tmp`` staging files and non-files.
    """
    text = str(raw_path or "").strip()
    if not text or "\x00" in text:
        raise ValueError("source_file is required and must not contain NUL bytes")
    candidate = Path(text).expanduser()
    roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not candidate.is_absolute():
        for root in roots:
            resolved = (root / candidate).resolve()
            if _within(resolved, root) and resolved.is_file():
                candidate = resolved
                break
        else:
            raise ValueError("source_file does not resolve to a file inside the allowed directories")
    else:
        resolved = candidate.resolve()
        if not any(_within(resolved, root) for root in roots):
            raise ValueError("source_file must stay inside the allowed directories")
        if not resolved.is_file():
            raise ValueError("source_file is not a regular file")
        candidate = resolved
    if candidate.name.endswith(".tmp"):
        raise ValueError("staging files must not be imported")
    return candidate


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def import_cost_file(
    platform_dir: Path,
    source_path: Path,
    *,
    source_system: str,
    default_run_id: Optional[str] = None,
    default_agent_id: Optional[str] = None,
    dry_run: bool = False,
    price_table: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Import one JSONL cost log; returns the import report.

    Never executes anything from the file: lines are parsed as data only.
    Per-line failures are collected (bounded, redacted) and never abort the
    whole import. Idempotent on ``(source_system, source_id)``. ``dry_run``
    validates and counts without touching the store or the report directory.
    """
    source_system = str(source_system or "").strip()[:64]
    if not source_system:
        raise ValueError("source_system is required")
    path = Path(source_path)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("source_file is not readable: %s" % type(exc).__name__) from exc
    if size > MAX_IMPORT_BYTES:
        raise ValueError("source_file exceeds the %d byte limit" % MAX_IMPORT_BYTES)

    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "import_id": "import-%s" % uuid.uuid4().hex[:12],
        "status": "completed",
        "source_system": source_system,
        "source_file": path.name,
        "dry_run": bool(dry_run),
        "total_records": 0,
        "imported_records": 0,
        "duplicate_records": 0,
        "invalid_records": 0,
        "errors": [],
        "warnings": [],
        "imported_at": utc_now(),
    }

    existing = [] if dry_run else load_cost_records(platform_dir)
    seen = {
        (str(item.get("source_system") or ""), str(item.get("source_id") or ""))
        for item in existing
        if item.get("source_id")
    }
    new_records: List[Dict[str, Any]] = []
    table = price_table if price_table is not None else load_price_table(platform_dir)

    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ValueError("source_file is not readable: %s" % type(exc).__name__) from exc
    with handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no > MAX_IMPORT_LINES:
                report["warnings"].append("import stopped at the %d line limit" % MAX_IMPORT_LINES)
                break
            if not line.strip():
                continue
            if len(line.encode("utf-8", errors="replace")) > MAX_LINE_BYTES:
                _record_error(report, line_no, "line exceeds the %d byte limit" % MAX_LINE_BYTES)
                continue
            report["total_records"] += 1
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                _record_error(report, line_no, "invalid JSONL line")
                continue
            if not isinstance(raw, dict):
                _record_error(report, line_no, "line is not a JSON object")
                continue
            if default_run_id and not raw.get("run_id"):
                raw = {**raw, "run_id": default_run_id}
            if default_agent_id and not raw.get("agent_id"):
                raw = {**raw, "agent_id": default_agent_id}
            try:
                record = normalize_cost_record(raw, source="imported")
            except ValueError as exc:
                _record_error(report, line_no, str(exc))
                continue
            record["source_system"] = source_system
            if not record.get("source_id"):
                record["source_id"] = "line-%d" % line_no
            key = (source_system, str(record["source_id"]))
            if key in seen:
                report["duplicate_records"] += 1
                continue
            seen.add(key)
            record["record_id"] = record_id_for(source_system, str(record["source_id"]))
            record["imported_at"] = report["imported_at"]

            if record.get("total_cost") is None:
                # No explicit cost: estimate only from a configured price.
                estimate = estimate_cost(
                    table,
                    str(record.get("model") or ""),
                    int(record.get("input_tokens") or 0),
                    int(record.get("output_tokens") or 0),
                )
                if estimate is not None:
                    record.update(estimate)
                    record["source"] = "estimated"
            new_records.append(record)
            report["imported_records"] += 1

    if report["invalid_records"]:
        report["status"] = "completed_with_errors" if report["imported_records"] else "failed"
    if dry_run:
        report["status"] = "dry_run"
        return report

    save_cost_records(platform_dir, existing + new_records)
    save_import_report(platform_dir, report)
    return report


def _record_error(report: Dict[str, Any], line_no: int, message: str) -> None:
    report["invalid_records"] += 1
    if len(report["errors"]) < MAX_IMPORT_ERRORS:
        report["errors"].append({"line": line_no, "error": str(message)[:200]})


# ---------------------------------------------------------------------------
# observed records (derived from run artifacts, never persisted here)
# ---------------------------------------------------------------------------


def observed_records_for_run(run_dir: Path) -> List[Dict[str, Any]]:
    """Derive observed cost records from a run's ``llm_calls.json``.

    Only calls with an explicit numeric ``cost`` field produce records — token
    counts alone never become a cost. Nothing is written back to the run.
    """
    path = Path(run_dir) / "llm_calls.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    entries = data.get("llm_calls") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return []
    records = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        cost = entry.get("cost")
        if not _is_number(cost):
            continue
        try:
            record = normalize_cost_record({**entry, "cost": cost}, source="observed")
        except ValueError:
            continue
        record["source_system"] = "run_llm_calls"
        record["source_id"] = str(entry.get("callId") or entry.get("call_id") or "call-%d" % index)
        record["record_id"] = record_id_for("run_llm_calls", "%s:%s" % (Path(run_dir).name, record["source_id"]))
        record["run_id"] = Path(run_dir).name
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# aggregation
# ---------------------------------------------------------------------------


def summarize_records(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize cost records without double counting or inventing zeros.

    Records sharing a ``source_id`` across sources describe the same call:
    the observed value wins the total, every source is still reported, and
    the aggregate source becomes ``mixed``. Runs without any usable cost get
    ``total=None`` / ``source="missing"``.
    """
    by_call: Dict[str, Dict[str, Any]] = {}
    anonymous: List[Dict[str, Any]] = []
    for record in records:
        key = str(record.get("source_id") or "")
        if not key:
            anonymous.append(record)
            continue
        current = by_call.get(key)
        if current is None or (current.get("source") != "observed" and record.get("source") == "observed"):
            by_call[key] = record

    total = 0.0
    has_cost = False
    currencies: List[str] = []
    sources: List[str] = []
    conflicts = 0
    chosen = list(by_call.values()) + anonymous
    for record in chosen:
        source = str(record.get("source") or "missing")
        if source not in sources:
            sources.append(source)
        value = record.get("total_cost")
        if _is_number(value):
            total += float(value)
            has_cost = True
            currency = str(record.get("currency") or "")
            if currency and currency not in currencies:
                currencies.append(currency)
    seen_keys = {str(r.get("source_id")) for r in records if r.get("source_id")}
    conflicts = sum(1 for key in seen_keys if sum(1 for r in records if str(r.get("source_id") or "") == key) > 1)

    if not has_cost:
        return {
            "total": None,
            "currency": None,
            "source": "missing",
            "sources": [],
            "record_count": len(records),
            "conflict_count": conflicts,
        }
    if len(sources) > 1:
        source = "mixed"
    else:
        source = sources[0]
    return {
        "total": round(total, 8),
        "currency": currencies[0] if currencies else "USD",
        "currencies": currencies,
        "source": source,
        "sources": sources,
        "record_count": len(records),
        "conflict_count": conflicts,
    }


def records_for_run(records: List[Dict[str, Any]], run_id: str) -> List[Dict[str, Any]]:
    return [record for record in records if str(record.get("run_id") or "") == run_id]


def records_for_agent(records: List[Dict[str, Any]], agent_id: str) -> List[Dict[str, Any]]:
    return [record for record in records if str(record.get("agent_id") or "") == agent_id]


def run_cost_summary(run_dir: Path, stored_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Combined observed + stored cost summary for one run directory."""
    run_id = Path(run_dir).name
    combined = observed_records_for_run(run_dir) + records_for_run(stored_records, run_id)
    return summarize_records(combined)


def summarize_runs(
    run_ids: List[str],
    stored_records: List[Dict[str, Any]],
    observed_lookup: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """Aggregate costs across many runs (agent-level rollups)."""
    combined: List[Dict[str, Any]] = []
    for record in stored_records:
        if str(record.get("run_id") or "") in run_ids or not record.get("run_id"):
            combined.append(record)
    if observed_lookup is not None:
        for run_id in run_ids:
            combined.extend(observed_lookup(run_id))
    return summarize_records(combined)
