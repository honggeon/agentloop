"""Agent detail workspace aggregation (read-only).

Powers the LangSmith-style information hierarchy — Agent identity ->
Models / Tools / Trends / KPIs / Health -> Runs -> Run detail — on top of
Auditrail's own run artifacts. This module only *reads* ``runs/<run_id>/``
and ``platform/`` state; it never writes run artifacts, never re-implements
audit/judge schemas, and never fabricates missing data:

- ``None``  = data absent or not computable
- ``[]``    = explicitly empty list
- ``unknown`` = an explicit not-evaluated state from an existing schema
- costs carry a ``observed|imported|estimated|missing`` source

Attribution priority for one run -> agent:

1. ``run.json.agent_ref.id`` (Run Contract v1)
2. configured agent whose endpoint host matches ``source.json.server_host``
   (legacy runs without ``run.json``); the host is never used as a name.

One bad run never aborts an aggregation: the row is skipped and recorded in
``errors`` with a redacted message, and the response sets ``partial=true``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from auditrail import run_contract
from auditrail import testsuites as suite_mod
from auditrail.audit import DANGEROUS_TOOLS
from auditrail.evaluation import load_evaluation

from backend.app import platform_store
from backend.app.config import Settings
from backend.app.run_executor import is_stale
# Reuse the single LLM cost rate table from the runs dashboard so the Agent
# workspace never invents a second pricing source.
from backend.app.routes.runs import _estimate_model_cost

RANGE_DAYS = {"7d": 7, "30d": 30, "all": None}
RUN_STATUS_FILTERS = ("all", "completed", "failed", "timed_out", "cancelled", "stale")
DEFAULT_RUN_LIMIT = 50
MAX_RUN_LIMIT = 200

COST_SOURCES = ("observed", "imported", "estimated", "missing")
HEALTH_LABELS = {"pass": "良好", "partial": "关注", "fail": "异常", "unknown": "未评估"}

#: Run lifecycle statuses that count as errors for KPI / trend purposes.
_ERROR_RUN_STATUSES = frozenset({"failed", "timed_out"})


# ---------------------------------------------------------------------------
# validation & small helpers
# ---------------------------------------------------------------------------


def validate_agent_id(agent_id: Any) -> str:
    text = str(agent_id or "").strip()
    if not text or "/" in text or "\\" in text or text in {".", ".."}:
        raise ValueError("invalid agent_id")
    return text


def validate_range(value: Any) -> str:
    text = str(value or "7d").strip()
    if text not in RANGE_DAYS:
        raise ValueError("range must be one of %s" % sorted(RANGE_DAYS))
    return text


def validate_status_filter(value: Any) -> str:
    text = str(value or "all").strip()
    if text not in RUN_STATUS_FILTERS:
        raise ValueError("status must be one of %s" % list(RUN_STATUS_FILTERS))
    return text


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _first_int(source: Dict[str, Any], *keys: str) -> int:
    for key in keys:
        if source.get(key) is not None:
            return _int_value(source.get(key))
    return 0


def _endpoint_host(endpoint: Any) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(endpoint or "").strip())
    return (parsed.hostname or "").lower()


def _health_metric(value: Any, status: str, source: str, detail: str) -> Dict[str, Any]:
    return {
        "value": value,
        "status": status,
        "source": source,
        "label": HEALTH_LABELS.get(status, "未评估"),
        "detail": detail,
    }


# ---------------------------------------------------------------------------
# run scanning (one pass, per-run fault isolation)
# ---------------------------------------------------------------------------


def _iter_run_dirs(settings: Settings):
    root = Path(settings.runs_dir)
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith("."):
            yield child


def _retry_index_for(state: Dict[str, Any], settings: Settings, job_cache: Dict[str, Optional[Dict[str, Any]]]) -> Optional[int]:
    suite_ref = state.get("suite_ref") or {}
    job_id = str(suite_ref.get("job_id") or "")
    attempt_id = str(suite_ref.get("attempt_id") or "")
    if not job_id or not attempt_id:
        return None
    if job_id not in job_cache:
        try:
            job_cache[job_id] = platform_store.get_suite_job(job_id, settings)
        except Exception:
            job_cache[job_id] = None
    record = job_cache.get(job_id)
    if not record:
        return None
    attempt = suite_mod.find_attempt(record, attempt_id)
    if attempt is None:
        return None
    return _int_value(attempt.get("retry_index"))


def _scan_one_run(run_dir: Path, settings: Settings, job_cache: Dict[str, Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Light per-run row. Raises nothing for missing artifacts (legacy runs)."""
    state = run_contract.load_run_state(run_dir)
    agent_ref = state.get("agent_ref") or {}
    test_case_ref = state.get("test_case_ref") or {}
    suite_ref = state.get("suite_ref") or {}
    source = _read_json(run_dir / "source.json") or {}

    judge = _read_json(run_dir / "agent_judge.json")
    judge_overall_status: Optional[str] = None
    completion_score: Optional[float] = None
    judge_reliability: Dict[str, Any] = {}
    judge_evidence_refs = 0
    if judge is not None:
        judge_overall_status = str(judge.get("overall_status") or "unknown")
        score = judge.get("completion_score")
        completion_score = float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else None
        reliability = judge.get("judge_reliability")
        judge_reliability = reliability if isinstance(reliability, dict) else {}
        for dimension in (judge.get("dimensions") or {}).values():
            if isinstance(dimension, dict):
                evidence = dimension.get("evidence")
                if isinstance(evidence, list):
                    judge_evidence_refs += len(evidence)

    evaluation = load_evaluation(run_dir)
    evaluation_evidence_refs = 0
    evaluation_status: Optional[str] = None
    if evaluation is not None and evaluation.get("status") != "invalid":
        evaluation_status = str(evaluation.get("status"))
        for group in ("deterministic_checks", "expectation_results", "artifact_checks"):
            for item in evaluation.get(group) or []:
                if isinstance(item, dict):
                    refs = item.get("evidence_refs")
                    if isinstance(refs, list):
                        evaluation_evidence_refs += len(refs)

    audit = _read_json(run_dir / "audit.json") or {}
    tool_calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    findings = [item for item in audit.get("findings") or [] if isinstance(item, dict)]
    tool_error_count = sum(1 for call in tool_calls if call.get("status") in {"error", "missing", "failed"})
    high_severity = sum(1 for item in findings if str(item.get("severity") or "").lower() == "error")
    has_audit = bool(audit)

    started_at = state.get("started_at")
    finished_at = state.get("finished_at")
    started_dt = _parse_iso(started_at)
    finished_dt = _parse_iso(finished_at)
    duration_ms: Optional[int] = None
    if started_dt and finished_dt and finished_dt >= started_dt:
        duration_ms = int((finished_dt - started_dt).total_seconds() * 1000)

    status = str(state.get("status") or "failed")
    stale = bool(is_stale(run_dir)) if status in {"queued", "running"} else False
    manifest = state.get("artifact_manifest") or []

    total_evidence = judge_evidence_refs + evaluation_evidence_refs
    return {
        "run_id": str(state.get("run_id") or run_dir.name),
        "agent_id": str(agent_ref.get("id") or "") or None,
        "case_id": str(test_case_ref.get("id") or "") or None,
        "job_id": str(suite_ref.get("job_id") or "") or None,
        "attempt_id": str(suite_ref.get("attempt_id") or "") or None,
        "retry_index": _retry_index_for(state, settings, job_cache),
        "status": status,
        "stale": stale,
        "judge_overall_status": judge_overall_status,
        "completion_score": completion_score,
        "started_at": started_at if isinstance(started_at, str) else None,
        "finished_at": finished_at if isinstance(finished_at, str) else None,
        "duration_ms": duration_ms,
        "tool_error_count": tool_error_count,
        "deterministic_finding_count": len(findings),
        "high_severity_finding_count": high_severity,
        "has_evidence": total_evidence > 0,
        "legacy_inferred": bool(state.get("legacy_inferred")),
        # internal aggregation payload (stripped before API serialization)
        "_effective_ts": started_dt or finished_dt,
        "_source_host": str(source.get("server_host") or "").lower(),
        "_has_trace": any(name in manifest for name in ("trajectory.jsonl", "session.json"))
        or (run_dir / "trajectory.jsonl").is_file()
        or (run_dir / "session.json").is_file(),
        "_run_dir": run_dir,
        "_tool_calls": tool_calls,
        "_tool_call_count": _int_value(audit.get("tool_call_count")) or len(tool_calls),
        "_findings": findings,
        "_has_audit": has_audit,
        "_judge_reliability": judge_reliability,
        "_evaluation_status": evaluation_status,
    }


