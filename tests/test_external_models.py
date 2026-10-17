"""Phase 3 unified external evidence model: normalization, merge, summary."""

import pytest

from auditrail.integrations import external_models as ext


# ---------------------------------------------------------------------------
# normalize_external_evaluation
# ---------------------------------------------------------------------------


def test_normalize_evaluation_full_fields():
    record = ext.normalize_external_evaluation(
        {
            "source_id": "score-1",
            "source_version": "v2",
            "run_id": "run-1",
            "agent_id": "agent-1",
            "suite_id": "suite-1",
            "case_id": "case-1",
            "metric_name": "helpfulness",
            "status": "pass",
            "score": 0.9,
            "score_min": 0.0,
            "score_max": 1.0,
            "label": "good",
            "comment": "looks fine",
            "metadata": {"note": "safe"},
        },
        source_system="langfuse",
        imported_at="2026-01-05T10:00:00Z",
    )
    assert record["schema_version"] == ext.SCHEMA_VERSION
    assert record["evaluation_id"].startswith("ext-eval-")
    assert record["source_system"] == "langfuse"
    assert record["source_id"] == "score-1"
    assert record["source_version"] == "v2"
    assert record["run_id"] == "run-1"
    assert record["agent_id"] == "agent-1"
    assert record["suite_id"] == "suite-1"
    assert record["case_id"] == "case-1"
    assert record["metric_name"] == "helpfulness"
    assert record["status"] == "pass"
    assert record["score"] == 0.9
    assert record["label"] == "good"
    assert record["comment"] == "looks fine"
    assert record["metadata"] == {"note": "safe"}
    assert record["provenance"]["source_system"] == "langfuse"
    assert record["provenance"]["source_id"] == "score-1"
    assert record["provenance"]["imported_at"] == "2026-01-05T10:00:00Z"
    assert record["provenance"]["association"] == "run_id"
    assert record["provenance"]["confidence"] == "exact"
    assert record["warnings"] == []


def test_normalize_evaluation_score_null_stays_null():
    record = ext.normalize_external_evaluation(
        {"source_id": "s-1", "run_id": "run-1", "score": None},
        source_system="langfuse",
    )
    assert record["score"] is None
    # ``value`` is the Langfuse-style fallback for the numeric score
    record = ext.normalize_external_evaluation(
        {"source_id": "s-2", "run_id": "run-1", "value": 4},
        source_system="langfuse",
    )
    assert record["score"] == 4.0


def test_normalize_evaluation_score_range_preserved_explicitly():
    record = ext.normalize_external_evaluation(
        {"source_id": "s-1", "run_id": "run-1", "score": 7, "score_min": 0, "score_max": 10},
        source_system="human_feedback",
    )
    # the original 0-10 range is kept; never reinterpreted as 0-100
    assert record["score"] == 7.0
    assert record["score_min"] == 0.0
    assert record["score_max"] == 10.0
    assert "completion_score" not in record


def test_normalize_evaluation_missing_fields_not_fabricated():
    record = ext.normalize_external_evaluation(
        {"source_id": "s-1"},
        source_system="langfuse",
    )
    assert record["run_id"] is None
    assert record["agent_id"] is None
    assert record["suite_id"] is None
    assert record["case_id"] is None
    assert record["score"] is None
    assert record["score_min"] is None
    assert record["score_max"] is None
    assert record["label"] is None
    assert record["comment"] is None
    assert record["metric_name"] == "unknown"
    assert record["status"] == "unknown"
    assert record["provenance"]["confidence"] == "unlinked"
    assert record["provenance"]["association"] == "none"
    assert record["metadata"] == {}


def test_normalize_evaluation_invalid_status_becomes_unknown():
    record = ext.normalize_external_evaluation(
        {"source_id": "s-1", "run_id": "run-1", "status": "excellent"},
        source_system="langfuse",
    )
    assert record["status"] == "unknown"


def test_normalize_evaluation_comment_truncated():
    record = ext.normalize_external_evaluation(
        {"source_id": "s-1", "run_id": "run-1", "comment": "x" * (ext.MAX_COMMENT_LENGTH + 100)},
        source_system="langfuse",
    )
    assert record["comment"] is not None
    assert len(record["comment"]) == ext.MAX_COMMENT_LENGTH


