"""Phase 3 Langfuse adapter: normalization, association, import pipeline, SSRF."""

import json
import sys

import pytest

from auditrail import netguard
from auditrail.integrations import external_models as ext
from auditrail.integrations import langfuse


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _context(**overrides):
    context = langfuse.new_run_context()
    for key, value in overrides.items():
        context[key] = value
    return context


def _trace(trace_id, **extra):
    raw = {"id": trace_id, "timestamp": "2026-01-05T10:00:01Z"}
    raw.update(extra)
    return raw


# ---------------------------------------------------------------------------
# association (link_trace)
# ---------------------------------------------------------------------------


def test_link_via_metadata_run_id_is_exact():
    trace = langfuse.normalize_trace(_trace("t-1", metadata={"run_id": "run-1"}))
    result = langfuse.link_trace(trace, _context(run_ids={"run-1"}))
    assert result["confidence"] == "exact"
    assert result["method"] == "run_id"
    assert result["run_ids"] == ["run-1"]
    assert result["trace"]["run_id"] == "run-1"
    assert result["trace"]["link"]["confidence"] == "exact"
    assert result["trace"]["import_status"] == "imported"


def test_link_via_session_id_is_strong():
    trace = langfuse.normalize_trace(_trace("t-2", sessionId="sess-1"))
    result = langfuse.link_trace(trace, _context(run_ids={"run-1"}, sessions={"sess-1": "run-1"}))
    assert result["confidence"] == "strong"
    assert result["method"] == "session_id"
    assert result["run_ids"] == ["run-1"]


def test_link_via_agent_time_window_is_weak():
    trace = langfuse.normalize_trace(
        _trace("t-3", metadata={"agent_id": "agent-1"}, timestamp="2026-01-05T10:00:01Z")
    )
    context = _context(
        run_ids={"run-1"},
        agent_runs={
            "agent-1": [
                {"run_id": "run-1", "started_at": "2026-01-05T10:00:00Z", "finished_at": "2026-01-05T10:00:02Z"}
            ]
        },
    )
    result = langfuse.link_trace(trace, context)
    assert result["confidence"] == "weak"
    assert result["method"] == "agent_time_window"
    assert result["run_ids"] == ["run-1"]
    assert result["trace"]["link"]["run_ids"] == ["run-1"]


def test_link_agent_time_window_multiple_candidates_all_kept_with_warning():
    trace = langfuse.normalize_trace(
        _trace("t-4", metadata={"agent_id": "agent-1"}, timestamp="2026-01-05T10:00:01Z")
    )
    context = _context(
        run_ids={"run-1", "run-2"},
        agent_runs={
            "agent-1": [
                {"run_id": "run-1", "started_at": "2026-01-05T10:00:00Z", "finished_at": "2026-01-05T10:00:02Z"},
                {"run_id": "run-2", "started_at": "2026-01-05T10:00:00Z", "finished_at": "2026-01-05T10:00:03Z"},
            ]
        },
    )
    result = langfuse.link_trace(trace, context)
    assert result["confidence"] == "weak"
    assert sorted(result["run_ids"]) == ["run-1", "run-2"]
    assert sorted(result["trace"]["link"]["run_ids"]) == ["run-1", "run-2"]
    assert any("weak time-window" in warning for warning in result["warnings"])
    assert any("weak time-window" in warning for warning in result["trace"]["warnings"])


def test_link_via_operator_source_id_map_is_strong():
    trace = langfuse.normalize_trace(_trace("t-5"))
    result = langfuse.link_trace(
        trace, _context(run_ids={"run-9"}, source_id_map={"t-5": "run-9"})
    )
    assert result["confidence"] == "strong"
    assert result["method"] == "source_id_map"
    assert result["run_ids"] == ["run-9"]


def test_unlinkable_trace_becomes_unlinked_external_trace():
    trace = langfuse.normalize_trace(_trace("t-6"))
    result = langfuse.link_trace(trace, _context(run_ids={"run-1"}))
    assert result["confidence"] == "unlinked"
    assert result["run_ids"] == []
    trace = result["trace"]
    assert trace["run_id"] is None
    assert trace["import_status"] == "unlinked"
    assert trace["link"]["confidence"] == "unlinked"
    # the association hint is stripped before storage
    assert "_session_id" not in trace


# ---------------------------------------------------------------------------
# import pipeline (traces)
# ---------------------------------------------------------------------------


