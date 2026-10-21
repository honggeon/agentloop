"""MLflow/OTLP export adapter tests (design doc §9 verification points).

All tests are deterministic: no network, no credentials, no .[agent] extra.
The OTLP sender is exercised through a monkeypatched urllib opener only.
"""

import json
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

from auditrail.integrations import mlflow as mlexp
from auditrail.run_contract import new_run_record, write_run_record
from auditrail.trajectory import write_json, write_jsonl


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_run(tmp_path, messages, *, llm_calls=None, run_id="run-test-0001", summary=None, with_run_json=True):
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    session = {"session": {"session_id": "session-test", "model": "deepseek-chat", "messages": messages}}
    write_json(run_dir / "session.json", session)
    doc = {"session_id": "session-test", "model": "deepseek-chat", "message_count": len(messages)}
    if summary:
        doc.update(summary)
    write_json(run_dir / "summary.json", doc)
    if with_run_json:
        write_run_record(run_dir, new_run_record(run_id))
    if llm_calls is not None:
        write_json(run_dir / "llm_calls.json", {"llm_calls": llm_calls})
    return run_dir


def _spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def _root(payload):
    return _spans(payload)[0]


def _children(payload):
    return _spans(payload)[1:]


def _attrs(span):
    out = {}
    for item in span.get("attributes") or []:
        value = item["value"]
        if "stringValue" in value:
            out[item["key"]] = value["stringValue"]
        elif "intValue" in value:
            out[item["key"]] = int(value["intValue"])
        elif "boolValue" in value:
            out[item["key"]] = value["boolValue"]
        elif "doubleValue" in value:
            out[item["key"]] = value["doubleValue"]
    return out


def _by_name(payload, name):
    return [s for s in _spans(payload) if s["name"] == name]


# ---------------------------------------------------------------------------
# §9: idempotency
# ---------------------------------------------------------------------------


def test_export_idempotent(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {
            "role": "assistant",
            "id": "m1",
            "timestamp": "1700000001000",
            "tool_calls": [{"id": "call-1", "function": {"name": "bash", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "tool_name": "bash", "content": "ok", "timestamp": "1700000002000"},
    ]
    run_dir = _write_run(tmp_path, messages)
    first = mlexp.build_trace_payload(run_dir)
    second = mlexp.build_trace_payload(run_dir)
    assert first == second
    ids_first = sorted((s["traceId"], s["spanId"]) for s in _spans(first))
    ids_second = sorted((s["traceId"], s["spanId"]) for s in _spans(second))
    assert ids_first == ids_second


# ---------------------------------------------------------------------------
# §9: missing discipline (no timestamps / llm_calls / tool result; orphan)
# ---------------------------------------------------------------------------


def test_missing_discipline(tmp_path):
    messages = [
        {"role": "user", "content": "do it"},
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "call-open", "function": {"name": "read", "arguments": "{}"}},
                {"function": {"name": "write", "arguments": ""}},  # no id, empty args
            ],
        },
        {"role": "tool", "tool_call_id": "call-orphan", "tool_name": "grep", "content": "orphan evidence"},
        {"role": "assistant", "content": "done"},
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages))
    children = _children(payload)

    # every child uses synthetic ordering time and end != start
    for span in children:
        assert _attrs(span)["auditrail.time_basis"] == "synthetic_order_only"
        assert int(span["endTimeUnixNano"]) > int(span["startTimeUnixNano"])

    # root inherits the weakest time basis
    assert _attrs(_root(payload))["auditrail.time_basis"] == "synthetic_order_only"

    # no usage attributes at all (no llm_calls.json)
    for span in children:
        attrs = _attrs(span)
        assert not [k for k in attrs if k.startswith("gen_ai.usage.")]

    # open tool call keeps missing marker; empty arguments are omitted
    open_spans = _by_name(payload, "read")
    assert open_spans and _attrs(open_spans[0])["auditrail.missing"] == "tool_result"
    write_spans = _by_name(payload, "write")
    assert "gen_ai.tool.call.arguments" not in _attrs(write_spans[0])
    assert "gen_ai.tool.call.id" not in _attrs(write_spans[0])
    assert _attrs(write_spans[0])["auditrail.id_anchor"] == "index"

    # orphan result exported as root child, evidence preserved
    orphan = _by_name(payload, "grep result")
    assert orphan
    orphan_attrs = _attrs(orphan[0])
    assert orphan_attrs["auditrail.orphan"] is True
    assert orphan_attrs["auditrail.missing"] == "tool_call"
    assert "orphan evidence" in orphan_attrs["gen_ai.tool.call.result"]