def test_normalize_evaluation_chain_of_thought_comment_dropped():
    for fragment in ("chain-of-thought", "chain_of_thought", "<thinking>", "system prompt:"):
        record = ext.normalize_external_evaluation(
            {"source_id": "s-1", "run_id": "run-1", "comment": "here is my %s dump ..." % fragment},
            source_system="langfuse",
        )
        assert record["comment"] is None, fragment


def test_normalize_evaluation_metadata_redacted():
    record = ext.normalize_external_evaluation(
        {
            "source_id": "s-1",
            "run_id": "run-1",
            "metadata": {
                "authorization": "Bearer topsecret",
                "api_token": "sk-abcdef",
                "note": "safe",
            },
        },
        source_system="langfuse",
    )
    assert record["metadata"] == {"note": "safe"}
    assert "topsecret" not in str(record)
    assert "sk-abcdef" not in str(record)


def test_normalize_evaluation_requires_source_id():
    with pytest.raises(ValueError):
        ext.normalize_external_evaluation({"run_id": "run-1"}, source_system="langfuse")
    with pytest.raises(ValueError):
        ext.normalize_external_evaluation("not-a-dict", source_system="langfuse")
    with pytest.raises(ValueError):
        ext.normalize_external_evaluation({"source_id": "s-1"}, source_system="")


def test_normalize_evaluation_id_deterministic():
    raw = {"source_id": "s-1", "run_id": "run-1"}
    first = ext.normalize_external_evaluation(raw, source_system="langfuse", imported_at="2026-01-05T10:00:00Z")
    second = ext.normalize_external_evaluation(raw, source_system="langfuse", imported_at="2026-01-05T10:00:00Z")
    assert first["evaluation_id"] == second["evaluation_id"]


# ---------------------------------------------------------------------------
# normalize_external_trace
# ---------------------------------------------------------------------------


def test_normalize_trace_tokens_cost_link_structure():
    record = ext.normalize_external_trace(
        {
            "source_id": "trace-1",
            "run_id": "run-1",
            "agent_id": "agent-1",
            "name": "chat",
            "started_at": "2026-01-05T10:00:00Z",
            "ended_at": "2026-01-05T10:00:02Z",
            "latency_ms": 2000,
            "tokens": {"input": 100, "output": 50, "total": 150},
            "cost": {"value": 0.0042, "currency": "USD", "source": "observed"},
        },
        source_system="langfuse",
        imported_at="2026-01-05T10:05:00Z",
    )
    assert record["trace_id"].startswith("ext-trace-")
    assert record["source_id"] == "trace-1"
    assert record["tokens"] == {"input": 100, "output": 50, "total": 150}
    assert record["cost"] == {"value": 0.0042, "currency": "USD", "source": "observed"}
    assert record["link"]["method"] == "run_id"
    assert record["link"]["confidence"] == "exact"
    assert record["link"]["run_ids"] == ["run-1"]
    assert record["import_status"] == "imported"
    assert record["imported_at"] == "2026-01-05T10:05:00Z"
    # the external trace id is only a source_id, never presented as run_id
    assert record["run_id"] == "run-1"


def test_normalize_trace_cost_missing_stays_missing():
    record = ext.normalize_external_trace(
        {"source_id": "trace-1", "run_id": "run-1", "tokens": {"total": 10}},
        source_system="langfuse",
    )
    assert record["cost"]["value"] is None
    assert record["cost"]["currency"] is None
    assert record["cost"]["source"] == "missing"


def test_normalize_trace_without_run_id_is_unlinked():
    record = ext.normalize_external_trace(
        {"source_id": "trace-1", "name": "orphan"},
        source_system="langfuse",
    )
    assert record["run_id"] is None
    assert record["link"]["confidence"] == "unlinked"
    assert record["link"]["run_ids"] == []
    assert record["import_status"] == "unlinked"


def test_normalize_trace_requires_source_id():
    with pytest.raises(ValueError):
        ext.normalize_external_trace({"name": "x"}, source_system="langfuse")


