"""Langfuse-style external trace / score import adapter (Phase 3).

The adapter speaks to *Langfuse-compatible exports* without requiring the
``langfuse`` SDK (never a core dependency):

- **file input**: a JSON or JSONL export of traces / scores, parsed as data
  only — nothing in the file is executed;
- **HTTP input**: an explicitly configured Langfuse public API endpoint,
  guarded by :mod:`auditrail.netguard` (scheme/host/port validation, DNS
  resolution checks, redirect re-validation). HTTP reading is disabled by
  default and requires ``enabled: true`` in the config. API tokens are read
  from an environment variable named by the config — the token value is
  never written to configs, logs, errors or stored records.

Association priority (strongest first)::

    platform run_id metadata
      > session_id
      > trace-level run_id field
      > operator source_id mapping
      > agent_id + time window      (always weak)
      > unlinked_external_trace

A trace id from Langfuse is stored as ``source_id`` only — it is never
treated as a platform run id. Traces that cannot be linked are kept as
``unlinked_external_trace`` records; link failures never block other traces.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import netguard
from ..costs import resolve_import_path
from ..run_contract import utc_now
from . import external_models as ext

SOURCE_SYSTEM = "langfuse"

#: HTTP fetch safety bounds.
MAX_HTTP_RESPONSE_BYTES = ext.MAX_IMPORT_BYTES
HTTP_TIMEOUT_SECONDS = 15
MAX_HTTP_PAGES = 20

_CONFIG_KEYS = ("base_url", "enabled", "token_env", "allowed_hosts")


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


def validate_config(config: Any, *, policy: Optional[netguard.EndpointPolicy] = None) -> Dict[str, Any]:
    """Validate and normalize a Langfuse adapter config. Raises ``ValueError``.

    Returned mapping::

        {
          "enabled": bool,          # HTTP reading (default False)
          "base_url": str | None,   # netguard-validated endpoint
          "token_env": str | None,  # name of the env var holding the token
        }

    The token *value* is never part of the normalized config.
    """
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ValueError("langfuse config must be an object")
    unknown = sorted(set(config) - set(_CONFIG_KEYS))
    if unknown:
        raise ValueError("langfuse config has unknown keys: %s" % ", ".join(unknown))

    enabled = bool(config.get("enabled", False))
    base_url: Optional[str] = None
    if config.get("base_url"):
        http_policy = policy or netguard.EndpointPolicy(
            allowed_hosts=tuple(str(item).lower() for item in config.get("allowed_hosts") or ()),
        )
        try:
            base_url = netguard.validate_endpoint(config.get("base_url"), http_policy)
        except netguard.EndpointRejected as exc:
            raise ValueError(str(exc)) from exc
    elif enabled:
        raise ValueError("langfuse HTTP import requires base_url")

    token_env = None
    if config.get("token_env") is not None:
        token_env = str(config.get("token_env") or "").strip()
        if not token_env or any(ch in token_env for ch in " \t\n\x00="):
            raise ValueError("token_env must be an environment variable name")

    return {"enabled": enabled, "base_url": base_url, "token_env": token_env}


# ---------------------------------------------------------------------------
# normalization (Langfuse export shapes -> unified external records)
# ---------------------------------------------------------------------------


def _dotted_get(value: Any, path: str) -> Any:
    current = value
    for part in str(path or "").split("."):
        if not part:
            return None
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _iso(raw: Any) -> Optional[str]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _usage_tokens(raw: Dict[str, Any]) -> Dict[str, Optional[int]]:
    """Extract token usage from a Langfuse trace/observation export."""
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    containers = [usage, raw]
    result: Dict[str, Optional[int]] = {"input": None, "output": None, "total": None}
    for container in containers:
        if not isinstance(container, dict):
            continue
        if result["input"] is None:
            result["input"] = ext._opt_int(
                container.get("input") or container.get("promptTokens") or container.get("prompt_tokens")
            )
        if result["output"] is None:
            result["output"] = ext._opt_int(
                container.get("output") or container.get("completionTokens") or container.get("completion_tokens")
            )
        if result["total"] is None:
            result["total"] = ext._opt_int(container.get("total") or container.get("totalTokens"))
    if result["total"] is None and (result["input"] or result["output"]):
        result["total"] = int(result["input"] or 0) + int(result["output"] or 0)
    return result


def _latency_ms(raw: Dict[str, Any]) -> Optional[int]:
    value = ext._opt_int(raw.get("latency_ms"))
    if value is not None:
        return value
    latency = raw.get("latency")
    if ext._is_number(latency):
        # Langfuse REST reports trace latency in seconds.
        return max(int(round(float(latency) * 1000.0)), 0)
    return None


def normalize_trace(raw: Any) -> Dict[str, Any]:
    """Normalize one Langfuse trace export into an :class:`ExternalTrace`.

    Association fields (``run_id``, ``session_id``, ``source_id_map``) are
    resolved separately by :func:`link_trace`; this function only shapes the
    record. Raises ``ValueError`` on unusable input.
    """
    if not isinstance(raw, dict):
        raise ValueError("langfuse trace must be an object")
    source_id = raw.get("id") or raw.get("trace_id")
    if not source_id:
        raise ValueError("langfuse trace is missing id")

    metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
    session_id = ext._opt_str(raw.get("sessionId") or raw.get("session_id"), limit=128)
    agent_id = ext._opt_str(
        raw.get("agent_id") or metadata.get("agent_id"), limit=128
    )

    status = "completed"
    status_message = raw.get("statusMessage") or raw.get("status_message")
    raw_status = str(raw.get("status") or "").upper()
    if raw_status == "ERROR" or status_message:
        status = "error"

    cost_value = ext._opt_float(
        raw.get("totalCost") if raw.get("totalCost") is not None else raw.get("calculatedTotalCost")
    )

    generations: List[Dict[str, Any]] = []
    observations = raw.get("observations") or raw.get("generations") or []
    if isinstance(observations, list):
        for item in observations[: ext.MAX_GENERATIONS]:
            if not isinstance(item, dict):
                continue
            generations.append(
                {
                    "source_id": ext._opt_str(item.get("id"), limit=128),
                    "name": ext._opt_str(item.get("name"), limit=128),
                    "model": ext._opt_str(item.get("model"), limit=128),
                    "tokens": _usage_tokens(item),
                    "cost": {
                        "value": ext._opt_float(item.get("calculatedTotalCost")),
                        "currency": "USD",
                        "source": "imported" if ext._opt_float(item.get("calculatedTotalCost")) is not None else "missing",
                    },
                }
            )

    record = {
        "source_id": str(source_id)[:128],
        "run_id": ext._opt_str(raw.get("run_id"), limit=128),
        "agent_id": agent_id,
        "name": ext._opt_str(raw.get("name"), limit=128),
        "started_at": _iso(raw.get("timestamp") or raw.get("startTime") or raw.get("started_at")),
        "ended_at": _iso(raw.get("endTime") or raw.get("ended_at")),
        "latency_ms": _latency_ms(raw),
        "status": status,
        "error": status_message if isinstance(status_message, str) else None,
        "generations": generations,
        "tokens": _usage_tokens(raw),
        "cost": {
            "value": cost_value,
            "currency": "USD" if cost_value is not None else None,
            "source": "imported" if cost_value is not None else "missing",
        },
        "metadata": metadata,
        "link": {"method": "none", "confidence": "unlinked"},
        # Adapter-private association hints (stripped before storage):
        "_session_id": session_id,
    }
    normalized = ext.normalize_external_trace(record, source_system=SOURCE_SYSTEM)
    # Re-attach the association hint for link_trace; it is stripped there
    # before the record is persisted.
    normalized["_session_id"] = session_id
    return normalized


def normalize_score(raw: Any) -> Dict[str, Any]:
    """Normalize one Langfuse score export into an :class:`ExternalEvaluation`.

    Score ranges are recorded explicitly; a 0–1 score is never reinterpreted
    as a 0–100 completion score.
    """
    if not isinstance(raw, dict):
        raise ValueError("langfuse score must be an object")
    source_id = raw.get("id") or raw.get("score_id")
    if not source_id:
        raise ValueError("langfuse score is missing id")

    data_type = str(raw.get("dataType") or raw.get("data_type") or "NUMERIC").upper()
    score = ext._opt_float(raw.get("value"))
    score_min: Optional[float] = None
    score_max: Optional[float] = None
    label = ext._opt_str(raw.get("stringValue") or raw.get("string_value"), limit=128)

    if data_type == "BOOLEAN":
        score_min, score_max = 0.0, 1.0
        if label is None and score is not None:
            label = "true" if score >= 1 else "false"
    elif data_type == "CATEGORICAL":
        score = None  # categorical scores carry a label, not a number
    else:
        score_min = ext._opt_float(raw.get("minValue") or raw.get("score_min"))
        score_max = ext._opt_float(raw.get("maxValue") or raw.get("score_max"))

    status = ext._opt_str(raw.get("status"), limit=32)
    if status is None:
        if data_type == "BOOLEAN" and score is not None:
            status = "pass" if score >= 1 else "fail"
        else:
            status = "unknown"

    trace_id = ext._opt_str(raw.get("traceId") or raw.get("trace_id"), limit=128)
    record = {
        "source_id": str(source_id)[:128],
        "source_version": ext._opt_str(raw.get("source") or raw.get("version"), limit=64),
        "run_id": ext._opt_str(raw.get("run_id"), limit=128),
        "agent_id": ext._opt_str(raw.get("agent_id"), limit=128),
        "metric_name": ext._opt_str(raw.get("name"), limit=128),
        "status": status,
        "score": score,
        "score_min": score_min,
        "score_max": score_max,
        "label": label,
        "comment": raw.get("comment"),
        "metadata": raw.get("metadata"),
        "provenance": {
            "association": "trace:%s" % trace_id if trace_id else "none",
        },
        "_trace_id": trace_id,
    }
    evaluation = ext.normalize_external_evaluation(record, source_system=SOURCE_SYSTEM)
    return evaluation


# ---------------------------------------------------------------------------
# association
# ---------------------------------------------------------------------------


def new_run_context() -> Dict[str, Any]:
    """An empty association context (platform run/session/agent knowledge)."""
    return {
        "run_ids": set(),
        "sessions": {},
        "source_id_map": {},
        "agent_runs": {},
    }


def link_trace(
    trace: Dict[str, Any],
    run_context: Dict[str, Any],
    *,
    run_id_field: str = "metadata.run_id",
    session_id_field: Optional[str] = None,
) -> Dict[str, Any]:
    """Link one normalized trace to platform runs.

    Returns a LinkResult mapping::

        {
          "trace": <updated trace record>,
          "run_ids": [str, ...],       # all linked runs (one-to-many kept)
          "method": str,               # run_id|session_id|trace_field|source_id_map|agent_time_window|none
          "confidence": "exact|strong|weak|unlinked",
          "warnings": [str, ...],
        }
    """
    warnings: List[str] = []
    run_ids: List[str] = []
    method = "none"
    confidence = "unlinked"

    known_runs = set(run_context.get("run_ids") or set())
    sessions = run_context.get("sessions") or {}
    source_id_map = run_context.get("source_id_map") or {}
    agent_runs = run_context.get("agent_runs") or {}

    raw_hint = {
        "metadata": trace.get("metadata") or {},
        "run_id": trace.get("run_id"),
    }

    def _known(candidate: Any) -> Optional[str]:
        text = ext._opt_str(candidate, limit=128)
        if text and (not known_runs or text in known_runs):
            return text
        if text and known_runs and text not in known_runs:
            warnings.append("run_id hint %s does not match a platform run" % text[:64])
        return None

    # 1. platform run_id carried in trace metadata (dotted path).
    candidate = _known(_dotted_get(raw_hint, run_id_field) or _dotted_get(trace, run_id_field))
    if candidate:
        run_ids, method, confidence = [candidate], "run_id", "exact"

    # 2. session id.
    if not run_ids:
        session_id = trace.get("_session_id")
        if session_id_field:
            session_id = session_id or _dotted_get(trace, session_id_field)
        mapped = sessions.get(str(session_id or ""))
        if mapped:
            run_ids, method, confidence = [str(mapped)], "session_id", "strong"

    # 3. trace-level run_id field.
    if not run_ids:
        candidate = _known(raw_hint.get("run_id"))
        if candidate:
            run_ids, method, confidence = [candidate], "trace_field", "strong"

    # 4. operator-provided source_id mapping.
    if not run_ids:
        mapped = source_id_map.get(str(trace.get("source_id") or ""))
        if mapped and (not known_runs or mapped in known_runs):
            run_ids, method, confidence = [str(mapped)], "source_id_map", "strong"

    # 5. agent_id + time window (always weak).
    if not run_ids:
        agent_id = trace.get("agent_id")
        started = _parse_iso_time(trace.get("started_at"))
        candidates = []
        if agent_id and started is not None:
            for entry in agent_runs.get(str(agent_id), []):
                window_start = _parse_iso_time(entry.get("started_at"))
                window_end = _parse_iso_time(entry.get("finished_at"))
                if window_start is None:
                    continue
                if window_end is not None and not (window_start <= started <= window_end):
                    continue
                if window_end is None and started < window_start:
                    continue
                candidates.append(str(entry.get("run_id") or ""))
        candidates = [item for item in candidates if item and (not known_runs or item in known_runs)]
        if candidates:
            run_ids, method, confidence = candidates, "agent_time_window", "weak"
            if len(candidates) > 1:
                warnings.append(
                    "weak time-window association matched %d runs; all kept" % len(candidates)
                )

    trace = dict(trace)
    trace.pop("_session_id", None)
    if not run_ids:
        ext.mark_trace_unlinked(trace, reason="no platform run association found")
    else:
        if len(run_ids) > 1 and confidence in ("exact", "strong"):
            warnings.append("multiple runs matched one trace; all associations kept")
        trace["run_id"] = run_ids[0]
        trace["link"] = {"method": method, "confidence": confidence, "run_ids": run_ids}
        trace["import_status"] = "imported"
    existing_warnings = list(trace.get("warnings") or [])
    trace["warnings"] = (existing_warnings + [item[:200] for item in warnings])[:10]
    return {
        "trace": trace,
        "run_ids": run_ids,
        "method": method,
        "confidence": confidence,
        "warnings": warnings,
    }


def _parse_iso_time(raw: Any):
    if not isinstance(raw, str) or not raw.strip():
        return None
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# source reading (file + optional HTTP)
# ---------------------------------------------------------------------------


def read_source_records(
    source: Any,
    *,
    kind: str,
    allowed_roots: List[Path],
    config: Optional[Dict[str, Any]] = None,
    fetcher: Optional[Any] = None,
) -> Tuple[List[Any], List[str]]:
    """Read raw trace/score payloads from a file or (explicitly enabled) HTTP
    endpoint. Returns ``(records, warnings)``; raises ``ValueError`` on source
    level failures. A single malformed record never aborts the read.
    """
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    source_kind = str(source.get("kind") or "file").strip()
    if source_kind == "file":
        path = resolve_import_path(source.get("path"), allowed_roots)
        return _read_file_records(path)
    if source_kind == "http":
        normalized = validate_config(config or {})
        if not normalized.get("enabled"):
            raise ValueError("langfuse HTTP import is disabled; set config.enabled=true explicitly")
        if not normalized.get("base_url"):
            raise ValueError("langfuse HTTP import requires config.base_url")
        return _read_http_records(normalized, kind, fetcher=fetcher)
    raise ValueError("unsupported source kind %r" % source_kind)


def _read_file_records(path: Path) -> Tuple[List[Any], List[str]]:
    warnings: List[str] = []
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("source file is not readable: %s" % type(exc).__name__) from exc
    if size > ext.MAX_IMPORT_BYTES:
        raise ValueError("source file exceeds the %d byte limit" % ext.MAX_IMPORT_BYTES)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError("source file is not readable: %s" % type(exc).__name__) from exc

    records: List[Any] = []
    stripped = text.lstrip()
    payload: Any = None
    parsed_whole = False
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            payload = json.loads(text)
            parsed_whole = True
        except json.JSONDecodeError:
            # A JSONL export also starts with "{" — fall through to the
            # line-by-line reader instead of failing the whole import.
            payload = None
    if parsed_whole:
        if isinstance(payload, dict):
            for key in ("traces", "scores", "data", "records"):
                if isinstance(payload.get(key), list):
                    records = payload[key]
                    break
            else:
                records = [payload]
        elif isinstance(payload, list):
            records = payload
    else:
        for line_no, line in enumerate(text.splitlines(), start=1):
            if line_no > ext.MAX_IMPORT_LINES:
                warnings.append("import stopped at the %d line limit" % ext.MAX_IMPORT_LINES)
                break
            if not line.strip():
                continue
            if len(line.encode("utf-8", errors="replace")) > ext.MAX_LINE_BYTES:
                warnings.append("line %d exceeds the byte limit and was skipped" % line_no)
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                warnings.append("line %d is not valid JSON and was skipped" % line_no)
    return records, warnings


def _read_http_records(
    config: Dict[str, Any],
    kind: str,
    *,
    fetcher: Optional[Any] = None,
) -> Tuple[List[Any], List[str]]:
    """Fetch trace/score pages from a Langfuse-compatible public API.

    The token is read from the environment variable named by
    ``config['token_env']``; its value never appears in errors or records.
    """
    import os

    base_url = str(config.get("base_url") or "")
    token = None
    if config.get("token_env"):
        token = os.environ.get(str(config["token_env"]))
    fetch = fetcher or _default_fetch
    records: List[Any] = []
    warnings: List[str] = []
    path = "/api/public/%s" % ("traces" if kind == "traces" else "scores")
    for page in range(1, MAX_HTTP_PAGES + 1):
        url = "%s%s?limit=100&page=%d" % (base_url, path, page)
        try:
            payload = fetch(url, token)
        except Exception as exc:
            # Never leak the URL query (may embed params) or token; type only.
            raise ValueError("langfuse HTTP fetch failed: %s" % type(exc).__name__) from exc
        if not isinstance(payload, dict):
            raise ValueError("langfuse HTTP response is not a JSON object")
        batch = payload.get("data")
        if not isinstance(batch, list):
            raise ValueError("langfuse HTTP response is missing the data list")
        records.extend(batch)
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        total_pages = meta.get("totalPages")
        if not batch or (ext._is_number(total_pages) and page >= int(total_pages)):
            break
    return records, warnings


def _default_fetch(url: str, token: Optional[str]) -> Any:
    """Stdlib HTTP GET with a bounded response; redirect targets are
    re-validated through the endpoint policy."""
    parsed_base = url.split("?", 1)[0]
    endpoint = netguard.validate_endpoint(parsed_base)
    headers = {"Accept": "application/json", "User-Agent": "auditrail-langfuse-import/1.0"}
    if token:
        headers["Authorization"] = "Bearer %s" % token

    class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: N802
            netguard.validate_endpoint(newurl.split("?", 1)[0])
            return super().redirect_request(req, fp, code, msg, hdrs, newurl)

    opener = urllib.request.build_opener(_GuardedRedirect())
    request = urllib.request.Request(endpoint + url[len(parsed_base):], headers=headers, method="GET")
    with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        body = response.read(MAX_HTTP_RESPONSE_BYTES + 1)
    if len(body) > MAX_HTTP_RESPONSE_BYTES:
        raise ValueError("response too large")
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# import pipelines
# ---------------------------------------------------------------------------


def import_traces(
    platform_dir: Path,
    source: Any,
    *,
    allowed_roots: List[Path],
    options: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    run_context: Optional[Dict[str, Any]] = None,
    fetcher: Optional[Any] = None,
) -> Dict[str, Any]:
    """Import Langfuse-style traces; returns the import report.

    Idempotent on ``(langfuse, source_id)``. Traces that cannot be linked are
    stored as ``unlinked_external_trace`` evidence. ``dry_run`` validates and
    counts without touching the store.
    """
    options = options or {}
    dry_run = bool(options.get("dry_run"))
    association = options.get("association") if isinstance(options.get("association"), dict) else {}
    report = ext.new_import_report(SOURCE_SYSTEM, dry_run=dry_run)
    report["kind"] = "traces"

    records, read_warnings = read_source_records(
        source, kind="traces", allowed_roots=allowed_roots, config=config, fetcher=fetcher
    )
    for message in read_warnings[: ext.MAX_IMPORT_ERRORS]:
        ext.record_import_warning(report, message)

    context = run_context or new_run_context()
    incoming: List[Dict[str, Any]] = []
    for index, raw in enumerate(records, start=1):
        report["total_records"] += 1
        try:
            trace = normalize_trace(raw)
        except ValueError as exc:
            ext.record_import_error(report, index, str(exc))
            continue
        try:
            result = link_trace(
                trace,
                context,
                run_id_field=str(association.get("run_id_field") or "metadata.run_id"),
                session_id_field=association.get("session_id_field"),
            )
        except Exception as exc:  # a link failure never blocks other traces
            ext.mark_trace_unlinked(trace, reason="association failed: %s" % type(exc).__name__)
            result = {"trace": trace, "warnings": list(trace.get("warnings") or [])}
        for message in result.get("warnings") or []:
            ext.record_import_warning(report, "trace %s: %s" % (trace.get("source_id"), message))
        linked = result["trace"]
        if linked.get("run_id") is None:
            report["unlinked_records"] += 1
        report["imported_records"] += 1
        incoming.append(linked)

    if dry_run:
        report["status"] = "dry_run"
        return report

    existing = ext.load_external_traces(platform_dir)
    merged, duplicates, merge_warnings = ext.merge_external_records(existing, incoming, id_field="trace")
    report["duplicate_records"] = duplicates
    report["imported_records"] -= duplicates
    for message in merge_warnings:
        ext.record_import_warning(report, message)
    ext.save_external_traces(platform_dir, merged)
    ext.finalize_import_report(report)
    ext.save_external_import_report(platform_dir, report)
    return report


def import_scores(
    platform_dir: Path,
    source: Any,
    *,
    allowed_roots: List[Path],
    options: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    trace_lookup: Optional[Any] = None,
    fetcher: Optional[Any] = None,
) -> Dict[str, Any]:
    """Import Langfuse-style scores into the external evaluation store.

    Scores never modify judge results, deterministic caps or completion
    scores. ``trace_lookup`` maps a Langfuse ``traceId`` to a platform
    ``run_id`` (derived from already imported traces when not provided).
    """
    options = options or {}
    dry_run = bool(options.get("dry_run"))
    report = ext.new_import_report(SOURCE_SYSTEM, dry_run=dry_run)
    report["kind"] = "scores"

    records, read_warnings = read_source_records(
        source, kind="scores", allowed_roots=allowed_roots, config=config, fetcher=fetcher
    )
    for message in read_warnings[: ext.MAX_IMPORT_ERRORS]:
        ext.record_import_warning(report, message)

    if trace_lookup is None:
        stored = ext.load_external_traces(platform_dir)
        trace_lookup = {
            str(item.get("source_id") or ""): item.get("run_id")
            for item in stored
            if item.get("source_id")
        }

    incoming: List[Dict[str, Any]] = []
    for index, raw in enumerate(records, start=1):
        report["total_records"] += 1
        try:
            evaluation = normalize_score(raw)
        except ValueError as exc:
            ext.record_import_error(report, index, str(exc))
            continue
        trace_id = None
        if isinstance(raw, dict):
            trace_id = raw.get("traceId") or raw.get("trace_id")
        if evaluation.get("run_id") is None and trace_id:
            linked_run = trace_lookup.get(str(trace_id))
            if linked_run:
                evaluation["run_id"] = linked_run
                evaluation["provenance"]["association"] = "trace:%s" % str(trace_id)[:64]
                evaluation["provenance"]["confidence"] = "strong"
        if evaluation.get("run_id") is None:
            report["unlinked_records"] += 1
        report["imported_records"] += 1
        incoming.append(evaluation)

    if dry_run:
        report["status"] = "dry_run"
        return report

    existing = ext.load_external_evaluations(platform_dir)
    merged, duplicates, merge_warnings = ext.merge_external_records(existing, incoming, id_field="evaluation")
    report["duplicate_records"] = duplicates
    report["imported_records"] -= duplicates
    for message in merge_warnings:
        ext.record_import_warning(report, message)
    ext.save_external_evaluations(platform_dir, merged)
    ext.finalize_import_report(report)
    ext.save_external_import_report(platform_dir, report)
    return report