def test_import_traces_unlinked_records_kept_and_counted(tmp_path):
    platform_dir = tmp_path / "platform"
    source = tmp_path / "imports" / "traces.jsonl"
    _write_jsonl(source, [_trace("t-1")])
    report = langfuse.import_traces(platform_dir, {"kind": "file", "path": str(source)}, allowed_roots=[tmp_path])
    assert report["status"] == "imported"
    assert report["total_records"] == 1
    assert report["imported_records"] == 1
    assert report["unlinked_records"] == 1
    stored = ext.load_external_traces(platform_dir)
    assert len(stored) == 1
    assert stored[0]["source_id"] == "t-1"
    assert stored[0]["run_id"] is None
    assert stored[0]["import_status"] == "unlinked"


def test_import_traces_many_to_one_run_kept(tmp_path):
    platform_dir = tmp_path / "platform"
    source = tmp_path / "imports" / "traces.jsonl"
    _write_jsonl(
        source,
        [
            _trace("t-1", metadata={"run_id": "run-1"}),
            _trace("t-2", metadata={"run_id": "run-1"}),
        ],
    )
    context = _context(run_ids={"run-1"})
    report = langfuse.import_traces(
        platform_dir, {"kind": "file", "path": str(source)}, allowed_roots=[tmp_path], run_context=context
    )
    assert report["imported_records"] == 2
    stored = ext.load_external_traces(platform_dir)
    assert len(stored) == 2
    assert all(record["run_id"] == "run-1" for record in stored)
    assert {record["source_id"] for record in stored} == {"t-1", "t-2"}


def test_import_traces_idempotent_on_source_id(tmp_path):
    platform_dir = tmp_path / "platform"
    source = tmp_path / "imports" / "traces.jsonl"
    _write_jsonl(source, [_trace("t-1", metadata={"run_id": "run-1"})])
    context = _context(run_ids={"run-1"})
    first = langfuse.import_traces(
        platform_dir, {"kind": "file", "path": str(source)}, allowed_roots=[tmp_path], run_context=context
    )
    second = langfuse.import_traces(
        platform_dir, {"kind": "file", "path": str(source)}, allowed_roots=[tmp_path], run_context=context
    )
    assert first["imported_records"] == 1
    assert second["duplicate_records"] == 1
    assert second["imported_records"] == 0
    assert len(ext.load_external_traces(platform_dir)) == 1


def test_import_traces_bad_record_does_not_block_others(tmp_path):
    platform_dir = tmp_path / "platform"
    source = tmp_path / "imports" / "traces.jsonl"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        '{"id": "t-1", "metadata": {"run_id": "run-1"}}\n'
        "{not valid json\n"
        '{"name": "missing id"}\n',
        encoding="utf-8",
    )
    report = langfuse.import_traces(
        platform_dir,
        {"kind": "file", "path": str(source)},
        allowed_roots=[tmp_path],
        run_context=_context(run_ids={"run-1"}),
    )
    # one good record imported, one invalid record, one bad JSONL line skipped
    assert report["status"] == "partial"
    assert report["imported_records"] == 1
    assert report["invalid_records"] == 1
    assert any("not valid JSON" in warning for warning in report["warnings"])
    assert len(ext.load_external_traces(platform_dir)) == 1


def test_import_traces_dry_run_does_not_persist(tmp_path):
    platform_dir = tmp_path / "platform"
    source = tmp_path / "imports" / "traces.jsonl"
    _write_jsonl(source, [_trace("t-1", metadata={"run_id": "run-1"})])
    report = langfuse.import_traces(
        platform_dir,
        {"kind": "file", "path": str(source)},
        allowed_roots=[tmp_path],
        options={"dry_run": True},
        run_context=_context(run_ids={"run-1"}),
    )
    assert report["status"] == "dry_run"
    assert report["imported_records"] == 1
    assert ext.load_external_traces(platform_dir) == []
    assert not (platform_dir / "external" / "traces.jsonl").exists()


# ---------------------------------------------------------------------------
# import pipeline (scores)
# ---------------------------------------------------------------------------


def _import_linked_trace(platform_dir, tmp_path, trace_id="t-1", run_id="run-1"):
    source = tmp_path / "imports" / "traces.jsonl"
    _write_jsonl(source, [_trace(trace_id, metadata={"run_id": run_id})])
    return langfuse.import_traces(
        platform_dir,
        {"kind": "file", "path": str(source)},
        allowed_roots=[tmp_path],
        run_context=_context(run_ids={run_id}),
    )