def test_mark_trace_unlinked_keeps_record():
    record = ext.normalize_external_trace(
        {"source_id": "trace-1", "run_id": "run-1"},
        source_system="langfuse",
    )
    marked = ext.mark_trace_unlinked(record, reason="no platform run association found")
    assert marked["run_id"] is None
    assert marked["import_status"] == "unlinked"
    assert marked["link"]["confidence"] == "unlinked"
    assert any("no platform run" in warning for warning in marked["warnings"])


# ---------------------------------------------------------------------------
# merge_external_records
# ---------------------------------------------------------------------------


def _record(source_id, **extra):
    base = {"source_system": "langfuse", "source_id": source_id, "imported_at": "2026-01-05T10:00:00Z"}
    base.update(extra)
    return base


def test_merge_identical_records_is_idempotent():
    existing = [_record("t-1", value=1)]
    incoming = [_record("t-1", value=1, imported_at="2026-01-06T10:00:00Z")]
    merged, duplicates, warnings = ext.merge_external_records(existing, incoming, id_field="trace")
    assert duplicates == 1
    assert warnings == []
    assert len(merged) == 1
    assert merged[0]["imported_at"] == "2026-01-05T10:00:00Z"


def test_merge_conflicting_record_keeps_original_and_warns():
    existing = [_record("t-1", value=1)]
    incoming = [_record("t-1", value=999)]
    merged, duplicates, warnings = ext.merge_external_records(existing, incoming, id_field="trace")
    assert duplicates == 1
    assert len(merged) == 1
    assert merged[0]["value"] == 1
    assert len(warnings) == 1
    assert "conflicting" in warnings[0]


def test_merge_new_records_appended():
    merged, duplicates, warnings = ext.merge_external_records(
        [_record("t-1")], [_record("t-2")], id_field="trace"
    )
    assert duplicates == 0
    assert warnings == []
    assert {record["source_id"] for record in merged} == {"t-1", "t-2"}


# ---------------------------------------------------------------------------
# external_summary
# ---------------------------------------------------------------------------


def test_external_summary_counts_and_sources():
    evaluations = [
        {"source_system": "langfuse", "run_id": "run-1"},
        {"source_system": "promptfoo", "run_id": None},
    ]
    traces = [
        {"source_system": "langfuse", "run_id": "run-1"},
        {"source_system": "langfuse", "run_id": None},
    ]
    summary = ext.external_summary(evaluations, traces)
    assert summary["count"] == 4
    assert summary["evaluation_count"] == 2
    assert summary["trace_count"] == 2
    assert summary["linked_count"] == 1
    assert summary["unlinked_count"] == 1
    assert summary["source_systems"] == ["langfuse", "promptfoo"]


def test_external_summary_empty():
    summary = ext.external_summary([], [])
    assert summary["count"] == 0
    assert summary["linked_count"] == 0
    assert summary["unlinked_count"] == 0
    assert summary["source_systems"] == []


# ---------------------------------------------------------------------------
# persistent store round-trip
# ---------------------------------------------------------------------------


def test_store_round_trip(tmp_path):
    evaluations = [_record("s-1", run_id="run-1")]
    traces = [_record("t-1", run_id=None)]
    ext.save_external_evaluations(tmp_path, evaluations)
    ext.save_external_traces(tmp_path, traces)
    assert ext.load_external_evaluations(tmp_path) == evaluations
    assert ext.load_external_traces(tmp_path) == traces
    assert ext.evaluations_for_run(ext.load_external_evaluations(tmp_path), "run-1") == evaluations
    assert ext.traces_for_run(ext.load_external_traces(tmp_path), "run-1") == []


def test_import_report_round_trip(tmp_path):
    report = ext.new_import_report("langfuse")
    report["total_records"] = 2
    report["imported_records"] = 1
    ext.record_import_error(report, 2, "bad record")
    ext.finalize_import_report(report)
    assert report["status"] == "partial"
    ext.save_external_import_report(tmp_path, report)
    loaded = ext.load_external_import_report(tmp_path, report["import_id"])
    assert loaded["import_id"] == report["import_id"]
    assert loaded["status"] == "partial"
    assert ext.load_external_import_report(tmp_path, "missing") is None
    assert ext.load_external_import_report(tmp_path, "../escape") is None
    listed = ext.list_external_import_reports(tmp_path)
    assert [item["import_id"] for item in listed] == [report["import_id"]]
