from __future__ import annotations

import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Response

from agentloop.cli import _run_dir, _source_meta, _write_run_outputs
from agentloop.agents.schemas import render_dimensions_markdown, render_reliability_markdown
from agentloop.agents.tools import build_chart_data, list_runs, load_audit, load_run_summary
from agentloop.client import AgentClient
from backend.app.config import get_settings, resolve_run_path


router = APIRouter(prefix="/api")


@router.get("/runs")
def get_runs() -> dict[str, Any]:
    settings = get_settings()
    runs = [_enrich_run_item(item) for item in list_runs(str(settings.runs_dir))]
    runs.sort(key=lambda item: (item.get("server_host") or "\uffff", item.get("name") or ""))
    return {"runs": runs}


@router.get("/runs/batch-summary")
def get_batch_summary() -> dict[str, Any]:
    settings = get_settings()
    runs = []
    root_causes: Counter[str] = Counter()
    total_tool_calls = 0
    failed_tool_calls = 0
    successful_runs = 0

    for item in list_runs(str(settings.runs_dir)):
        run_id = str(item.get("name") or "")
        if not run_id:
            continue
        path = Path(str(item.get("path") or settings.runs_dir / run_id))
        run = _build_batch_run(path, run_id)
        runs.append(run)
        total_tool_calls += int(run["tool_call_count"])
        failed_tool_calls += int(run["tool_error_count"])
        if run["is_success"]:
            successful_runs += 1
        for label, count in run["root_cause_counts"].items():
            root_causes[label] += count

    total_runs = len(runs)
    runs.sort(key=lambda run: (run.get("server_host") or "\uffff", run["is_success"], run["run_id"]))
    return {
        "total_runs": total_runs,
        "successful_runs": successful_runs,
        "failed_runs": total_runs - successful_runs,
        "success_rate": _ratio(successful_runs, total_runs),
        "tool_error_rate": _ratio(failed_tool_calls, total_tool_calls),
        "average_tool_calls": _ratio(total_tool_calls, total_runs),
        "total_tool_calls": total_tool_calls,
        "tool_error_count": failed_tool_calls,
        "root_causes": [
            {"label": label, "count": count}
            for label, count in root_causes.most_common(8)
        ],
        "runs": [
            {key: value for key, value in run.items() if key != "root_cause_counts"}
            for run in runs
        ],
    }


@router.post("/runs/import-session")
def import_remote_session(payload: dict[str, Any]) -> dict[str, Any]:
    base_url = str(payload.get("base_url") or "").strip().rstrip("/")
    user_id = str(payload.get("user_id") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    token = str(payload.get("token") or "").strip() or None
    timeout = _float_or_default(payload.get("timeout"), 60.0)
    _validate_import_request(base_url, user_id, session_id)

    settings = get_settings()
    try:
        session_response = AgentClient(base_url, user_id, token=token, timeout=timeout).fetch_session(session_id)
        run_dir = _run_dir(settings.runs_dir, session_id)
        _write_run_outputs(run_dir, session_response, source=_source_meta(base_url, user_id))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "run": _enrich_run_item(_run_item(run_dir)),
        "batch_summary": get_batch_summary(),
    }


@router.delete("/runs/{run_id}")
def delete_run(run_id: str) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    shutil.rmtree(path)
    return {"deleted": run_id, "runs": get_runs()["runs"]}


@router.get("/runs/{run_id}")
def get_run(run_id: str) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    return {"run_id": run_id, "summary": load_run_summary(str(path))}


@router.get("/runs/{run_id}/trajectory")
def get_run_trajectory(run_id: str) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    return {"run_id": run_id, "messages": _read_jsonl(path / "trajectory.jsonl")}


@router.get("/runs/{run_id}/audit")
def get_run_audit(run_id: str) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    return load_audit(str(path))


@router.get("/runs/{run_id}/judge")
def get_run_judge(run_id: str) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    judge_path = path / "agent_judge.json"
    if not judge_path.exists():
        return {"status": "missing"}
    return _normalize_judge_charts(_read_json(judge_path))


@router.get("/runs/{run_id}/charts")
def get_run_charts(run_id: str) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    chart_path = path / "chart_data.json"
    if chart_path.exists():
        return _read_json(chart_path)
    return build_chart_data(str(path))


@router.get("/runs/{run_id}/runtime-metrics")
def get_run_runtime_metrics(run_id: str) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    return _build_runtime_metrics(path, run_id)


@router.get("/runs/{run_id}/export.md")
def export_run_markdown(run_id: str) -> Response:
    path = _run_path_or_404(run_id)
    filename = run_id.replace('"', "_")
    return Response(
        content=_render_run_markdown_report(run_id, path),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{filename}-evaluation.md"'},
    )


@router.get("/runtime-trends")
def get_runtime_trends() -> dict[str, Any]:
    settings = get_settings()
    metrics = []
    for item in list_runs(str(settings.runs_dir)):
        run_id = str(item.get("name") or "")
        if not run_id:
            continue
        path = Path(str(item.get("path") or settings.runs_dir / run_id))
        metrics.append(_build_runtime_metrics(path, run_id))
    metrics.sort(key=lambda item: str(item.get("startTime") or item.get("traceId") or ""), reverse=True)
    recent = metrics[:100]
    total = len(recent)
    return {
        "sampleSize": total,
        "successRate": _ratio(sum(1 for item in recent if item.get("status") == "success"), total),
        "averageDuration": _average(item.get("duration") for item in recent),
        "averageTokens": _average(item.get("llmMetrics", {}).get("totalTokens") for item in recent),
        "averageCost": _average(item.get("costMetrics", {}).get("totalCost") for item in recent),
    }


