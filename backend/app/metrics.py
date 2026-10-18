"""Phase 5 metrics registry (Prometheus text exposition).

Hard rules enforced here:

- **no high-cardinality labels**: label values come from bounded enums
  (route templates, methods, status codes, source systems, job statuses).
  ``run_id``/``trace_id``/``user_id``/``import_id`` are rejected at
  registration time;
- **no sensitive values**: no prompts, tokens, user identifiers or
  connection strings ever enter a metric name, label or help string;
- scrape-time gauges use callbacks so operational state (worker heartbeats,
  stale jobs, index status) is always current without writes on the hot
  path.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Label values matching these look like per-entity identifiers and are
#: rejected to keep cardinality bounded.
_FORBIDDEN_LABEL_NAMES = frozenset(
    {"run_id", "trace_id", "user_id", "import_id", "job_id", "session_id", "token", "actor", "email", "sub"}
)

#: Histogram buckets (seconds) shared by duration metrics.
DEFAULT_BUCKETS = (0.05, 0.25, 1.0, 5.0, 15.0, 60.0, 300.0)

_LOCK = threading.RLock()
_COUNTERS: Dict[str, Dict[Tuple[Tuple[str, str], ...], float]] = {}
_COUNTER_META: Dict[str, str] = {}
_GAUGE_CALLBACKS: Dict[str, Tuple[Callable[[], List[Tuple[Dict[str, str], float]]], str]] = {}
_HISTOGRAMS: Dict[str, Dict[Tuple[Tuple[str, str], ...], List[float]]] = {}
_HISTOGRAM_META: Dict[str, Tuple[str, Tuple[float, ...]]] = {}


def _validate_labels(labels: Dict[str, str]) -> Tuple[Tuple[str, str], ...]:
    cleaned = []
    for name, value in sorted((labels or {}).items()):
        key = str(name).strip().lower()
        if key in _FORBIDDEN_LABEL_NAMES:
            raise ValueError("metric label %r is forbidden (high cardinality / sensitive)" % name)
        text = str(value)
        if len(text) > 64:
            text = text[:64]
        cleaned.append((key, text))
    return tuple(cleaned)


def reset_registry() -> None:
    """Clear all series (tests only)."""
    with _LOCK:
        _COUNTERS.clear()
        _COUNTER_META.clear()
        _GAUGE_CALLBACKS.clear()
        _HISTOGRAMS.clear()
        _HISTOGRAM_META.clear()


def counter_inc(name: str, amount: float = 1.0, *, _help: str = "", **labels: str) -> None:
    key = _validate_labels(labels)
    with _LOCK:
        _COUNTER_META.setdefault(name, _help)
        series = _COUNTERS.setdefault(name, {})
        series[key] = series.get(key, 0.0) + float(amount)


def histogram_observe(name: str, value: float, *, _help: str = "", buckets: Tuple[float, ...] = DEFAULT_BUCKETS, **labels: str) -> None:
    key = _validate_labels(labels)
    with _LOCK:
        _HISTOGRAM_META.setdefault(name, (_help, tuple(buckets)))
        series = _HISTOGRAMS.setdefault(name, {})
        # store count/sum per bucket layout: [<=b1, <=b2, ..., +Inf count, sum]
        counts = series.get(key)
        if counts is None:
            counts = [0.0] * (len(buckets) + 2)
            series[key] = counts
        for index, bound in enumerate(buckets):
            if value <= bound:
                counts[index] += 1.0
        counts[len(buckets)] += 1.0  # total count
        counts[len(buckets) + 1] += float(value)  # sum


def gauge_callback(name: str, callback: Callable[[], List[Tuple[Dict[str, str], float]]], *, _help: str = "") -> None:
    """Register a scrape-time gauge. ``callback`` returns (labels, value)
    pairs and must be cheap and never raise."""
    with _LOCK:
        _GAUGE_CALLBACKS[name] = (callback, _help)


def _esc(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _label_string(key: Tuple[Tuple[str, str], ...]) -> str:
    if not key:
        return ""
    return "{%s}" % ",".join('%s="%s"' % (name, _esc(value)) for name, value in key)


def render_prometheus() -> str:
    """Serialize the registry in Prometheus text exposition format."""
    lines: List[str] = []
    with _LOCK:
        for name in sorted(_COUNTERS):
            help_text = _COUNTER_META.get(name) or name
            lines.append("# HELP %s %s" % (name, help_text))
            lines.append("# TYPE %s counter" % name)
            for key, value in sorted(_COUNTERS[name].items()):
                lines.append("%s%s %g" % (name, _label_string(key), value))
        for name in sorted(_HISTOGRAMS):
            help_text, buckets = _HISTOGRAM_META.get(name, (name, DEFAULT_BUCKETS))
            lines.append("# HELP %s %s" % (name, help_text))
            lines.append("# TYPE %s histogram" % name)
            for key, counts in sorted(_HISTOGRAMS[name].items()):
                label_list = list(key)
                for index, bound in enumerate(buckets):
                    le_labels = _label_string(tuple(label_list + [("le", "%g" % bound)]))
                    lines.append("%s_bucket%s %g" % (name, le_labels, counts[index]))
                inf_labels = _label_string(tuple(label_list + [("le", "+Inf")]))
                lines.append("%s_bucket%s %g" % (name, inf_labels, counts[len(buckets)]))
                lines.append("%s_count%s %g" % (name, _label_string(key), counts[len(buckets)]))
                lines.append("%s_sum%s %g" % (name, _label_string(key), counts[len(buckets) + 1]))
        callbacks = list(_GAUGE_CALLBACKS.items())
    for name, (callback, help_text) in sorted(callbacks):
        lines.append("# HELP %s %s" % (name, help_text or name))
        lines.append("# TYPE %s gauge" % name)
        try:
            samples = callback() or []
        except Exception:
            samples = []
        for labels, value in samples:
            try:
                key = _validate_labels(labels)
            except ValueError:
                continue
            lines.append("%s%s %g" % (name, _label_string(key), float(value)))
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# HTTP instrumentation middleware
# ---------------------------------------------------------------------------


class HttpMetricsMiddleware:
    """ASGI middleware counting requests and durations with bounded labels
    (route template + method + status class — never raw paths with ids)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method") or "GET")
        started = time.monotonic()
        status_code = 500

        async def send_wrapper(message: Dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status") or 500)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get("route")
            template = getattr(route, "path", None) or _path_class(str(scope.get("path") or ""))
            labels = {
                "method": method,
                "route": template,
                "status": "%dxx" % (status_code // 100),
            }
            counter_inc("auditrail_http_requests_total", _help="HTTP requests", **labels)
            histogram_observe(
                "auditrail_http_request_duration_seconds",
                time.monotonic() - started,
                _help="HTTP request duration",
                **labels,
            )


def _path_class(path: str) -> str:
    """Bounded fallback classification for unmatched paths."""
    if path.startswith("/api/"):
        parts = [part for part in path.split("/") if part]
        return "/" + "/".join(parts[:3]) if len(parts) >= 3 else path
    return path[:64]


# ---------------------------------------------------------------------------
# import job instrumentation (called by the worker at terminal states)
# ---------------------------------------------------------------------------


def record_import_finished(job: Dict[str, Any]) -> None:
    """Terminal-state metrics for one ImportJob. Bounded labels only
    (source_system/kind/status/error code class — never import_id)."""
    try:
        source = str(job.get("source_system") or "unknown")[:64]
        kind = str(job.get("kind") or "unknown")[:64]
        status = str(job.get("status") or "unknown")
        counter_inc(
            "auditrail_import_jobs_total",
            _help="ImportJobs reaching a terminal status",
            source_system=source,
            kind=kind,
            status=status,
        )
        if status == "failed":
            error = job.get("error") if isinstance(job.get("error"), dict) else {}
            counter_inc(
                "auditrail_import_job_failures_total",
                _help="Failed ImportJobs",
                source_system=source,
                code=str(error.get("code") or "unknown")[:64],
            )
        imported = int(job.get("imported_records") or 0)
        if imported and not job.get("dry_run"):
            counter_inc(
                "auditrail_external_records_total",
                imported,
                _help="External evidence records imported",
                source_system=source,
                kind=kind,
            )
        started = job.get("started_at")
        finished = job.get("finished_at")
        if started and finished:
            from datetime import datetime

            try:
                t0 = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
                duration = max((t1 - t0).total_seconds(), 0.0)
                histogram_observe(
                    "auditrail_import_job_duration_seconds",
                    duration,
                    _help="ImportJob duration",
                    source_system=source,
                    kind=kind,
                )
            except ValueError:
                pass
    except Exception:
        pass  # metrics must never fail an import


def record_index_refresh(reason: str, duration_seconds: float, *, error: bool = False) -> None:
    """Run-index refresh instrumentation (bounded reason label)."""
    try:
        counter_inc("auditrail_run_index_refresh_total", _help="Run index refreshes", reason=str(reason)[:32])
        histogram_observe(
            "auditrail_run_index_refresh_duration_seconds",
            float(duration_seconds),
            _help="Run index refresh duration",
        )
        if error:
            counter_inc("auditrail_run_index_errors_total", _help="Run index refresh errors")
    except Exception:
        pass


def record_suite_job(status: str, duration_seconds: Optional[float] = None) -> None:
    try:
        counter_inc("auditrail_suite_jobs_total", _help="SuiteJobs reaching a terminal status", status=str(status)[:32])
        if duration_seconds is not None:
            histogram_observe(
                "auditrail_suite_job_duration_seconds", float(duration_seconds), _help="SuiteJob duration"
            )
    except Exception:
        pass