def test_import_scores_linked_via_trace_id_strong(tmp_path):
    platform_dir = tmp_path / "platform"
    _import_linked_trace(platform_dir, tmp_path)
    scores = tmp_path / "imports" / "scores.jsonl"
    _write_jsonl(
        scores,
        [{"id": "s-1", "traceId": "t-1", "name": "quality", "value": 0.9, "dataType": "NUMERIC"}],
    )
    report = langfuse.import_scores(platform_dir, {"kind": "file", "path": str(scores)}, allowed_roots=[tmp_path])
    assert report["status"] == "imported"
    assert report["imported_records"] == 1
    stored = ext.load_external_evaluations(platform_dir)
    assert len(stored) == 1
    evaluation = stored[0]
    assert evaluation["run_id"] == "run-1"
    assert evaluation["score"] == 0.9
    assert evaluation["provenance"]["confidence"] == "strong"
    assert evaluation["provenance"]["association"] == "trace:t-1"


def test_import_scores_never_touch_run_artifacts(tmp_path):
    platform_dir = tmp_path / "platform"
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    run_dir.mkdir(parents=True)
    judge = {"overall_status": "success", "completion_score": 88}
    evaluation_json = {"status": "completed", "final_quality": {"status": "success"}}
    (run_dir / "agent_judge.json").write_text(json.dumps(judge), encoding="utf-8")
    (run_dir / "evaluation.json").write_text(json.dumps(evaluation_json), encoding="utf-8")
    judge_before = (run_dir / "agent_judge.json").read_bytes()
    evaluation_before = (run_dir / "evaluation.json").read_bytes()

    scores = tmp_path / "imports" / "scores.jsonl"
    _write_jsonl(
        scores,
        [{"id": "s-1", "traceId": "t-1", "run_id": "run-1", "name": "quality", "value": 1.0}],
    )
    report = langfuse.import_scores(platform_dir, {"kind": "file", "path": str(scores)}, allowed_roots=[tmp_path])
    assert report["imported_records"] == 1
    # external evidence lives only under platform/external/; run artifacts are byte-identical
    assert (run_dir / "agent_judge.json").read_bytes() == judge_before
    assert (run_dir / "evaluation.json").read_bytes() == evaluation_before
    assert not (run_dir / "external").exists()
    assert (platform_dir / "external" / "evaluations.jsonl").is_file()


def test_import_scores_unlinked_score_kept(tmp_path):
    platform_dir = tmp_path / "platform"
    scores = tmp_path / "imports" / "scores.jsonl"
    _write_jsonl(scores, [{"id": "s-1", "traceId": "ghost", "name": "quality", "value": 0.5}])
    report = langfuse.import_scores(platform_dir, {"kind": "file", "path": str(scores)}, allowed_roots=[tmp_path])
    assert report["imported_records"] == 1
    assert report["unlinked_records"] == 1
    stored = ext.load_external_evaluations(platform_dir)
    assert stored[0]["run_id"] is None


def test_import_scores_boolean_and_categorical_shapes(tmp_path):
    platform_dir = tmp_path / "platform"
    scores = tmp_path / "imports" / "scores.jsonl"
    _write_jsonl(
        scores,
        [
            {"id": "s-b", "traceId": "t-1", "name": "is_correct", "value": 1, "dataType": "BOOLEAN"},
            {"id": "s-c", "traceId": "t-1", "name": "grade", "stringValue": "good", "dataType": "CATEGORICAL"},
        ],
    )
    report = langfuse.import_scores(platform_dir, {"kind": "file", "path": str(scores)}, allowed_roots=[tmp_path])
    assert report["imported_records"] == 2
    by_id = {record["source_id"]: record for record in ext.load_external_evaluations(platform_dir)}
    boolean = by_id["s-b"]
    assert boolean["score_min"] == 0.0
    assert boolean["score_max"] == 1.0
    assert boolean["label"] == "true"
    assert boolean["status"] == "pass"
    categorical = by_id["s-c"]
    assert categorical["score"] is None
    assert categorical["label"] == "good"


# ---------------------------------------------------------------------------
# SSRF / config validation
# ---------------------------------------------------------------------------


def test_validate_config_rejects_ssrf_endpoints():
    for bad in ("http://169.254.169.254", "http://127.0.0.1", "ftp://example.com"):
        with pytest.raises(ValueError):
            langfuse.validate_config({"enabled": True, "base_url": bad})