def _run_path_or_404(run_id: str) -> Path:
    try:
        return resolve_run_path(run_id, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _read_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="%s must contain a JSON object" % path.name)
    return data


def _normalize_judge_charts(judge: dict[str, Any]) -> dict[str, Any]:
    findings = [item for item in judge.get("findings") or [] if isinstance(item, dict)]
    charts = dict(judge.get("charts") or {})
    charts["finding_severity"] = [
        {"severity": label, "count": count}
        for label, count in Counter(str(item.get("severity") or "") for item in findings if item.get("severity")).items()
    ]
    return {**judge, "charts": charts}


def _render_run_markdown_report(run_id: str, path: Path) -> str:
    summary = _read_optional_json(path / "summary.json")
    audit = _read_optional_json(path / "audit.json")
    judge = _read_optional_json(path / "agent_judge.json")
    charts = _read_optional_json(path / "chart_data.json")
    if not charts:
        charts = build_chart_data(str(path))
    runtime = _build_runtime_metrics(path, run_id)
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    sections = [
        f"# Evaluation Report: {run_id}",
        "",
        f"Generated: {generated_at}",
        "",
        "## Run Metadata",
        _markdown_table(
            ["Field", "Value"],
            [
                ["Run ID", run_id],
                ["Session ID", summary.get("session_id") or runtime.get("traceId") or ""],
                ["Model", summary.get("model") or ""],
                ["Server Host", _source_from_run(path, summary).get("server_host") or ""],
                ["Messages", summary.get("message_count") or audit.get("message_count") or runtime.get("steps") or 0],
                ["Tool Calls", audit.get("tool_call_count") or runtime.get("toolMetrics", {}).get("calls") or 0],
            ],
        ),
        "",
        "## Runtime Health",
        _markdown_table(
            ["Metric", "Value"],
            [
                ["Status", runtime.get("status") or ""],
                ["Duration", _markdown_duration(runtime.get("duration"))],
                ["Total Steps", runtime.get("executionMetrics", {}).get("total_steps") or 0],
                ["Completed Steps", runtime.get("executionMetrics", {}).get("completed_steps") or 0],
                ["Failed Steps", runtime.get("executionMetrics", {}).get("failed_steps") or 0],
                ["Health Score", runtime.get("healthScore", {}).get("total") or 0],
            ],
        ),
        "",
        "## LLM Metrics",
        _markdown_table(
            ["Metric", "Value"],
            [
                ["Calls", runtime.get("llmMetrics", {}).get("calls") or 0],
                ["Input Tokens", runtime.get("llmMetrics", {}).get("inputTokens") or 0],
                ["Output Tokens", runtime.get("llmMetrics", {}).get("outputTokens") or 0],
                ["Total Tokens", runtime.get("llmMetrics", {}).get("totalTokens") or 0],
                ["Average Latency", _markdown_duration(runtime.get("llmMetrics", {}).get("averageLatency"))],
                ["Estimated Cost", _markdown_cost(runtime.get("costMetrics", {}).get("totalCost"))],
            ],
        ),
        "",
        "## Tool Metrics",
        _markdown_table(
            ["Metric", "Value"],
            [
                ["Tool Calls", runtime.get("toolMetrics", {}).get("calls") or 0],
                ["Success", runtime.get("toolMetrics", {}).get("success") or 0],
                ["Failed", runtime.get("toolMetrics", {}).get("failed") or 0],
                ["Timeout", runtime.get("toolMetrics", {}).get("timeout") or 0],
                ["Average Tool Latency", _markdown_duration(runtime.get("toolMetrics", {}).get("averageLatency"))],
            ],
        ),
        "",
        _render_judge_markdown(judge),
        "",
        _render_findings_markdown("## Deterministic Findings", audit.get("findings") or []),
        "",
        _render_findings_markdown("## Runtime Errors", runtime.get("errors") or []),
        "",
        _render_chart_summary(charts),
        "",
    ]
    return "\n".join(sections)


def _render_judge_markdown(judge: dict[str, Any]) -> str:
    if not judge:
        return "## DeepAgent Judge\n\nDeepAgent analysis has not been generated."
    rows = [
        ["Status", judge.get("overall_status") or judge.get("status") or ""],
        ["Completion Score", judge.get("completion_score") or ""],
        ["Confidence", judge.get("confidence") or ""],
    ]
    return "\n".join(
        [
            "## DeepAgent Judge",
            _markdown_table(["Field", "Value"], rows),
            "",
            "### Summary",
            str(judge.get("summary") or ""),
            "",
            render_dimensions_markdown(judge.get("dimensions")),
            "",
            render_reliability_markdown(judge.get("judge_reliability")),
            "",
            _render_findings_markdown("### Judge Findings", judge.get("findings") or []),
        ]
    )


def _render_findings_markdown(title: str, findings: Any) -> str:
    if not isinstance(findings, list) or not findings:
        return f"{title}\n\nNo findings."
    rows = []
    for item in findings:
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                item.get("severity") or item.get("type") or "",
                item.get("title") or item.get("category") or item.get("code") or item.get("type") or "",
                item.get("recommendation") or item.get("message") or "",
            ]
        )
    if not rows:
        return f"{title}\n\nNo findings."
    return f"{title}\n\n{_markdown_table(['Severity', 'Category', 'Message'], rows)}"