# ---------------------------------------------------------------------------
# §9: sentinel values (dsh form)
# ---------------------------------------------------------------------------


def test_sentinel_values_dsh_form(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {"role": "assistant", "id": "m1", "content": "hello", "timestamp": "1700000001000"},
    ]
    llm_calls = [
        {
            "call_id": "m1",
            "model": "unknown",
            "provider": "",
            "status": "success",  # hardcoded constant, not evidence
            "token_usage": {"input_tokens": 10, "output_tokens": 5},
        }
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages, llm_calls=llm_calls))
    chat = _by_name(payload, "Assistant Response")
    assert chat
    attrs = _attrs(chat[0])
    assert "gen_ai.request.model" not in attrs
    assert "gen_ai.provider.name" not in attrs
    assert chat[0]["status"]["code"] == 0  # UNSET
    assert attrs["gen_ai.usage.input_tokens"] == 10
    assert attrs["gen_ai.usage.output_tokens"] == 5


# ---------------------------------------------------------------------------
# §9: dual schema (proxy form) — five assertions
# ---------------------------------------------------------------------------


def test_dual_schema_proxy_form(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {"role": "assistant", "content": "hello", "timestamp": "1700000001000"},
    ]
    llm_calls = [
        {
            "callId": "b00b3934-8a82-416f-abb8-8bd001bbb771",  # uuid4 fallback
            "provider": "proxy",
            "model": "deepseek-chat",
            "latency": 1.527,  # SECONDS, not ms
            "tokenUsage": {"inputTokens": 232, "outputTokens": 94, "totalTokens": 326},
            "status": "success",
        }
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages, llm_calls=llm_calls))
    chat = _by_name(payload, "Assistant Response")[0]
    attrs = _attrs(chat)

    # ① latency seconds → ms, no 1000x error
    start_ns = int(chat["startTimeUnixNano"])
    end_ns = int(chat["endTimeUnixNano"])
    assert (end_ns - start_ns) == 1527 * 1_000_000
    assert attrs["auditrail.duration_unit_source"] == "seconds"
    # ② cache attributes omitted (proxy form has none)
    assert "gen_ai.usage.cache_read.input_tokens" not in attrs
    assert "auditrail.usage.cache_write_tokens" not in attrs
    # ③ provider="proxy" omitted
    assert "gen_ai.provider.name" not in attrs
    # ④ uuid fallback anchor labeled synthetic
    assert attrs["auditrail.id_anchor"] == "uuid"
    # usage still mapped
    assert attrs["gen_ai.usage.input_tokens"] == 232


def test_dual_schema_proxy_zero_usage_is_missing(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {"role": "assistant", "content": "hello", "timestamp": "1700000001000"},
    ]
    llm_calls = [
        {
            "callId": "chatcmpl-real-id",
            "provider": "proxy",
            "model": "deepseek-chat",
            "latency": 0.5,
            "tokenUsage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
            "status": "success",
        }
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages, llm_calls=llm_calls))
    attrs = _attrs(_by_name(payload, "Assistant Response")[0])
    # ⑤ all-zero proxy usage → every gen_ai.usage.* attribute omitted
    assert not [k for k in attrs if k.startswith("gen_ai.usage.")]
    # response-id shaped callId is an observed anchor
    assert attrs["auditrail.id_anchor"] == "response_id"


def test_proxy_start_end_times_used(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {"role": "assistant", "content": "hello", "timestamp": "1700000001000"},
    ]
    llm_calls = [
        {
            "callId": "chatcmpl-1",
            "provider": "proxy",
            "model": "deepseek-chat",
            "startTime": "2026-10-21T08:00:00.000Z",
            "endTime": "2026-10-21T08:00:02.500Z",
            "latency": 2.5,
            "tokenUsage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13},
            "status": "success",
        }
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages, llm_calls=llm_calls))
    chat = _by_name(payload, "Assistant Response")[0]
    attrs = _attrs(chat)
    assert attrs["auditrail.time_basis"] == "derived"
    assert attrs["auditrail.duration_basis"] == "derived"
    duration_ns = int(chat["endTimeUnixNano"]) - int(chat["startTimeUnixNano"])
    assert duration_ns == 2500 * 1_000_000


# ---------------------------------------------------------------------------
# §9: root span time discipline
# ---------------------------------------------------------------------------