def _scan_runs(settings: Settings) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    rows: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    job_cache: Dict[str, Optional[Dict[str, Any]]] = {}
    for run_dir in _iter_run_dirs(settings):
        try:
            rows.append(_scan_one_run(run_dir, settings, job_cache))
        except Exception as exc:  # one bad run must never abort the scan
            errors.append(
                {
                    "run_id": run_dir.name,
                    "stage": "scan",
                    "message": "run aggregation failed: %s" % type(exc).__name__,
                }
            )
    return rows, errors


def _matches_agent(row: Dict[str, Any], agent_id: str, endpoint_host: str) -> bool:
    ref = row.get("agent_id")
    if ref:
        return ref == agent_id
    # Legacy run without run.json/agent_ref: fall back to the source host of a
    # configured agent. The host is a match key only, never a display name.
    if endpoint_host and row.get("_source_host"):
        return row["_source_host"] == endpoint_host
    return False


def _in_range(row: Dict[str, Any], range_key: str, now: datetime) -> bool:
    days = RANGE_DAYS[range_key]
    if days is None:
        return True
    ts = row.get("_effective_ts")
    if ts is None:
        return False
    return ts >= now - timedelta(days=days)


def _sort_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (row.get("_effective_ts") or datetime.min.replace(tzinfo=timezone.utc), row["run_id"]),
        reverse=True,
    )


