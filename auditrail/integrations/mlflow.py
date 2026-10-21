"""auditrail → OTLP/MLflow export adapter (mapping version ``otel-map-1``).

Implements docs/superpowers/specs/2026-10-21-mlflow-otlp-export-mapping-design.md:
``runs/<run_id>/`` artifacts are the only factual contract; the exported OTLP
payload is a one-way, lossy, versioned view. Every field carries a provenance
class (observed / derived / synthetic); exporter-generated identifiers, the
root span and ordering edges are organizational structure, never execution
evidence of the audited agent.

The module is stdlib-only (urllib for OTLP/HTTP) so the export path never
becomes a core dependency. All network failures surface explicitly as
:class:`MlflowExportError`.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..audit import _explicit_exit_code, _looks_like_tool_error
from ..costs import load_cost_records, records_for_run
from ..run_contract import load_run_state
from ..runtime import _preview, _tool_call_args, _tool_call_id, _tool_call_name
from ..trajectory import messages_from_session_response

#: Mapping rules version (design doc §3). Bump on any field/provenance/ID change.
MAPPING_VERSION = "otel-map-1"

#: Fixed base for synthetic ordering-only timestamps (2000-01-01T00:00:00Z, ms).
#: Chosen to avoid implying any real wall-clock date (design doc §4).
SYNTHETIC_BASE_MS = 946684800000

#: Sentinel values written by upstream normalizers that must be treated as
#: missing (design doc §5).
SENTINEL_MODEL = "unknown"
SENTINEL_PROVIDERS = ("", "proxy")

#: OTLP span status codes.
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

#: Provider name normalization towards the gen_ai semconv lowercase enums.
_PROVIDER_ENUM_ALIASES = {"deepseek": "deepseek"}

_HTTP_TIMEOUT_SECONDS = 15
_MAX_PAYLOAD_BYTES = 32 * 1024 * 1024


class MlflowExportError(RuntimeError):
    """Explicit export failure; never swallowed."""

    def __init__(self, code: str, message: str, *, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status


# ---------------------------------------------------------------------------
# OTLP JSON encoding helpers
# ---------------------------------------------------------------------------


def _attr(key: str, value: Any) -> Dict[str, Any]:
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    elif isinstance(value, (dict, list)):
        encoded = {"stringValue": json.dumps(value, ensure_ascii=False, default=str)}
    else:
        encoded = {"stringValue": str(value)}
    return {"key": key, "value": encoded}


def _attrs(pairs: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [_attr(key, value) for key, value in pairs.items() if value is not None]


def _span_record(
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: Optional[str],
    name: str,
    start_ns: int,
    end_ns: int,
    attributes: Dict[str, Any],
    status_code: int = STATUS_UNSET,
) -> Dict[str, Any]:
    record = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": _attrs(attributes),
        "status": {"code": status_code},
    }
    if parent_span_id:
        record["parentSpanId"] = parent_span_id
    return record


# ---------------------------------------------------------------------------
# Stable identifier generation (design doc §3)
# ---------------------------------------------------------------------------


def trace_id_for(run_id: str) -> str:
    digest = hashlib.sha256(("auditrail:%s:%s" % (MAPPING_VERSION, run_id)).encode("utf-8"))
    return digest.hexdigest()[:32]


def span_id_for(trace_id: str, span_key: str) -> str:
    digest = hashlib.sha256(("%s:%s" % (trace_id, span_key)).encode("utf-8"))
    return digest.hexdigest()[:16]


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Optional[Any]:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def load_run_inputs(run_dir: Path) -> Dict[str, Any]:
    """Load the factual contract files of one run. Missing files stay missing."""
    run_dir = Path(run_dir)
    session = _read_json(run_dir / "session.json")
    summary = _read_json(run_dir / "summary.json") or {}
    llm_calls_doc = _read_json(run_dir / "llm_calls.json") or {}
    llm_calls = llm_calls_doc.get("llm_calls") if isinstance(llm_calls_doc, dict) else None
    messages: List[Dict[str, Any]] = []
    if isinstance(session, dict):
        # Raw session messages (same input as audit_messages): they retain
        # evidence fields that trajectory.jsonl normalization drops
        # (is_error / exit_code / status).
        messages = [m for m in messages_from_session_response(session) if isinstance(m, dict)]
    return {
        "run_dir": run_dir,
        "run_state": load_run_state(run_dir),
        "summary": summary if isinstance(summary, dict) else {},
        "messages": messages,
        "llm_calls": [c for c in (llm_calls or []) if isinstance(c, dict)],
    }


# ---------------------------------------------------------------------------
# llm_calls dual-schema handling (design doc §5)
# ---------------------------------------------------------------------------


def _llm_call_form(call: Dict[str, Any]) -> str:
    """Record-level form detection: proxy (camelCase) vs dsh (snake_case)."""
    if "callId" in call or "tokenUsage" in call:
        return "proxy"
    return "dsh"


_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)


def _llm_call_id(call: Dict[str, Any], form: str) -> Tuple[Optional[str], str]:
    """Return (call identifier, id_anchor kind) per design doc §3.

    proxy form: a provider response id is an observed anchor; the uuid4()
    fallback (llm_proxy.py:210) is stable once persisted but synthetic.
    """
    if form == "proxy":
        raw = call.get("callId")
        if raw:
            text = str(raw)
            return text, ("uuid" if _UUID_RE.match(text) else "response_id")
        return None, "missing"
    raw = call.get("call_id")
    if raw:
        text = str(raw)
        if text.startswith("turn") and "-step" in text:
            return text, "turn_step"
        return text, "message_id"
    return None, "missing"


def _llm_call_usage(call: Dict[str, Any], form: str) -> Optional[Dict[str, Any]]:
    """Extract token usage; sentinel zero-fill (proxy) counts as missing."""
    raw = call.get("tokenUsage") if form == "proxy" else call.get("token_usage")
    if not isinstance(raw, dict):
        return None
    if form == "proxy":
        # llm_proxy.py:427-433 zero-fills each missing key; per design doc §5
        # a real call never has 0 input tokens, so 0 is treated as missing
        # key by key (never exported as an observed 0).
        def _nz(key: str) -> Optional[int]:
            value = raw.get(key)
            if isinstance(value, (int, float)) and int(value) > 0:
                return int(value)
            return None

        usage = {"input": _nz("inputTokens"), "output": _nz("outputTokens"), "cache_read": None, "cache_write": None}
        if usage["input"] is None and usage["output"] is None:
            return None
        return usage
    return {
        "input": raw.get("input_tokens") if isinstance(raw.get("input_tokens"), int) else None,
        "output": raw.get("output_tokens") if isinstance(raw.get("output_tokens"), int) else None,
        "cache_read": raw.get("cache_read_tokens") if isinstance(raw.get("cache_read_tokens"), int) else None,
        "cache_write": raw.get("cache_write_tokens") if isinstance(raw.get("cache_write_tokens"), int) else None,
    }


def _llm_call_model(call: Dict[str, Any]) -> Optional[str]:
    model = str(call.get("model") or "").strip()
    if not model or model == SENTINEL_MODEL:
        return None
    return model


def _llm_call_provider(call: Dict[str, Any]) -> Optional[str]:
    provider = str(call.get("provider") or "").strip()
    if provider in SENTINEL_PROVIDERS:
        return None
    return _PROVIDER_ENUM_ALIASES.get(provider.lower(), provider.lower())


def _llm_call_latency_ms(call: Dict[str, Any], form: str) -> Tuple[Optional[float], Optional[str]]:
    """Return (latency_ms, unit_source). proxy latency is SECONDS (doc §5)."""
    if form == "proxy":
        value = call.get("latency")
        if isinstance(value, (int, float)):
            return float(value) * 1000.0, "seconds"
        return None, None
    value = call.get("latency_ms")
    if isinstance(value, (int, float)):
        return float(value), "milliseconds"
    return None, None


def _llm_call_times(call: Dict[str, Any], form: str) -> Tuple[Optional[int], Optional[int]]:
    """proxy form carries wall-clock startTime/endTime (ISO). Return epoch ms."""
    if form != "proxy":
        return None, None
    start = _parse_iso_ms(call.get("startTime"))
    end = _parse_iso_ms(call.get("endTime"))
    return start, end


def _parse_iso_ms(value: Any) -> Optional[int]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Timestamp discipline (design doc §4)
# ---------------------------------------------------------------------------


def _message_time_ms(message: Dict[str, Any]) -> Optional[int]:
    """Observed epoch-millis timestamp, or None when absent/unparseable."""
    raw = message.get("timestamp")
    if raw in (None, ""):
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    # Values below 1e12 are epoch seconds; the DSH recorder writes millis.
    return value * 1000 if value < 1_000_000_000_000 else value


class _Clock:
    """Assigns span times under the §4 discipline.

    Observed message times are used verbatim; messages without one get
    monotonic placeholder times (synthetic, ordering-only). The placeholder
    base never comes from the collection clock or the export clock.
    """

    def __init__(self, observed_times: List[int]):
        self.base_ms = min(observed_times) if observed_times else SYNTHETIC_BASE_MS
        self.has_observed = bool(observed_times)
        self._counter = 0

    def start_for(self, message: Dict[str, Any]) -> Tuple[int, str]:
        observed = _message_time_ms(message)
        if observed is not None:
            # Observed start; ties (same millisecond) keep trajectory order
            # because spans are emitted sequentially (doc §4 tie-break).
            return observed, "observed"
        self._counter += 1
        return self.base_ms + self._counter, "synthetic_order_only"

    @staticmethod
    def ms_to_ns(value_ms) -> int:
        # Integer millisecond inputs convert exactly; float64 cannot hold
        # epoch-nanos (19 digits > 53-bit mantissa), so multiply in int domain.
        if isinstance(value_ms, int):
            return value_ms * 1_000_000
        return int(round(value_ms * 1_000_000))


# ---------------------------------------------------------------------------
# Tool status via the audit judgement chain (design doc §6)
# ---------------------------------------------------------------------------


def _tool_status(message: Dict[str, Any], content: str) -> Tuple[int, str]:
    """Map a tool result to (OTLP status code, auditrail.status_basis).

    Mirrors audit.py's ``_is_tool_error`` priority without duplicating it:
    is_error flag → explicit exit code → content heuristic. The heuristic may
    never override an observed negative signal.
    """
    flag = message.get("is_error")
    if isinstance(flag, bool):
        return (STATUS_ERROR if flag else STATUS_OK), "observed_flag"
    exit_code = _explicit_exit_code(message, content)
    if exit_code is not None:
        structured = any(
            message.get(key) is not None for key in ("exit_code", "exitCode", "exit_status", "exitStatus")
        )
        basis = "observed_exit_code" if structured else "derived_exit_code_marker"
        return (STATUS_ERROR if exit_code != 0 else STATUS_OK), basis
    if _looks_like_tool_error(content):
        return STATUS_ERROR, "heuristic"
    return STATUS_UNSET, "none"


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def build_trace_payload(run_dir: Path, *, platform_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Build the OTLP/HTTP JSON payload for one run (pure, deterministic)."""
    inputs = load_run_inputs(run_dir)
    run_state = inputs["run_state"]
    summary = inputs["summary"]
    messages = inputs["messages"]
    llm_calls = inputs["llm_calls"]

    run_id = str(run_state.get("run_id") or Path(run_dir).name)
    if not run_id:
        raise MlflowExportError("missing_run_id", "run directory has no run_id", retryable=False)

    trace_id = trace_id_for(run_id)
    observed_times = [t for t in (_message_time_ms(m) for m in messages) if t is not None]
    clock = _Clock(observed_times)

    spans: List[Dict[str, Any]] = []
    anchor_stats: Dict[str, int] = {}

    def _bump_anchor(kind: str) -> None:
        anchor_stats[kind] = anchor_stats.get(kind, 0) + 1

    # --- llm_calls enrichment pairing -------------------------------------
    # Pair in order with assistant messages that produced LLM output; the
    # pairing boundary is derived unless call_id == message id (doc §6).
    llm_by_message_id: Dict[str, Dict[str, Any]] = {}
    llm_queue: List[Dict[str, Any]] = []
    for call in llm_calls:
        form = _llm_call_form(call)
        call_id, anchor = _llm_call_id(call, form)
        if call_id and anchor == "message_id":
            llm_by_message_id[call_id] = call
        else:
            llm_queue.append(call)

    user_messages: List[Dict[str, Any]] = []
    open_tool_spans: Dict[str, int] = {}  # tool_call_id -> index into spans
    pending_llm_queue = list(llm_queue)

    def _next_llm_call(message: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], str]:
        message_id = str(message.get("id") or "")
        if message_id and message_id in llm_by_message_id:
            return llm_by_message_id.pop(message_id), "message_id"
        if pending_llm_queue:
            return pending_llm_queue.pop(0), "ordered_pairing"
        return None, "none"

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "user":
            content = str(message.get("content") or "")
            if content:
                user_messages.append(message)
            continue

        if role == "assistant":
            tool_calls = [c for c in (message.get("tool_calls") or []) if isinstance(c, dict)]
            content = str(message.get("content") or "").strip()
            if not tool_calls and not content:
                continue
            start_ms, time_basis = clock.start_for(message)
            llm_call, pairing = _next_llm_call(message)
            attrs: Dict[str, Any] = {
                "gen_ai.operation.name": "chat",
                "auditrail.time_basis": time_basis,
                "auditrail.provenance.span_boundary": "observed" if pairing == "message_id" else "derived",
            }
            end_ms: Optional[float] = None
            end_ns_exact: Optional[int] = None
            duration_basis: Optional[str] = None
            if llm_call is not None:
                form = _llm_call_form(llm_call)
                model = _llm_call_model(llm_call)
                provider = _llm_call_provider(llm_call)
                usage = _llm_call_usage(llm_call, form)
                latency_ms, unit_source = _llm_call_latency_ms(llm_call, form)
                proxy_start_ms, proxy_end_ms = _llm_call_times(llm_call, form)
                if model:
                    attrs["gen_ai.request.model"] = model
                if provider:
                    attrs["gen_ai.provider.name"] = provider
                    attrs["auditrail.provenance.provider"] = "derived"
                if usage:
                    if usage.get("input") is not None:
                        attrs["gen_ai.usage.input_tokens"] = usage["input"]
                    if usage.get("output") is not None:
                        attrs["gen_ai.usage.output_tokens"] = usage["output"]
                    if usage.get("cache_read") is not None:
                        attrs["gen_ai.usage.cache_read.input_tokens"] = usage["cache_read"]
                        # DSH source input excludes cache; semconv SHOULD include it.
                        attrs["auditrail.usage.input_excludes_cache"] = True
                    if usage.get("cache_write") is not None:
                        attrs["auditrail.usage.cache_write_tokens"] = usage["cache_write"]
                if form == "proxy" and proxy_start_ms is not None and proxy_end_ms is not None:
                    start_ms, time_basis = proxy_start_ms, "derived"
                    attrs["auditrail.time_basis"] = "derived"
                    end_ms = float(proxy_end_ms)
                    duration_basis = "derived"
                elif latency_ms is not None:
                    end_ms = float(start_ms) + latency_ms
                    # exact duration in the ns domain (float64 cannot hold it)
                    end_ns_exact = clock.ms_to_ns(start_ms) + int(round(latency_ms * 1_000_000))
                    duration_basis = "derived"
                    attrs["auditrail.provenance.duration"] = "derived"
                    if unit_source:
                        attrs["auditrail.duration_unit_source"] = unit_source
            finish_reason = message.get("finish_reason")
            if finish_reason:
                attrs["auditrail.llm.finish_reason"] = str(finish_reason)
            if content:
                attrs["gen_ai.output.messages"] = [
                    {"role": "assistant", "content": content, "auditrail.message_index": index}
                ]
                attrs["auditrail.provenance.messages"] = "derived"
            elif tool_calls:
                attrs["gen_ai.output.messages"] = [
                    {
                        "role": "assistant",
                        "tool_calls": [
                            {"name": _tool_call_name(c) or "unknown", "arguments": _tool_call_args(c)}
                            for c in tool_calls
                        ],
                        "auditrail.message_index": index,
                    }
                ]
                attrs["auditrail.provenance.messages"] = "derived"
            # span key (doc §3): call id > message id > trajectory index
            if llm_call is not None:
                form = _llm_call_form(llm_call)
                call_id, anchor = _llm_call_id(llm_call, form)
            else:
                call_id, anchor = None, "missing"
            if call_id:
                span_key = "llm:%s" % call_id
                _bump_anchor(anchor)
                # anchor ∈ response_id|uuid|turn_step|message_id (doc §3);
                # the uuid fallback is stable but synthetic.
                attrs["auditrail.id_anchor"] = anchor
            elif message.get("id"):
                span_key = "msg:%s" % message.get("id")
                _bump_anchor("message_id")
                attrs["auditrail.id_anchor"] = "message_id"
            else:
                span_key = "idx:%d" % index
                _bump_anchor("index")
                attrs["auditrail.id_anchor"] = "index"
            if end_ms is None:
                # No observed/derived duration: synthetic 1ms end (never equal
                # to start), explicitly labeled (doc §4).
                end_ms = float(start_ms) + 1.0
                duration_basis = "synthetic"
            if duration_basis:
                attrs.setdefault("auditrail.duration_basis", duration_basis)
            span_id = span_id_for(trace_id, span_key)
            spans.append(
                _span_record(
                    trace_id=trace_id,
                    span_id=span_id,
                    parent_span_id=None,  # attached to root below
                    name="LLM Call" if tool_calls else "Assistant Response",
                    start_ns=clock.ms_to_ns(start_ms),
                    end_ns=end_ns_exact if end_ns_exact is not None else clock.ms_to_ns(end_ms),
                    attributes=attrs,
                    status_code=STATUS_UNSET,  # doc §5/§6: never map llm status
                )
            )
            spans[-1]["_root_child"] = True

            for call_index, call in enumerate(tool_calls):
                tool_name = _tool_call_name(call) or "unknown"
                tool_call_id = _tool_call_id(call)
                t_start_ms = float(start_ms) + 0.1 * (call_index + 1)
                t_attrs: Dict[str, Any] = {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool_name,
                    "auditrail.time_basis": time_basis,
                }
                if tool_name == "skill_view":
                    t_attrs["auditrail.tool.display_type"] = "Skill"
                if tool_call_id:
                    t_attrs["gen_ai.tool.call.id"] = str(tool_call_id)
                    span_key = "tool:%s" % tool_call_id
                    _bump_anchor("tool")
                    t_attrs["auditrail.id_anchor"] = "tool_call_id"
                else:
                    span_key = "idx:%d:%d" % (index, call_index)
                    _bump_anchor("index")
                    t_attrs["auditrail.id_anchor"] = "index"
                raw_args = _tool_call_args(call)
                if raw_args:
                    preview = _preview(raw_args, 240)
                    t_attrs["gen_ai.tool.call.arguments"] = preview
                    if len(raw_args) > 240:
                        t_attrs["auditrail.truncated"] = True
                t_span_id = span_id_for(trace_id, span_key)
                spans.append(
                    _span_record(
                        trace_id=trace_id,
                        span_id=t_span_id,
                        parent_span_id=None,
                        name=tool_name,
                        start_ns=clock.ms_to_ns(t_start_ms),
                        end_ns=clock.ms_to_ns(t_start_ms + 1.0),
                        attributes={**t_attrs, "auditrail.duration_basis": "synthetic"},
                        status_code=STATUS_UNSET,
                    )
                )
                spans[-1]["_root_child"] = True
                if tool_call_id:
                    open_tool_spans[str(tool_call_id)] = len(spans) - 1
            continue

        if role == "tool":
            content = str(message.get("content") or "")
            tool_call_id = str(message.get("tool_call_id") or "")
            tool_name = str(message.get("tool_name") or "tool")
            start_ms, time_basis = clock.start_for(message)
            status_code, basis = _tool_status(message, content)
            target_index = open_tool_spans.pop(tool_call_id, None) if tool_call_id else None
            if target_index is not None:
                # Merge the result into the execute_tool span (doc §6).
                span = spans[target_index]
                merged = {a["key"]: a for a in span["attributes"]}
                merged["gen_ai.tool.call.result"] = _attr("gen_ai.tool.call.result", _preview(content, 4000))
                merged["auditrail.status_basis"] = _attr("auditrail.status_basis", basis)
                merged["auditrail.provenance.result_pairing"] = _attr(
                    "auditrail.provenance.result_pairing", "derived"
                )
                span["attributes"] = list(merged.values())
                span["status"] = {"code": status_code}
                end_ms = float(start_ms)
                span["endTimeUnixNano"] = str(clock.ms_to_ns(end_ms))
                if span["endTimeUnixNano"] <= span["startTimeUnixNano"]:
                    span["endTimeUnixNano"] = str(int(span["startTimeUnixNano"]) + 1_000_000)
                    span["attributes"].append(_attr("auditrail.duration_basis", "synthetic"))
                else:
                    span["attributes"].append(_attr("auditrail.duration_basis", "derived"))
            else:
                # Orphan tool result: direct child of root, never dropped (doc §6).
                o_attrs: Dict[str, Any] = {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool_name,
                    "auditrail.orphan": True,
                    "auditrail.missing": "tool_call",
                    "auditrail.status_basis": basis,
                    "auditrail.time_basis": time_basis,
                    "gen_ai.tool.call.result": _preview(content, 4000),
                    "auditrail.duration_basis": "synthetic",
                }
                if tool_call_id:
                    o_attrs["gen_ai.tool.call.id"] = tool_call_id
                    o_key = "orphan:%s" % tool_call_id
                else:
                    o_key = "orphan:idx:%d" % index
                o_attrs["auditrail.id_anchor"] = "tool_call_id" if tool_call_id else "index"
                _bump_anchor("tool" if tool_call_id else "index")
                spans.append(
                    _span_record(
                        trace_id=trace_id,
                        span_id=span_id_for(trace_id, o_key),
                        parent_span_id=None,
                        name="%s result" % tool_name,
                        start_ns=clock.ms_to_ns(float(start_ms)),
                        end_ns=clock.ms_to_ns(float(start_ms) + 1.0),
                        attributes=o_attrs,
                        status_code=status_code,
                    )
                )
                spans[-1]["_root_child"] = True

    # Tool calls without results stay open (doc §6: auditrail.missing).
    for span_index in open_tool_spans.values():
        spans[span_index]["attributes"].append(_attr("auditrail.missing", "tool_result"))

    # --- root span ---------------------------------------------------------
    root_start_ns = min(int(s["startTimeUnixNano"]) for s in spans) if spans else clock.ms_to_ns(SYNTHETIC_BASE_MS)
    root_end_ns = max(int(s["endTimeUnixNano"]) for s in spans) if spans else root_start_ns + 1_000_000
    weakest_basis = "observed"
    for span in spans:
        basis = next(
            (a["value"].get("stringValue") for a in span["attributes"] if a["key"] == "auditrail.time_basis"),
            None,
        )
        if basis == "synthetic_order_only":
            weakest_basis = "synthetic_order_only"
            break
        if basis == "derived":
            weakest_basis = "derived"

    root_attrs: Dict[str, Any] = {
        "auditrail.provenance.root": "synthetic",
        "auditrail.mapping_version": MAPPING_VERSION,
        "auditrail.run_id": run_id,
        "auditrail.time_basis": weakest_basis,
        "auditrail.id_anchor_stats": anchor_stats,
    }
    status = run_state.get("status")
    if status:
        root_attrs["auditrail.run.status"] = status
        if run_state.get("legacy_inferred"):
            root_attrs["auditrail.provenance.run_status"] = "derived"
        else:
            root_attrs["auditrail.provenance.run_status"] = "observed"
    if run_state.get("status_reason"):
        root_attrs["auditrail.run.status_reason"] = str(run_state.get("status_reason"))
    if run_state.get("started_at"):
        root_attrs["auditrail.collection.started_at"] = str(run_state.get("started_at"))
    if run_state.get("finished_at"):
        root_attrs["auditrail.collection.finished_at"] = str(run_state.get("finished_at"))
    agent_ref = run_state.get("agent_ref") or summary.get("model")
    if agent_ref:
        root_attrs["auditrail.agent.ref"] = (
            json.dumps(agent_ref, ensure_ascii=False, default=str) if isinstance(agent_ref, dict) else str(agent_ref)
        )

    # User input on the root span; multi-turn grouping is derived (doc §6).
    if user_messages:
        grouped = []
        for turn, message in enumerate(user_messages):
            grouped.append(
                {
                    "role": "user",
                    "content": str(message.get("content") or ""),
                    "auditrail.turn_index": turn,
                    "auditrail.message_index": message.get("index", turn),
                }
            )
        root_attrs["gen_ai.input.messages"] = grouped
        root_attrs["auditrail.provenance.input_grouping"] = "derived"

    # Cost attributes (optional; doc §6 costs row).
    if platform_dir is not None:
        try:
            records = records_for_run(load_cost_records(Path(platform_dir)), run_id)
        except Exception:
            records = []
        run_level_costs = [r for r in records if r.get("source") and r.get("source") != "missing"]
        for record in run_level_costs:
            amount = record.get("amount")
            if amount is None:
                continue
            root_attrs.setdefault("auditrail.cost.amount", amount)
            if record.get("currency"):
                root_attrs.setdefault("auditrail.cost.currency", record.get("currency"))
            root_attrs.setdefault("auditrail.cost.source", record.get("source"))
            if record.get("price_version"):
                root_attrs.setdefault("auditrail.cost.price_version", record.get("price_version"))
            break  # v1: run-level aggregate only, first non-missing record

    root_span_id = span_id_for(trace_id, "root")
    for span in spans:
        if span.pop("_root_child", False):
            span["parentSpanId"] = root_span_id
    root = _span_record(
        trace_id=trace_id,
        span_id=root_span_id,
        parent_span_id=None,
        name="auditrail.run",
        start_ns=root_start_ns,
        end_ns=root_end_ns,
        attributes=root_attrs,
        status_code=STATUS_UNSET,
    )

    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": _attrs(
                        {
                            "service.name": "auditrail",
                            "auditrail.mapping_version": MAPPING_VERSION,
                            "auditrail.run_id": run_id,
                        }
                    )
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "auditrail.integrations.mlflow", "version": MAPPING_VERSION},
                        "spans": [root] + spans,
                    }
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# OTLP/HTTP sender (MLflow ingestion prerequisites: design doc §7)
# ---------------------------------------------------------------------------