def _render_chart_summary(charts: dict[str, Any]) -> str:
    tool_distribution = charts.get("tool_distribution") or []
    severity = charts.get("finding_severity") or charts.get("deterministic_finding_severity") or []
    rows = [
        ["Tool Distribution Items", len(tool_distribution) if isinstance(tool_distribution, list) else 0],
        ["Finding Severity Items", len(severity) if isinstance(severity, list) else 0],
    ]
    return "## Chart Data Summary\n\n%s" % _markdown_table(["Metric", "Value"], rows)


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    rendered = [
        "| %s |" % " | ".join(_markdown_cell(header) for header in headers),
        "| %s |" % " | ".join("---" for _ in headers),
    ]
    for row in rows:
        rendered.append("| %s |" % " | ".join(_markdown_cell(value) for value in row))
    return "\n".join(rendered)


def _markdown_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("|", "\\|")


def _markdown_duration(value: Any) -> str:
    parsed = _float_or_default(value, 0)
    if not parsed:
        return "0s"
    return "%.3fs" % parsed


def _markdown_cost(value: Any) -> str:
    return "$%.6f" % _float_or_default(value, 0)


def _build_runtime_metrics(path: Path, run_id: str) -> dict[str, Any]:
    summary = _read_optional_json(path / "summary.json")
    audit = _read_optional_json(path / "audit.json")
    judge = _read_optional_json(path / "agent_judge.json")
    trajectory = _read_jsonl(path / "trajectory.jsonl")
    tool_calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    findings = [item for item in audit.get("findings") or [] if isinstance(item, dict)]
    judge_findings = [item for item in judge.get("findings") or [] if isinstance(item, dict)]
    all_findings = findings + judge_findings

    total_steps = _int_or_default(audit.get("message_count"), _int_or_default(summary.get("message_count"), len(trajectory)))
    tool_call_count = _int_or_default(audit.get("tool_call_count"), _int_or_default(summary.get("tool_call_count"), len(tool_calls)))
    failed_tool_calls = sum(1 for call in tool_calls if call.get("status") in {"error", "missing", "failed"})
    failed_steps = failed_tool_calls + sum(1 for item in findings if str(item.get("severity") or "").lower() == "error")
    llm_calls = _int_or_default(summary.get("assistant_count"), _int_or_default(audit.get("assistant_count"), 0))
    input_tokens = _first_int(summary, "input_tokens", "inputTokens", "prompt_tokens")
    output_tokens = _first_int(summary, "output_tokens", "outputTokens", "completion_tokens")
    total_tokens = _first_int(summary, "total_tokens", "totalTokens", "tokens")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    if total_tokens and input_tokens == 0 and output_tokens == 0:
        input_tokens = int(total_tokens * 0.78)
        output_tokens = total_tokens - input_tokens

    status = _runtime_status(judge, findings, trajectory)
    start_ms, end_ms = _run_bounds(trajectory)
    duration = round(max((end_ms - start_ms) / 1000.0, 0.0), 3) if start_ms and end_ms else float(max(total_steps - 1, 0))
    error_metrics = _error_metrics(all_findings)
    latency = _latency_metrics(duration, llm_calls, tool_call_count)
    tool_latency = _tool_latency(duration, tool_call_count)
    health = _health_score(status, tool_call_count, failed_tool_calls, failed_steps, total_steps, duration, total_tokens)
    lifecycle = _lifecycle_stages(status, start_ms, duration, failed_tool_calls, error_metrics)
    llm_calls_detail = _extract_llm_calls(path, run_id, summary, audit, trajectory)
    llm_dashboard = _build_llm_dashboard(llm_calls_detail, run_id, summary)
    llm_summary = llm_dashboard["summary"]
    if llm_summary["calls"]:
        llm_calls = llm_summary["calls"]
        input_tokens = llm_summary["inputTokens"]
        output_tokens = llm_summary["outputTokens"]
        total_tokens = llm_summary["totalTokens"]
        latency = {"average": llm_summary["averageLatency"], "max": llm_summary["maxLatency"]}

    return {
        "traceId": str(summary.get("session_id") or run_id),
        "status": status,
        "startTime": _iso_from_ms(start_ms),
        "endTime": _iso_from_ms(end_ms),
        "duration": duration,
        "lifecycleStages": lifecycle,
        "steps": total_steps,
        "executionMetrics": {
            "total_duration": duration,
            "total_steps": total_steps,
            "completed_steps": max(total_steps - failed_steps, 0),
            "failed_steps": failed_steps,
            "retry_count": _count_matching(all_findings, "retry"),
            "timeout_count": error_metrics["timeout"],
        },
        "llmMetrics": {
            "calls": llm_calls,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
            "averageLatency": latency["average"],
            "maxLatency": latency["max"],
        },
        "llmDashboard": llm_dashboard,
        "toolMetrics": {
            "calls": tool_call_count,
            "success": max(tool_call_count - failed_tool_calls, 0),
            "failed": failed_tool_calls,
            "averageLatency": tool_latency,
            "timeout": error_metrics["timeout"],
        },
        "errorMetrics": error_metrics,
        "costMetrics": {
            "tokenCost": _estimate_token_cost(total_tokens),
            "totalCost": _estimate_token_cost(total_tokens),
        },
        "healthScore": health,
        "performanceBreakdown": _performance_breakdown(duration, llm_calls, tool_call_count),
        "errors": _runtime_errors(all_findings),
    }