def _public_row(row: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


# ---------------------------------------------------------------------------
# LLM call extraction (models card / token & cost KPIs)
# ---------------------------------------------------------------------------


def _llm_calls_for_run(run_dir: Path) -> List[Dict[str, Any]]:
    raw: List[Any] = []
    payload = _read_json(run_dir / "llm_calls.json")
    if payload and isinstance(payload.get("llm_calls"), list):
        raw.extend(payload["llm_calls"])
    if not raw:
        summary = _read_json(run_dir / "summary.json") or {}
        value = summary.get("llm_calls") or summary.get("llmCalls")
        if isinstance(value, list):
            raw.extend(value)
    return [item for item in raw if isinstance(item, dict)]


def _normalize_llm_call(call: Dict[str, Any]) -> Dict[str, Any]:
    usage = call.get("tokenUsage") or call.get("token_usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = _first_int(usage, "inputTokens", "input_tokens", "input", "prompt_tokens")
    output_tokens = _first_int(usage, "outputTokens", "output_tokens", "output", "completion_tokens")
    total_tokens = _first_int(usage, "totalTokens", "total_tokens", "total", "tokens")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens

    latency_ms: Optional[float] = None
    explicit_ms = call.get("latencyMs") or call.get("latency_ms")
    if isinstance(explicit_ms, (int, float)) and not isinstance(explicit_ms, bool):
        latency_ms = max(float(explicit_ms), 0.0)
    else:
        seconds = call.get("latency") or call.get("totalLatency") or call.get("total_latency")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            latency_ms = max(float(seconds), 0.0) * 1000.0
        else:
            start = _parse_iso(call.get("startTime") or call.get("start_time"))
            end = _parse_iso(call.get("endTime") or call.get("end_time"))
            if start and end and end >= start:
                latency_ms = (end - start).total_seconds() * 1000.0

    cost = call.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        cost = None

    return {
        "model": str(call.get("model") or "") or "unknown",
        "provider": str(call.get("provider") or "") or None,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "latency_ms": latency_ms,
        "status": str(call.get("status") or ""),
        "cost": float(cost) if cost is not None else None,
    }


def _aggregate_models(runs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Group normalized LLM calls by model. Returns (models, totals)."""
    groups: Dict[str, Dict[str, Any]] = {}
    saw_llm_data = False
    for row in runs:
        calls = _llm_calls_for_run(row["_run_dir"])
        if calls:
            saw_llm_data = True
        for call in (_normalize_llm_call(item) for item in calls):
            bucket = groups.setdefault(
                call["model"],
                {
                    "model": call["model"],
                    "provider": call["provider"],
                    "llm_call_count": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "_latencies": [],
                    "error_count": 0,
                    "_cost": 0.0,
                    "_cost_observed": 0,
                    "_cost_estimated": 0,
                },
            )
            if bucket["provider"] is None and call["provider"]:
                bucket["provider"] = call["provider"]
            bucket["llm_call_count"] += 1
            bucket["input_tokens"] += call["input_tokens"]
            bucket["output_tokens"] += call["output_tokens"]
            bucket["total_tokens"] += call["total_tokens"]
            if call["latency_ms"] is not None:
                bucket["_latencies"].append(call["latency_ms"])
            if call["status"] == "failed":
                bucket["error_count"] += 1
            if call["cost"] is not None:
                bucket["_cost"] += call["cost"]
                bucket["_cost_observed"] += 1
            elif call["total_tokens"] > 0:
                bucket["_cost"] += _estimate_model_cost(call["model"], call["input_tokens"], call["output_tokens"])
                bucket["_cost_estimated"] += 1

    models: List[Dict[str, Any]] = []
    totals = {"total_tokens": 0, "total_cost": 0.0, "cost_observed": 0, "cost_estimated": 0, "saw_llm_data": saw_llm_data}
    for model, bucket in sorted(groups.items()):
        latencies = bucket.pop("_latencies")
        cost = round(bucket.pop("_cost"), 6)
        observed = bucket.pop("_cost_observed")
        estimated = bucket.pop("_cost_estimated")
        if observed and not estimated:
            cost_source = "observed"
        elif observed or estimated:
            cost_source = "estimated" if estimated else "observed"
        else:
            cost_source = "missing"
        if cost_source == "missing":
            cost_value: Optional[float] = None
        else:
            cost_value = cost
        totals["total_tokens"] += bucket["total_tokens"]
        totals["cost_observed"] += observed
        totals["cost_estimated"] += estimated
        if cost_value is not None:
            totals["total_cost"] += cost_value
        models.append(
            {
                "model": model,
                "provider": bucket["provider"],
                "llm_call_count": bucket["llm_call_count"],
                "input_tokens": bucket["input_tokens"],
                "output_tokens": bucket["output_tokens"],
                "total_tokens": bucket["total_tokens"],
                "average_latency_ms": round(sum(latencies) / len(latencies), 1) if latencies else None,
                "error_count": bucket["error_count"],
                "cost": cost_value,
                "cost_source": cost_source,
            }
        )
    totals["total_cost"] = round(totals["total_cost"], 6)
    return models, totals


# ---------------------------------------------------------------------------
# tools card
# ---------------------------------------------------------------------------


def _aggregate_tools(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    groups: Dict[str, Dict[str, int]] = {}
    for row in runs:
        for call in row.get("_tool_calls") or []:
            name = str(call.get("tool") or "unknown")
            bucket = groups.setdefault(name, {"call_count": 0, "success_count": 0, "error_count": 0, "dangerous_count": 0})
            bucket["call_count"] += 1
            if call.get("status") in {"error", "missing", "failed"}:
                bucket["error_count"] += 1
            else:
                bucket["success_count"] += 1
            if name in DANGEROUS_TOOLS:
                bucket["dangerous_count"] += 1
    return [
        {
            "tool_name": name,
            "call_count": bucket["call_count"],
            "success_count": bucket["success_count"],
            "error_count": bucket["error_count"],
            # Tool error rate is a per-tool-call ratio — deliberately separate
            # from the run failure rate KPI.
            "error_rate": round(bucket["error_count"] / bucket["call_count"], 4) if bucket["call_count"] else None,
            "dangerous_count": bucket["dangerous_count"],
            "audit_source": "deterministic",
        }
        for name, bucket in sorted(groups.items())
    ]


# ---------------------------------------------------------------------------
# trends / KPIs / health
# ---------------------------------------------------------------------------


def _traces_per_day(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[str, Dict[str, int]] = {}
    for row in runs:
        ts = row.get("_effective_ts")
        if ts is None:
            continue
        date = ts.astimezone(timezone.utc).date().isoformat()
        bucket = buckets.setdefault(
            date,
            {"run_count": 0, "completed_count": 0, "failed_count": 0, "timed_out_count": 0, "cancelled_count": 0},
        )
        bucket["run_count"] += 1
        status = row.get("status")
        if status == "completed":
            bucket["completed_count"] += 1
        elif status == "failed":
            bucket["failed_count"] += 1
        elif status == "timed_out":
            bucket["timed_out_count"] += 1
        elif status == "cancelled":
            bucket["cancelled_count"] += 1
    return [
        {
            "date": date,
            "run_count": bucket["run_count"],
            "completed_count": bucket["completed_count"],
            "failed_count": bucket["failed_count"],
            "timed_out_count": bucket["timed_out_count"],
            "cancelled_count": bucket["cancelled_count"],
            # error_count = failed + timed_out (explicitly documented; cancelled
            # and unknown are never counted as errors)
            "error_count": bucket["failed_count"] + bucket["timed_out_count"],
        }
        for date, bucket in sorted(buckets.items())
    ]


def _build_kpis(runs: List[Dict[str, Any]], totals: Dict[str, Any]) -> Dict[str, Any]:
    run_count = len(runs)
    trace_count = sum(1 for row in runs if row.get("_has_trace"))
    error_runs = sum(1 for row in runs if row.get("status") in _ERROR_RUN_STATUSES)
    scores = [row["completion_score"] for row in runs if isinstance(row.get("completion_score"), (int, float))]
    total_valid = sum(
        _int_value(row.get("_judge_reliability", {}).get("total_evidence_count"))
        for row in runs
        if row.get("_judge_reliability")
    )
    valid_evidence = sum(
        _int_value(row.get("_judge_reliability", {}).get("valid_evidence_count"))
        for row in runs
        if row.get("_judge_reliability")
    )
    if totals["cost_observed"] or totals["cost_estimated"]:
        total_cost: Optional[float] = totals["total_cost"]
        total_cost_source = "observed" if not totals["cost_estimated"] else "estimated"
    else:
        total_cost = None
        total_cost_source = "missing"
    return {
        # trace_count: runs that produced trajectory/session evidence;
        # run_count: all runs attributed to the agent in range.
        "trace_count": trace_count,
        "run_count": run_count,
        "error_rate": round(error_runs / run_count, 4) if run_count else None,
        "completion_score": round(sum(scores) / len(scores), 1) if scores else None,
        "token_count": totals["total_tokens"] if totals["saw_llm_data"] else None,
        "total_cost": total_cost,
        "total_cost_source": total_cost_source,
        "judge_evidence_coverage": round(valid_evidence / total_valid, 4) if total_valid else None,
    }


def _build_health(runs: List[Dict[str, Any]], tools: List[Dict[str, Any]], kpis: Dict[str, Any]) -> Dict[str, Any]:
    scores = [row["completion_score"] for row in runs if isinstance(row.get("completion_score"), (int, float))]
    if scores:
        avg = sum(scores) / len(scores)
        status = "pass" if avg >= 80 else "partial" if avg >= 60 else "fail"
        task_completion = _health_metric(round(avg, 1), status, "observed", "基于 %d 个有效裁判评分的平均值" % len(scores))
    else:
        task_completion = _health_metric(None, "unknown", "missing", "时间范围内没有可用的裁判评分")

    tool_calls = sum(item["call_count"] for item in tools)
    tool_errors = sum(item["error_count"] for item in tools)
    if tool_calls:
        stability = round((tool_calls - tool_errors) / tool_calls * 100, 1)
        status = "pass" if stability >= 95 else "partial" if stability >= 80 else "fail"
        tool_stability = _health_metric(stability, status, "observed", "工具调用成功率（确定性审计），与 Run 失败率分开统计")
    else:
        tool_stability = _health_metric(None, "unknown", "missing", "时间范围内没有工具调用数据")

    durations = [row["duration_ms"] for row in runs if isinstance(row.get("duration_ms"), (int, float))]
    if durations:
        avg_ms = sum(durations) / len(durations)
        status = "pass" if avg_ms <= 60_000 else "partial" if avg_ms <= 180_000 else "fail"
        performance = _health_metric(round(avg_ms), status, "observed", "平均 Run 时长（毫秒），阈值 60s/180s")
    else:
        performance = _health_metric(None, "unknown", "missing", "时间范围内没有可用的运行时长数据")

    if kpis["total_cost"] is not None:
        completed = sum(1 for row in runs if row.get("status") == "completed")
        per_run = round(kpis["total_cost"] / completed, 6) if completed else None
        source = kpis["total_cost_source"]
        if per_run is None:
            cost_efficiency = _health_metric(None, "unknown", source, "有成本数据但没有已完成的 Run 可分摊")
        else:
            status = "pass" if per_run <= 0.5 else "partial" if per_run <= 2.0 else "fail"
            detail = "每完成一次 Run 的平均成本（%s），阈值 0.5/2.0" % ("估算" if source == "estimated" else "观测")
            cost_efficiency = _health_metric(per_run, status, source, detail)
    else:
        cost_efficiency = _health_metric(None, "unknown", "missing", "没有可靠的成本来源")

    total_findings = sum(row["deterministic_finding_count"] for row in runs)
    high_severity = sum(row["high_severity_finding_count"] for row in runs)
    has_audit_data = any(row.get("_has_audit") for row in runs)
    if has_audit_data:
        finding_status = "fail" if high_severity else "partial" if total_findings else "pass"
        deterministic = _health_metric(total_findings, finding_status, "observed", "确定性审计 finding 总数（不覆盖、不伪造）")
        severity_status = "fail" if high_severity else "pass"
        severity = _health_metric(high_severity, severity_status, "observed", "severity=error 的高危 finding 数量")
    else:
        deterministic = _health_metric(None, "unknown", "missing", "时间范围内没有审计数据")
        severity = _health_metric(None, "unknown", "missing", "时间范围内没有审计数据")

    return {
        "task_completion": task_completion,
        "tool_stability": tool_stability,
        "performance": performance,
        "cost_efficiency": cost_efficiency,
        "deterministic_finding_count": deterministic,
        "high_severity_finding_count": severity,
    }


# ---------------------------------------------------------------------------
# public API used by the routes
# ---------------------------------------------------------------------------


def _agent_header(
    agent_id: str,
    config: Optional[Dict[str, Any]],
    rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    ordered = _sort_rows(rows)
    latest = ordered[0] if ordered else None
    metadata = config.get("metadata") if config else None
    environment = None
    if isinstance(metadata, dict) and metadata.get("environment"):
        environment = str(metadata.get("environment"))
    source_host = _endpoint_host(config.get("endpoint")) if config else None
    if not source_host and latest:
        source_host = latest.get("_source_host") or None
    if config is not None and config.get("name"):
        name = str(config.get("name"))
    elif agent_id.startswith("host:"):
        name = agent_id[len("host:") :]
    else:
        name = agent_id
    return {
        "agent_id": agent_id,
        "name": name,
        "type": str(config.get("adapter")) if config else None,
        "environment": environment,
        "source_host": source_host or None,
        "derived": config is None,
        "created_at": None,
        "last_run_at": (latest.get("started_at") or latest.get("finished_at")) if latest else None,
        "last_run_status": latest.get("status") if latest else None,
    }


def _host_agent_id(source_host: str) -> str:
    """Synthetic id for a host-derived agent (last-resort identity)."""
    return "host:%s" % source_host


def resolve_agent(agent_id: str, settings: Settings) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, str]]]:
    """Resolve (config, matched run rows, scan errors) for one agent.

    ``config=None`` with non-empty rows means a derived agent (from run data);
    ``config=None`` with no rows means the agent does not exist at all.
    ``host:<hostname>`` ids address the last-resort identity in the
    attribution chain: legacy runs without ``agent_ref`` grouped by their
    ``source.json`` host.
    """
    agent_id = validate_agent_id(agent_id)
    config = platform_store.get_agent(agent_id, settings)
    rows, errors = _scan_runs(settings)
    if agent_id.startswith("host:"):
        host = agent_id[len("host:") :].lower()
        if not host:
            return None, [], errors
        matched = [row for row in rows if not row.get("agent_id") and row.get("_source_host") == host]
        if not matched:
            return None, [], errors
        return None, matched, errors
    endpoint_host = _endpoint_host(config.get("endpoint")) if config else ""
    matched = [row for row in rows if _matches_agent(row, agent_id, endpoint_host)]
    if config is None and not matched:
        return None, [], errors
    return config, matched, errors


def get_agent_detail(agent_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    config, rows, errors = resolve_agent(agent_id, settings)
    if config is None and not rows:
        return None
    detail: Dict[str, Any] = {
        "agent": _agent_header(agent_id, config, rows),
        "capabilities": list(config.get("capabilities") or []) if config else [],
        "run_count": len(rows),
        "partial": bool(errors),
        "errors": errors,
    }
    if config is not None:
        detail["config"] = {
            "agent_id": config.get("agent_id"),
            "name": config.get("name"),
            "version": config.get("version"),
            "adapter": config.get("adapter"),
            "endpoint": config.get("endpoint"),
            "capabilities": list(config.get("capabilities") or []),
            "max_concurrency": config.get("max_concurrency"),
            "config_hash": config.get("config_hash"),
        }
    else:
        detail["config"] = None
    return detail


def get_agent_overview(agent_id: str, range_key: str, settings: Settings, recent_limit: int = 5) -> Optional[Dict[str, Any]]:
    config, rows, errors = resolve_agent(agent_id, settings)
    if config is None and not rows:
        return None
    now = datetime.now(timezone.utc)
    scoped = [row for row in rows if _in_range(row, range_key, now)]
    ordered = _sort_rows(scoped)

    models, totals = _aggregate_models(scoped)
    tools = _aggregate_tools(scoped)
    kpis = _build_kpis(scoped, totals)
    health = _build_health(scoped, tools, kpis)

    return {
        "agent": _agent_header(agent_id, config, rows),
        "models": models,
        "tools": tools,
        "traces": _traces_per_day(scoped),
        "kpis": kpis,
        "health": health,
        "recent_runs": [_public_row(row) for row in ordered[: max(recent_limit, 0)]],
        "data_range": range_key,
        "partial": bool(errors),
        "errors": errors,
    }


def list_agent_runs(
    agent_id: str,
    range_key: str,
    status_filter: str,
    settings: Settings,
    *,
    limit: int = DEFAULT_RUN_LIMIT,
    offset: int = 0,
) -> Optional[Dict[str, Any]]:
    config, rows, errors = resolve_agent(agent_id, settings)
    if config is None and not rows:
        return None
    now = datetime.now(timezone.utc)
    scoped = [row for row in rows if _in_range(row, range_key, now)]
    if status_filter == "stale":
        scoped = [row for row in scoped if row.get("stale")]
    elif status_filter != "all":
        scoped = [row for row in scoped if row.get("status") == status_filter]
    ordered = _sort_rows(scoped)
    limit = min(max(int(limit), 1), MAX_RUN_LIMIT)
    offset = max(int(offset), 0)
    return {
        "items": [_public_row(row) for row in ordered[offset : offset + limit]],
        "total": len(ordered),
        "range": range_key,
        "status": status_filter,
        "limit": limit,
        "offset": offset,
        "partial": bool(errors),
        "errors": errors,
    }


def list_agents_with_stats(settings: Settings) -> Dict[str, Any]:
    """Enrich the existing agent config list with run stats, plus derived agents.

    ``agents`` keeps its original config semantics (additive ``derived`` and
    ``stats`` keys only); agents discoverable only from run data appear under
    ``derived_agents`` and are never persisted as configs.
    """
    configs = platform_store.list_agents(settings)
    rows, errors = _scan_runs(settings)
    by_ref: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        ref = row.get("agent_id")
        if ref:
            by_ref.setdefault(ref, []).append(row)

    configured_ids = {str(config.get("agent_id")) for config in configs}
    enriched: List[Dict[str, Any]] = []
    for config in configs:
        agent_id = str(config.get("agent_id") or "")
        agent_rows = by_ref.get(agent_id, [])
        if not agent_rows:
            host = _endpoint_host(config.get("endpoint"))
            if host:
                agent_rows = [row for row in rows if not row.get("agent_id") and row.get("_source_host") == host]
        stats = _stats_for(agent_rows)
        enriched.append({**config, "derived": False, "stats": stats})

    derived: List[Dict[str, Any]] = []
    for ref_id, agent_rows in sorted(by_ref.items()):
        if ref_id in configured_ids:
            continue
        stats = _stats_for(agent_rows)
        latest = _sort_rows(agent_rows)[0] if agent_rows else None
        derived.append(
            {
                "agent_id": ref_id,
                "name": ref_id,
                "derived": True,
                "source_host": (latest.get("_source_host") or None) if latest else None,
                "stats": stats,
            }
        )

    # Last-resort identity: legacy runs without agent_ref, grouped by source
    # host. Hosts claimed by a configured agent's endpoint stay with that
    # agent; the host is shown via source_host, never invented as a name.
    configured_hosts = {_endpoint_host(config.get("endpoint")) for config in configs}
    configured_hosts.discard("")
    by_host: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        host = row.get("_source_host") or ""
        if row.get("agent_id") or not host or host in configured_hosts or host == "unknown":
            continue
        by_host.setdefault(host, []).append(row)
    for host, host_rows in sorted(by_host.items()):
        derived.append(
            {
                "agent_id": _host_agent_id(host),
                "name": host,
                "derived": True,
                "source_host": host,
                "stats": _stats_for(host_rows),
            }
        )
    return {"agents": enriched, "derived_agents": derived, "partial": bool(errors), "errors": errors}


def _stats_for(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = _sort_rows(rows)
    latest = ordered[0] if ordered else None
    return {
        "run_count": len(rows),
        "last_run_at": (latest.get("started_at") or latest.get("finished_at")) if latest else None,
        "last_run_status": latest.get("status") if latest else None,
    }