def export_run(
    run_dir: Path,
    *,
    endpoint: str,
    experiment_id: str,
    platform_dir: Optional[Path] = None,
    timeout: int = _HTTP_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Build the payload and POST it to ``{endpoint}/v1/traces``.

    MLflow ingestion requires MLflow 3.6.0+ with a SQL backend store, the
    ``x-mlflow-experiment-id`` header, and OTLP/HTTP (no gRPC). All network
    failures surface as :class:`MlflowExportError`.
    """
    base = str(endpoint or "").strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise MlflowExportError("invalid_endpoint", "endpoint must be http(s)://...", retryable=False)
    experiment = str(experiment_id or "").strip()
    if not experiment:
        raise MlflowExportError("missing_experiment_id", "experiment_id is required", retryable=False)

    payload = build_trace_payload(run_dir, platform_dir=platform_dir)
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    if len(body) > _MAX_PAYLOAD_BYTES:
        raise MlflowExportError(
            "payload_too_large",
            "payload exceeds %d bytes" % _MAX_PAYLOAD_BYTES,
            retryable=False,
        )
    request = urllib.request.Request(
        base + "/v1/traces",
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-mlflow-experiment-id": experiment,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
    except urllib.error.HTTPError as exc:
        raise MlflowExportError(
            "mlflow_http_error",
            "MLflow OTLP ingest failed: HTTP %s" % exc.code,
            retryable=exc.code >= 500 or exc.code == 429,
            status=exc.code,
        ) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise MlflowExportError(
            "mlflow_unreachable",
            "MLflow OTLP ingest unreachable: %s" % exc,
            retryable=True,
        ) from exc
    return {
        "status": "exported",
        "http_status": status,
        "bytes": len(body),
        "mapping_version": MAPPING_VERSION,
    }