def test_root_span_time_basis_weakest_wins(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {"role": "assistant", "content": "a", "timestamp": "1700000001000"},
        {"role": "user", "content": "again"},  # no timestamp → synthetic
        {"role": "assistant", "content": "b"},
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages))
    root = _root(payload)
    assert _attrs(root)["auditrail.time_basis"] == "synthetic_order_only"
    child_starts = [int(s["startTimeUnixNano"]) for s in _children(payload)]
    child_ends = [int(s["endTimeUnixNano"]) for s in _children(payload)]
    assert int(root["startTimeUnixNano"]) == min(child_starts)
    assert int(root["endTimeUnixNano"]) == max(child_ends)


# ---------------------------------------------------------------------------
# §9: anchor degradation observability
# ---------------------------------------------------------------------------


def test_anchor_degradation_stats(tmp_path):
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},  # no id → idx anchor
        {"role": "assistant", "tool_calls": [{"id": "call-9", "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call-9", "tool_name": "bash", "content": "ok"},
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages))
    root_attrs = _attrs(_root(payload))
    stats = json.loads(root_attrs["auditrail.id_anchor_stats"])
    assert stats.get("index", 0) >= 1
    assert stats.get("tool") == 1
    chat = _by_name(payload, "Assistant Response")[0]
    assert _attrs(chat)["auditrail.id_anchor"] == "index"


# ---------------------------------------------------------------------------
# §9: status priority (heuristic never overrides observed negative)
# ---------------------------------------------------------------------------


def test_status_observed_flag_beats_heuristic(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {
            "role": "assistant",
            "timestamp": "1700000001000",
            "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}],
        },
        # content mentions errors, but observed flag says fine
        {"role": "tool", "tool_call_id": "c1", "tool_name": "bash",
         "content": "error handling docs", "is_error": False, "timestamp": "1700000002000"},
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages))
    tool = _by_name(payload, "bash")[0]
    assert tool["status"]["code"] == 1  # OK
    assert _attrs(tool)["auditrail.status_basis"] == "observed_flag"