def _extract_llm_calls(
    path: Path,
    run_id: str,
    summary: dict[str, Any],
    audit: dict[str, Any],
    trajectory: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw_calls: list[Any] = []
    for source in (summary, audit, _read_optional_json(path / "llm_calls.json")):
        value = source.get("llm_calls") or source.get("llmCalls")
        if isinstance(value, list):
            raw_calls.extend(value)
    calls = [
        _normalize_llm_call(call, index, run_id, summary)
        for index, call in enumerate(raw_calls, start=1)
        if isinstance(call, dict)
    ]
    if calls:
        return calls
    return _llm_calls_from_trajectory(trajectory, run_id, summary)


def _normalize_llm_call(call: dict[str, Any], index: int, run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    token_usage = call.get("tokenUsage") or call.get("token_usage") or {}
    if not isinstance(token_usage, dict):
        token_usage = {}
    prompt = call.get("prompt") or {}
    if not isinstance(prompt, dict):
        prompt = {}
    quality = call.get("quality") or call.get("qualityMetrics") or call.get("quality_metrics") or {}
    if not isinstance(quality, dict):
        quality = {}

    start_time = str(_pick(call, "startTime", "start_time") or "")
    end_time = str(_pick(call, "endTime", "end_time") or "")
    latency = _llm_latency(call, start_time, end_time)
    input_tokens = _first_int(token_usage, "inputTokens", "input_tokens", "input", "prompt_tokens")
    output_tokens = _first_int(token_usage, "outputTokens", "output_tokens", "output", "completion_tokens")
    total_tokens = _first_int(token_usage, "totalTokens", "total_tokens", "total", "tokens")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    model = str(_pick(call, "model") or summary.get("model") or "unknown")
    cost = _float_or_default(_pick(call, "cost"), -1)
    if cost < 0:
        cost = _estimate_model_cost(model, input_tokens, output_tokens)
    status = str(_pick(call, "status") or "success").lower()
    error = _normalize_error(call.get("error"))
    if error and status == "success":
        status = "failed"

    return {
        "callId": str(_pick(call, "callId", "call_id", "id") or f"llm-{index}"),
        "traceId": str(_pick(call, "traceId", "trace_id") or summary.get("session_id") or run_id),
        "name": str(_pick(call, "name", "stage", "role") or f"LLM Call #{index}"),
        "model": model,
        "provider": str(_pick(call, "provider") or _provider_from_model(model)),
        "temperature": _optional_float(_pick(call, "temperature")),
        "maxTokens": _optional_int(_pick(call, "maxTokens", "max_tokens")),
        "startTime": start_time,
        "endTime": end_time,
        "latency": {
            "queueTime": round(max(_float_or_default(_pick(call, "queueTime", "queue_time"), 0), 0), 3),
            "generationTime": round(max(_float_or_default(_pick(call, "generationTime", "generation_time"), latency), 0), 3),
            "total": latency,
        },
        "tokenUsage": {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
        },
        "prompt": {
            "system": str(_pick(prompt, "system", "systemPrompt", "system_prompt") or ""),
            "user": str(_pick(prompt, "user", "userPrompt", "user_prompt") or ""),
            "context": str(_pick(prompt, "context") or ""),
            "version": str(_pick(prompt, "version", "promptVersion", "prompt_version") or "unversioned"),
            "systemPromptHash": str(_pick(prompt, "systemPromptHash", "system_prompt_hash") or ""),
            "template": str(_pick(prompt, "template") or ""),
        },
        "response": str(_pick(call, "response", "output", "content") or ""),
        "status": "failed" if status in {"failed", "error", "timeout"} else "success",
        "error": error,
        "cost": round(cost, 6),
        "quality": _normalize_quality(quality, call),
    }


def _llm_calls_from_trajectory(trajectory: list[dict[str, Any]], run_id: str, summary: dict[str, Any]) -> list[dict[str, Any]]:
    assistant_rows = [row for row in trajectory if row.get("role") == "assistant"]
    calls = []
    previous_ms = _timestamp_ms(trajectory[0].get("timestamp")) if trajectory else 0
    model = str(summary.get("model") or "unknown")
    summary_input_tokens = _first_int(summary, "input_tokens", "inputTokens", "prompt_tokens")
    summary_output_tokens = _first_int(summary, "output_tokens", "outputTokens", "completion_tokens")
    summary_total_tokens = _first_int(summary, "total_tokens", "totalTokens", "tokens")
    if summary_total_tokens and summary_input_tokens == 0 and summary_output_tokens == 0:
        summary_input_tokens = int(summary_total_tokens * 0.78)
        summary_output_tokens = summary_total_tokens - summary_input_tokens
    call_count = max(len(assistant_rows), 1)
    for index, row in enumerate(assistant_rows, start=1):
        end_ms = _timestamp_ms(row.get("timestamp"))
        start_ms = previous_ms if previous_ms and end_ms and previous_ms <= end_ms else end_ms
        content = str(row.get("content") or row.get("reasoning") or row.get("reasoning_content") or "")
        if summary_total_tokens:
            input_tokens = _split_count(summary_input_tokens, call_count, index)
            output_tokens = _split_count(summary_output_tokens, call_count, index)
        else:
            output_tokens = _rough_token_count(content)
            input_tokens = 0
        total_tokens = input_tokens + output_tokens
        calls.append(
            {
                "callId": f"llm-{index}",
                "traceId": str(summary.get("session_id") or run_id),
                "name": "Tool Planning" if row.get("tool_calls") else "Assistant Response",
                "model": model,
                "provider": _provider_from_model(model),
                "temperature": None,
                "maxTokens": None,
                "startTime": _iso_from_ms(start_ms),
                "endTime": _iso_from_ms(end_ms),
                "latency": {"queueTime": 0, "generationTime": round(max((end_ms - start_ms) / 1000.0, 0), 3), "total": round(max((end_ms - start_ms) / 1000.0, 0), 3)},
                "tokenUsage": {"inputTokens": input_tokens, "outputTokens": output_tokens, "totalTokens": total_tokens},
                "prompt": {"system": "", "user": "", "context": "", "version": "observed", "systemPromptHash": "", "template": ""},
                "response": content,
                "status": "success",
                "error": None,
                "cost": _estimate_model_cost(model, input_tokens, output_tokens),
                "quality": _normalize_quality({}, {"response": content}),
            }
        )
        previous_ms = end_ms
    return calls


def _split_count(total: int, parts: int, index: int) -> int:
    if total <= 0 or parts <= 0:
        return 0
    base = total // parts
    remainder = total % parts
    return base + (1 if index <= remainder else 0)


def _build_llm_dashboard(calls: list[dict[str, Any]], run_id: str, summary: dict[str, Any]) -> dict[str, Any]:
    total_calls = len(calls)
    failed = sum(1 for call in calls if call["status"] == "failed")
    input_tokens = sum(call["tokenUsage"]["inputTokens"] for call in calls)
    output_tokens = sum(call["tokenUsage"]["outputTokens"] for call in calls)
    total_tokens = sum(call["tokenUsage"]["totalTokens"] for call in calls)
    total_cost = round(sum(call["cost"] for call in calls), 6)
    latencies = [call["latency"]["total"] for call in calls]
    models = sorted({call["model"] for call in calls if call["model"]})

    return {
        "summary": {
            "traceId": str(summary.get("session_id") or run_id),
            "calls": total_calls,
            "models": models,
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": total_tokens,
            "averageLatency": _average(latencies),
            "maxLatency": round(max(latencies), 3) if latencies else 0,
            "estimatedCost": total_cost,
            "errorRate": _ratio(failed, total_calls),
        },
        "timeline": [_llm_timeline_item(call, index) for index, call in enumerate(calls, start=1)],
        "calls": calls,
        "tokenAnalytics": _llm_token_analytics(calls),
        "modelPerformance": _llm_model_performance(calls),
        "promptVersions": _llm_prompt_versions(calls),
        "qualityMetrics": _llm_quality_metrics(calls),
        "errorAnalysis": _llm_error_analysis(calls),
        "filters": ["Agent", "Skill", "Tool", "Trace"],
    }


def _llm_timeline_item(call: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "index": index,
        "callId": call["callId"],
        "name": call["name"],
        "model": call["model"],
        "provider": call["provider"],
        "latency": call["latency"]["total"],
        "tokens": call["tokenUsage"]["totalTokens"],
        "status": call["status"],
        "startTime": call["startTime"],
        "endTime": call["endTime"],
        "error": call["error"],
    }


def _llm_token_analytics(calls: list[dict[str, Any]]) -> dict[str, Any]:
    input_values = [call["tokenUsage"]["inputTokens"] for call in calls]
    output_values = [call["tokenUsage"]["outputTokens"] for call in calls]
    total_values = [call["tokenUsage"]["totalTokens"] for call in calls]
    return {
        "inputTrend": [{"callId": call["callId"], "value": call["tokenUsage"]["inputTokens"]} for call in calls],
        "outputTrend": [{"callId": call["callId"], "value": call["tokenUsage"]["outputTokens"]} for call in calls],
        "totalTrend": [{"callId": call["callId"], "value": call["tokenUsage"]["totalTokens"]} for call in calls],
        "averageInputTokens": round(_average(input_values)),
        "averageOutputTokens": round(_average(output_values)),
        "maxContext": max(total_values) if total_values else 0,
    }


def _llm_model_performance(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for call in calls:
        grouped.setdefault(call["model"], []).append(call)
    rows = []
    for model in sorted(grouped):
        items = grouped[model]
        success = sum(1 for call in items if call["status"] == "success")
        rows.append(
            {
                "model": model,
                "provider": items[0]["provider"],
                "calls": len(items),
                "success": success,
                "successRate": _ratio(success, len(items)),
                "averageLatency": _average(call["latency"]["total"] for call in items),
                "tokens": sum(call["tokenUsage"]["totalTokens"] for call in items),
                "cost": round(sum(call["cost"] for call in items), 6),
            }
        )
    return rows


def _llm_prompt_versions(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for call in calls:
        prompt = call["prompt"]
        key = (prompt["version"], prompt["systemPromptHash"], prompt["template"])
        grouped.setdefault(key, []).append(call)
    rows = []
    for version, prompt_hash, template in sorted(grouped):
        items = grouped[(version, prompt_hash, template)]
        success = sum(1 for call in items if call["status"] == "success")
        rows.append(
            {
                "version": version,
                "systemPromptHash": prompt_hash,
                "template": template,
                "calls": len(items),
                "successRate": _ratio(success, len(items)),
                "averageTokens": round(_average(call["tokenUsage"]["totalTokens"] for call in items)),
                "cost": round(sum(call["cost"] for call in items), 6),
            }
        )
    return rows


def _llm_quality_metrics(calls: list[dict[str, Any]]) -> dict[str, Any]:
    response_lengths = [call["quality"]["responseLength"] for call in calls]
    format_scores = [call["quality"]["formatCompliance"] for call in calls if call["quality"]["formatCompliance"] is not None]
    hallucination = [call["quality"]["hallucinationRisk"] for call in calls if call["quality"]["hallucinationRisk"] is not None]
    following = [call["quality"]["instructionFollowing"] for call in calls if call["quality"]["instructionFollowing"] is not None]
    self_scores = [call["quality"]["selfEvaluationScore"] for call in calls if call["quality"]["selfEvaluationScore"] is not None]
    quality_scores = [call["quality"]["qualityScore"] for call in calls if call["quality"]["qualityScore"] is not None]
    return {
        "qualityScore": int(_average(quality_scores)) if quality_scores else 0,
        "responseLength": round(_average(response_lengths)),
        "formatCompliance": _average(format_scores),
        "hallucinationRisk": _average(hallucination),
        "instructionFollowing": _average(following),
        "selfEvaluationScore": round(_average(self_scores)),
    }


def _llm_error_analysis(calls: list[dict[str, Any]]) -> dict[str, int]:
    metrics = {
        "timeout": 0,
        "rateLimit": 0,
        "contextLengthExceeded": 0,
        "invalidResponse": 0,
        "jsonParseError": 0,
        "contentFilter": 0,
        "other": 0,
    }
    for call in calls:
        error = call.get("error")
        if not error:
            continue
        text = "%s %s" % (error.get("type") or "", error.get("message") or "")
        lowered = text.lower()
        if "timeout" in lowered:
            metrics["timeout"] += 1
        elif "rate" in lowered and "limit" in lowered:
            metrics["rateLimit"] += 1
        elif "context" in lowered and ("overflow" in lowered or "length" in lowered or "exceed" in lowered):
            metrics["contextLengthExceeded"] += 1
        elif "json" in lowered:
            metrics["jsonParseError"] += 1
        elif "filter" in lowered or "safety" in lowered:
            metrics["contentFilter"] += 1
        elif "invalid" in lowered or "response" in lowered:
            metrics["invalidResponse"] += 1
        else:
            metrics["other"] += 1
    return metrics


def _build_batch_run(path: Path, run_id: str) -> dict[str, Any]:
    summary = _read_optional_json(path / "summary.json")
    source = _source_from_run(path, summary)
    audit = _read_optional_json(path / "audit.json")
    judge = _read_optional_json(path / "agent_judge.json")
    tool_calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    tool_call_count = _int_or_default(audit.get("tool_call_count"), len(tool_calls))
    if tool_call_count == 0:
        tool_call_count = _int_or_default(summary.get("tool_call_count"), len(tool_calls))
    tool_error_count = sum(1 for call in tool_calls if call.get("status") in {"error", "missing"})
    status = str(judge.get("overall_status") or "unknown")
    completion_score = _optional_int(judge.get("completion_score"))
    is_success = _is_success(status, completion_score)
    root_cause_counts = _root_cause_counts(judge, audit)
    failure_root_cause = next(iter(root_cause_counts), "")
    return {
        "run_id": run_id,
        "model": summary.get("model") or "",
        "server_host": source.get("server_host") or "",
        "source_base_url": source.get("source_base_url") or "",
        "status": status,
        "completion_score": completion_score,
        "is_success": is_success,
        "tool_call_count": tool_call_count,
        "tool_error_count": tool_error_count,
        "tool_error_rate": _ratio(tool_error_count, tool_call_count),
        "failure_root_cause": "" if is_success else failure_root_cause,
        "root_cause_counts": dict(root_cause_counts),
    }


def _run_item(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "path": str(path),
        "has_summary": (path / "summary.json").exists(),
        "has_audit": (path / "audit.json").exists(),
        "has_trajectory": (path / "trajectory.jsonl").exists(),
        "has_judge": (path / "agent_judge.json").exists(),
    }


def _enrich_run_item(item: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(item.get("path") or ""))
    source = _source_from_run(path)
    return {
        **item,
        "server_host": source.get("server_host") or "",
        "source_base_url": source.get("source_base_url") or "",
        "server_netloc": source.get("server_netloc") or "",
    }


def _source_from_run(path: Path, summary: dict[str, Any] | None = None) -> dict[str, Any]:
    source = _read_optional_json(path / "source.json")
    if source.get("server_host") or source.get("source_base_url"):
        return {
            "server_host": str(source.get("server_host") or ""),
            "source_base_url": str(source.get("source_base_url") or ""),
            "server_netloc": str(source.get("server_netloc") or ""),
        }
    current = summary if summary is not None else _read_optional_json(path / "summary.json")
    return {
        "server_host": str(current.get("server_host") or ""),
        "source_base_url": str(current.get("source_base_url") or ""),
        "server_netloc": str(current.get("server_netloc") or ""),
    }


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _read_json(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _runtime_status(judge: dict[str, Any], findings: list[dict[str, Any]], trajectory: list[dict[str, Any]]) -> str:
    status = str(judge.get("overall_status") or judge.get("status") or "").lower()
    if status in {"success", "failed", "running"}:
        return status
    if status == "partial":
        return "failed"
    if trajectory and not _has_final_assistant(trajectory):
        return "running"
    if any(_is_blocking_runtime_finding(item) for item in findings):
        return "failed"
    return "success"


def _is_blocking_runtime_finding(item: dict[str, Any]) -> bool:
    if str(item.get("severity") or "").lower() not in {"error", "critical"}:
        return False
    code = str(item.get("code") or item.get("category") or "").lower()
    return code not in {"tool_error"}


def _has_final_assistant(trajectory: list[dict[str, Any]]) -> bool:
    for row in reversed(trajectory):
        if row.get("role") == "assistant" and str(row.get("content") or "").strip():
            return True
    return False


def _run_bounds(trajectory: list[dict[str, Any]]) -> tuple[int, int]:
    values = [_timestamp_ms(row.get("timestamp")) for row in trajectory]
    values = [value for value in values if value > 0]
    if not values:
        return 0, 0
    return min(values), max(values)


def _timestamp_ms(value: Any) -> int:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0
    if parsed <= 0:
        return 0
    if parsed < 100_000_000:
        return int(parsed)
    return int(parsed * 1000 if parsed < 10_000_000_000 else parsed)


def _iso_from_ms(value: int) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _first_int(payload: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = _optional_int(payload.get(key))
        if value is not None:
            return value
    return 0


def _error_metrics(findings: list[dict[str, Any]]) -> dict[str, int]:
    metrics = {
        "llmError": 0,
        "toolError": 0,
        "timeout": 0,
        "invalidOutput": 0,
        "validationFailed": 0,
    }
    for finding in findings:
        text = "%s %s %s" % (finding.get("code") or "", finding.get("category") or "", finding.get("message") or "")
        lowered = text.lower()
        if "timeout" in lowered:
            metrics["timeout"] += 1
        elif "llm" in lowered or "model" in lowered:
            metrics["llmError"] += 1
        elif "schema" in lowered or "validation" in lowered:
            metrics["validationFailed"] += 1
        elif "invalid" in lowered or "output" in lowered:
            metrics["invalidOutput"] += 1
        elif "tool" in lowered or "missing_tool" in lowered:
            metrics["toolError"] += 1
    return metrics


def _runtime_errors(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    errors = []
    for index, finding in enumerate(findings):
        if str(finding.get("severity") or "").lower() not in {"error", "critical", "high"}:
            continue
        label = str(finding.get("code") or finding.get("category") or finding.get("title") or "Runtime Error")
        message = str(finding.get("message") or finding.get("recommendation") or "")
        errors.append({"type": label, "count": 1, "traceNodeId": "finding-%s" % index, "message": message})
    collapsed: dict[str, dict[str, Any]] = {}
    for item in errors:
        current = collapsed.setdefault(item["type"], dict(item))
        if current is not item:
            current["count"] += item["count"]
    return list(collapsed.values())


def _count_matching(findings: list[dict[str, Any]], needle: str) -> int:
    return sum(1 for item in findings if needle in json.dumps(item, ensure_ascii=False).lower())


def _latency_metrics(duration: float, llm_calls: int, tool_calls: int) -> dict[str, float]:
    llm_budget = duration * 0.2 if tool_calls else duration * 0.55
    average = round(llm_budget / llm_calls, 3) if llm_calls else 0.0
    return {"average": average, "max": round(average * 1.8, 3) if average else 0.0}


def _tool_latency(duration: float, tool_calls: int) -> float:
    if not tool_calls:
        return 0.0
    return round((duration * 0.7) / tool_calls, 3)


def _estimate_token_cost(total_tokens: int) -> float:
    return round((total_tokens / 1000.0) * 0.005, 4)


def _estimate_model_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    lowered = model.lower()
    if "deepseek" in lowered:
        input_rate = 0.00014
        output_rate = 0.00028
    elif "gpt-5" in lowered:
        input_rate = 0.00125
        output_rate = 0.01
    elif "gpt-4" in lowered:
        input_rate = 0.005
        output_rate = 0.015
    else:
        input_rate = 0.0025
        output_rate = 0.0075
    return round((input_tokens / 1000.0) * input_rate + (output_tokens / 1000.0) * output_rate, 6)


def _provider_from_model(model: str) -> str:
    lowered = model.lower()
    if "deepseek" in lowered:
        return "DeepSeek"
    if "kimi" in lowered or lowered.startswith("k2") or lowered.startswith("k3"):
        return "Moonshot"
    if "claude" in lowered:
        return "Anthropic"
    if "gemini" in lowered:
        return "Google"
    if "qwen" in lowered:
        return "Alibaba"
    if "gpt" in lowered or "o3" in lowered or "o4" in lowered:
        return "OpenAI"
    return "Unknown"


def _pick(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return None


def _llm_latency(call: dict[str, Any], start_time: str, end_time: str) -> float:
    milliseconds = _optional_float(_pick(call, "latencyMs", "latency_ms"))
    if milliseconds is not None:
        return round(max(milliseconds / 1000.0, 0), 3)
    seconds = _optional_float(_pick(call, "latency", "totalLatency", "total_latency"))
    if seconds is not None:
        return round(max(seconds, 0), 3)
    start_ms = _timestamp_from_iso(start_time)
    end_ms = _timestamp_from_iso(end_time)
    if start_ms and end_ms and end_ms >= start_ms:
        return round((end_ms - start_ms) / 1000.0, 3)
    return 0


def _timestamp_from_iso(value: str) -> int:
    if not value:
        return 0
    try:
        normalized = value.replace("Z", "+00:00")
        return int(datetime.fromisoformat(normalized).timestamp() * 1000)
    except ValueError:
        return _timestamp_ms(value)


def _normalize_error(error: Any) -> dict[str, str] | None:
    if not error:
        return None
    if isinstance(error, str):
        return {"type": "LLM Error", "message": error}
    if isinstance(error, dict):
        return {
            "type": str(error.get("type") or error.get("code") or "LLM Error"),
            "message": str(error.get("message") or error.get("detail") or ""),
        }
    return {"type": "LLM Error", "message": str(error)}


def _normalize_quality(quality: dict[str, Any], call: dict[str, Any]) -> dict[str, Any]:
    response = str(_pick(call, "response", "output", "content") or "")
    response_length = _optional_int(_pick(quality, "responseLength", "response_length"))
    if response_length is None:
        response_length = len(response)
    format_compliance = _quality_ratio(_pick(quality, "formatCompliance", "format_compliance"))
    hallucination_risk = _quality_ratio(_pick(quality, "hallucinationRisk", "hallucination_risk"))
    instruction_following = _quality_ratio(_pick(quality, "instructionFollowing", "instruction_following"))
    self_evaluation = _optional_float(_pick(quality, "selfEvaluationScore", "self_evaluation_score"))
    if self_evaluation is not None and self_evaluation <= 1:
        self_evaluation *= 100

    score_parts = []
    if format_compliance is not None:
        score_parts.append(format_compliance)
    if hallucination_risk is not None:
        score_parts.append(1 - hallucination_risk)
    if instruction_following is not None:
        score_parts.append(instruction_following)
    if self_evaluation is not None:
        score_parts.append(max(min(self_evaluation / 100, 1), 0))
    quality_score = int((sum(score_parts) / len(score_parts)) * 100) if score_parts else 0

    return {
        "responseLength": response_length,
        "formatCompliance": format_compliance,
        "hallucinationRisk": hallucination_risk,
        "instructionFollowing": instruction_following,
        "selfEvaluationScore": round(self_evaluation) if self_evaluation is not None else None,
        "qualityScore": quality_score,
    }


def _quality_ratio(value: Any) -> float | None:
    parsed = _optional_float(value)
    if parsed is None:
        return None
    if parsed > 1:
        parsed = parsed / 100
    return round(max(min(parsed, 1), 0), 3)


def _rough_token_count(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / 4))


def _health_score(
    status: str,
    tool_calls: int,
    failed_tool_calls: int,
    failed_steps: int,
    total_steps: int,
    duration: float,
    total_tokens: int,
) -> dict[str, Any]:
    task = 95 if status == "success" else 40 if status == "failed" else 70
    tool = round(100 * (1 - _ratio(failed_tool_calls, tool_calls))) if tool_calls else 100
    performance = max(45, round(100 - min(duration, 120) / 120 * 45))
    error = round(100 * (1 - _ratio(failed_steps, total_steps))) if total_steps else 100
    cost = max(45, round(100 - min(total_tokens, 100_000) / 100_000 * 40))
    total = round(task * 0.35 + tool * 0.2 + performance * 0.18 + error * 0.17 + cost * 0.1)
    return {
        "total": total,
        "taskSuccess": task,
        "toolStability": tool,
        "performance": performance,
        "costEfficiency": cost,
        "errorRate": error,
    }


def _lifecycle_stages(
    status: str,
    start_ms: int,
    duration: float,
    failed_tool_calls: int,
    error_metrics: dict[str, int],
) -> list[dict[str, Any]]:
    names = ["Initialization", "Planning", "Reasoning", "Tool Execution", "Observation", "Reflection", "Final Response"]
    weights = [0.06, 0.09, 0.16, 0.46, 0.08, 0.1, 0.05]
    if duration <= 0:
        weights = [1 / len(names)] * len(names)
    current = start_ms
    stages = []
    for index, name in enumerate(names):
        stage_duration = round(duration * weights[index], 3)
        end = current + int(stage_duration * 1000) if current else 0
        stage_status = "success"
        if name == "Tool Execution" and failed_tool_calls:
            stage_status = "failed"
        elif name == "Reflection" and (error_metrics["validationFailed"] or error_metrics["invalidOutput"]):
            stage_status = "failed"
        elif name == "Final Response" and status == "failed" and not any(stage["status"] == "failed" for stage in stages):
            stage_status = "failed"
        elif status == "running" and index == len(names) - 1:
            stage_status = "running"
        stages.append(
            {
                "name": name,
                "startTime": _iso_from_ms(current),
                "endTime": _iso_from_ms(end),
                "duration": stage_duration,
                "status": stage_status,
            }
        )
        current = end
    return stages


def _performance_breakdown(duration: float, llm_calls: int, tool_calls: int) -> list[dict[str, Any]]:
    if duration <= 0:
        return [
            {"label": "Tool Execution", "value": 0, "percent": 0},
            {"label": "LLM Generation", "value": 0, "percent": 0},
            {"label": "RAG Retrieval", "value": 0, "percent": 0},
            {"label": "Other", "value": 0, "percent": 0},
        ]
    tool_percent = 70 if tool_calls else 15
    llm_percent = 20 if llm_calls else 5
    rag_percent = 8
    other_percent = max(100 - tool_percent - llm_percent - rag_percent, 0)
    return [
        {"label": "Tool Execution", "value": round(duration * tool_percent / 100, 3), "percent": tool_percent},
        {"label": "LLM Generation", "value": round(duration * llm_percent / 100, 3), "percent": llm_percent},
        {"label": "RAG Retrieval", "value": round(duration * rag_percent / 100, 3), "percent": rag_percent},
        {"label": "Other", "value": round(duration * other_percent / 100, 3), "percent": other_percent},
    ]


def _root_cause_counts(judge: dict[str, Any], audit: dict[str, Any]) -> Counter[str]:
    causes: Counter[str] = Counter()
    for finding in judge.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        label = _finding_label(finding, "category", "title")
        if label:
            causes[label] += 1
    if causes:
        return causes
    for finding in audit.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        label = _finding_label(finding, "code", "message")
        if label:
            causes[label] += 1
    return causes


def _finding_label(finding: dict[str, Any], category_key: str, title_key: str) -> str:
    category = str(finding.get(category_key) or "").strip()
    title = str(finding.get(title_key) or "").strip()
    if category and title:
        return f"{category}: {title}"
    return title or category


def _is_success(status: str, completion_score: int | None) -> bool:
    if status == "success":
        return True
    if status in {"failed", "partial"}:
        return False
    return completion_score is not None and completion_score >= 80


def _optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_default(value: Any, default: int) -> int:
    parsed = _optional_int(value)
    return default if parsed is None else parsed


def _ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0
    return numerator / denominator


def _average(values: Any) -> float:
    parsed = []
    for value in values:
        try:
            parsed.append(float(value))
        except (TypeError, ValueError):
            continue
    if not parsed:
        return 0
    return round(sum(parsed) / len(parsed), 3)


def _float_or_default(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _validate_import_request(base_url: str, user_id: str, session_id: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="base_url must be an http(s) URL")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id is required")
    if not session_id or "/" in session_id or "\\" in session_id:
        raise HTTPException(status_code=400, detail="session_id is required")