def test_validate_config_rejects_unknown_keys_and_bad_token_env():
    with pytest.raises(ValueError):
        langfuse.validate_config({"enabled": True, "bogus": 1})
    with pytest.raises(ValueError):
        langfuse.validate_config({"token_env": "NOT A NAME"})
    with pytest.raises(ValueError):
        langfuse.validate_config("not-a-dict")


def test_validate_config_defaults_disabled():
    normalized = langfuse.validate_config({})
    assert normalized == {"enabled": False, "base_url": None, "token_env": None}


def test_validate_config_allowed_host_with_injected_resolver():
    policy = netguard.EndpointPolicy(
        allowed_hosts=("langfuse.example",),
        resolver=lambda host, port: [(2, 1, 6, "", ("93.184.216.34", port))],
    )
    normalized = langfuse.validate_config(
        {"enabled": True, "base_url": "https://langfuse.example", "token_env": "LF_TOKEN"},
        policy=policy,
    )
    assert normalized["enabled"] is True
    assert normalized["base_url"] == "https://langfuse.example:443"
    assert normalized["token_env"] == "LF_TOKEN"


def test_http_import_disabled_by_default(tmp_path):
    with pytest.raises(ValueError, match="disabled"):
        langfuse.read_source_records({"kind": "http"}, kind="traces", allowed_roots=[tmp_path], config={})


def test_http_import_enabled_requires_base_url(tmp_path):
    with pytest.raises(ValueError):
        langfuse.read_source_records({"kind": "http"}, kind="traces", allowed_roots=[tmp_path], config={"enabled": True})


# ---------------------------------------------------------------------------
# HTTP import via injected fetcher (no real network)
# ---------------------------------------------------------------------------


def test_http_import_uses_injected_fetcher_and_never_leaks_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITRAIL_TEST_LF_TOKEN", "lf-super-secret-token")
    calls = []

    def fake_fetch(url, token):
        calls.append((url, token))
        assert token == "lf-super-secret-token"
        page = int(url.rsplit("page=", 1)[1])
        if page == 1:
            return {
                "data": [{"id": "t-http-1", "metadata": {"run_id": "run-1"}}],
                "meta": {"totalPages": 2},
            }
        return {"data": [{"id": "t-http-2"}], "meta": {"totalPages": 2}}

    config = {
        "enabled": True,
        "base_url": "https://langfuse.example",
        "token_env": "AUDITRAIL_TEST_LF_TOKEN",
        "allowed_hosts": ["langfuse.example"],
    }
    report = langfuse.import_traces(
        tmp_path / "platform",
        {"kind": "http"},
        allowed_roots=[tmp_path],
        config=config,
        run_context=_context(run_ids={"run-1"}),
        fetcher=fake_fetch,
    )
    # paginated through both pages without any real network call
    assert len(calls) == 2
    assert calls[0][0].endswith("/api/public/traces?limit=100&page=1")
    assert calls[1][0].endswith("/api/public/traces?limit=100&page=2")
    assert report["imported_records"] == 2
    stored = ext.load_external_traces(tmp_path / "platform")
    assert {record["source_id"] for record in stored} == {"t-http-1", "t-http-2"}
    # the token value never lands in the report or the stored records
    assert "lf-super-secret-token" not in json.dumps(report, ensure_ascii=False)
    assert "lf-super-secret-token" not in (tmp_path / "platform" / "external" / "traces.jsonl").read_text(
        encoding="utf-8"
    )


def test_http_fetch_failure_is_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDITRAIL_TEST_LF_TOKEN", "lf-super-secret-token")

    def failing_fetch(url, token):
        raise RuntimeError("boom with token %s" % token)

    config = {
        "enabled": True,
        "base_url": "https://langfuse.example",
        "token_env": "AUDITRAIL_TEST_LF_TOKEN",
        "allowed_hosts": ["langfuse.example"],
    }
    with pytest.raises(ValueError) as excinfo:
        langfuse.import_traces(
            tmp_path / "platform",
            {"kind": "http"},
            allowed_roots=[tmp_path],
            config=config,
            fetcher=failing_fetch,
        )
    assert "lf-super-secret-token" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# no SDK / yaml dependency
# ---------------------------------------------------------------------------


def test_adapter_needs_no_langfuse_sdk_or_yaml():
    assert "langfuse" not in sys.modules  # the SDK is never imported
    # the adapter module itself is importable without yaml installed
    assert sys.modules["auditrail.integrations.langfuse"] is langfuse