def test_status_exit_code_marker_and_heuristic(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {
            "role": "assistant",
            "timestamp": "1700000001000",
            "tool_calls": [
                {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
                {"id": "c2", "function": {"name": "read", "arguments": "{}"}},
                {"id": "c3", "function": {"name": "glob", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "tool_name": "bash",
         "content": "tests mention error strings\n\n[exit code: 0]", "timestamp": "1700000002000"},
        {"role": "tool", "tool_call_id": "c2", "tool_name": "read",
         "content": "Error executing tool: file not found", "timestamp": "1700000003000"},
        {"role": "tool", "tool_call_id": "c3", "tool_name": "glob",
         "content": "Error: file not found", "timestamp": "1700000004000"},
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages))
    bash = _by_name(payload, "bash")[0]
    read = _by_name(payload, "read")[0]
    assert bash["status"]["code"] == 1  # explicit zero exit code wins over text
    assert _attrs(bash)["auditrail.status_basis"] == "derived_exit_code_marker"
    assert read["status"]["code"] == 2  # heuristic fallback
    assert _attrs(read)["auditrail.status_basis"] == "heuristic"
    # bare "Error:" text is not a heuristic signal (audit.py is conservative):
    # no signal → UNSET, never a guessed OK
    glob_tool = _by_name(payload, "glob")[0]
    assert glob_tool["status"]["code"] == 0
    assert _attrs(glob_tool)["auditrail.status_basis"] == "none"


# ---------------------------------------------------------------------------
# token usage: DSH cache mapping and semconv divergence marker
# ---------------------------------------------------------------------------


def test_dsh_cache_usage_mapping(tmp_path):
    messages = [
        {"role": "user", "content": "hi", "timestamp": "1700000000000"},
        {"role": "assistant", "id": "m1", "content": "hello", "timestamp": "1700000001000"},
    ]
    llm_calls = [
        {
            "call_id": "m1",
            "model": "deepseek-chat",
            "provider": "DeepSeek",
            "status": "success",
            "token_usage": {
                "input_tokens": 232,
                "output_tokens": 94,
                "total_tokens": 11718,
                "cache_read_tokens": 11392,
                "cache_write_tokens": 0,
            },
            "latency_ms": 1527,
        }
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages, llm_calls=llm_calls))
    attrs = _attrs(_by_name(payload, "Assistant Response")[0])
    assert attrs["gen_ai.usage.input_tokens"] == 232
    assert attrs["gen_ai.usage.output_tokens"] == 94
    assert attrs["gen_ai.usage.cache_read.input_tokens"] == 11392
    assert attrs["auditrail.usage.input_excludes_cache"] is True
    assert attrs["auditrail.usage.cache_write_tokens"] == 0
    # provider normalized to semconv lowercase enum
    assert attrs["gen_ai.provider.name"] == "deepseek"
    assert attrs["auditrail.provenance.provider"] == "derived"
    # latency_ms marked derived
    assert attrs["auditrail.provenance.duration"] == "derived"


# ---------------------------------------------------------------------------
# multi-turn user input grouping
# ---------------------------------------------------------------------------


def test_user_input_grouping(tmp_path):
    messages = [
        {"role": "user", "content": "first", "timestamp": "1700000000000"},
        {"role": "user", "content": "second", "timestamp": "1700000000500"},
        {"role": "assistant", "content": "answer", "timestamp": "1700000001000"},
    ]
    payload = mlexp.build_trace_payload(_write_run(tmp_path, messages))
    root_attrs = _attrs(_root(payload))
    grouped = json.loads(root_attrs["gen_ai.input.messages"])
    assert [m["content"] for m in grouped] == ["first", "second"]
    assert [m["auditrail.turn_index"] for m in grouped] == [0, 1]
    assert root_attrs["auditrail.provenance.input_grouping"] == "derived"


# ---------------------------------------------------------------------------
# cost attachment (optional platform dir)
# ---------------------------------------------------------------------------


def test_cost_attachment(tmp_path):
    run_dir = _write_run(
        tmp_path,
        [
            {"role": "user", "content": "hi", "timestamp": "1700000000000"},
            {"role": "assistant", "content": "hello", "timestamp": "1700000001000"},
        ],
    )
    platform_dir = tmp_path / "platform"
    platform_dir.mkdir()
    record = {
        "run_id": "run-test-0001",
        "amount": 0.0123,
        "currency": "USD",
        "source": "estimated",
        "price_version": "prices-2026-10",
        "source_system": "dsh",
        "source_id": "m1",
    }
    (platform_dir / "cost_records.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    payload = mlexp.build_trace_payload(run_dir, platform_dir=platform_dir)
    root_attrs = _attrs(_root(payload))
    assert root_attrs["auditrail.cost.source"] == "estimated"
    assert root_attrs["auditrail.cost.price_version"] == "prices-2026-10"

    # without platform dir there are no cost attributes
    payload_plain = mlexp.build_trace_payload(run_dir)
    assert not [k for k in _attrs(_root(payload_plain)) if k.startswith("auditrail.cost.")]


# ---------------------------------------------------------------------------
# §9: sender via fake exporter (no real network)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_export_run_posts_to_mlflow(tmp_path, monkeypatch):
    run_dir = _write_run(
        tmp_path,
        [
            {"role": "user", "content": "hi", "timestamp": "1700000000000"},
            {"role": "assistant", "content": "hello", "timestamp": "1700000001000"},
        ],
    )
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeResponse(200)

    monkeypatch.setattr(mlexp.urllib.request, "urlopen", fake_urlopen)
    result = mlexp.export_run(run_dir, endpoint="http://mlflow.local:5000/", experiment_id="42")
    assert result["status"] == "exported"
    assert captured["url"] == "http://mlflow.local:5000/v1/traces"
    assert captured["headers"].get("X-mlflow-experiment-id") == "42"
    assert captured["body"]["resourceSpans"][0]["scopeSpans"][0]["scope"]["version"] == "otel-map-1"


def test_export_run_http_error_explicit(tmp_path, monkeypatch):
    run_dir = _write_run(
        tmp_path,
        [{"role": "user", "content": "hi", "timestamp": "1700000000000"}],
    )

    def fake_urlopen(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, BytesIO(b""))

    monkeypatch.setattr(mlexp.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(mlexp.MlflowExportError) as err:
        mlexp.export_run(run_dir, endpoint="http://mlflow.local:5000", experiment_id="42")
    assert err.value.code == "mlflow_http_error"
    assert err.value.retryable is True
    assert err.value.status == 503


def test_export_run_validates_inputs(tmp_path):
    run_dir = _write_run(tmp_path, [{"role": "user", "content": "hi"}])
    with pytest.raises(mlexp.MlflowExportError):
        mlexp.export_run(run_dir, endpoint="not-a-url", experiment_id="1")
    with pytest.raises(mlexp.MlflowExportError):
        mlexp.export_run(run_dir, endpoint="http://x", experiment_id="")
