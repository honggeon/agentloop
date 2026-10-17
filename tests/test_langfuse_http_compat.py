"""Phase 4 Langfuse compatibility: cursor/page pagination, schema variants,
partial errors, retry classification, cancel between pages, token hygiene."""

import json

import pytest

from auditrail.integrations import external_models as ext
from auditrail.integrations import langfuse


def _config():
    return {
        "enabled": True,
        "base_url": "https://langfuse.example",
        "token_env": "AUDITRAIL_TEST_LF_TOKEN",
        "allowed_hosts": ["langfuse.example"],
    }


# ---------------------------------------------------------------------------
# pagination variants
# ---------------------------------------------------------------------------


def test_page_token_pagination_via_total_pages():
    pages = {
        1: {"data": [{"id": "t-1"}], "meta": {"totalPages": 2}},
        2: {"data": [{"id": "t-2"}], "meta": {"totalPages": 2}},
    }

    def fetch(url, token):
        page = int(url.rsplit("page=", 1)[1])
        return pages[page]

    seen = list(langfuse.iter_http_pages(langfuse.validate_config(_config()), "traces", fetcher=fetch))
    assert [item["page"] for item in seen] == [1, 2]
    assert [r["id"] for item in seen for r in item["records"]] == ["t-1", "t-2"]


def test_cursor_pagination_stops_when_cursor_omitted():
    def fetch(url, token):
        if "cursor=" not in url:
            return {"data": [{"id": "t-1"}], "meta": {"cursor": "c-2"}}
        assert "cursor=c-2" in url
        return {"data": [{"id": "t-2"}], "meta": {}}

    seen = list(langfuse.iter_http_pages(langfuse.validate_config(_config()), "traces", fetcher=fetch))
    assert len(seen) == 2  # never falls back to page arithmetic mid-cursor-stream
    assert seen[0]["next_cursor"] == "c-2"
    assert seen[1]["has_more"] is False


def test_response_field_variants_and_partial_error():
    def fetch(url, token):
        # v1-ish shape: `traces` key, snake_case meta, partial error marker
        return {"traces": [{"id": "t-1"}], "meta": {"total_pages": 1}, "error": {"partial": True}}

    seen = list(langfuse.iter_http_pages(langfuse.validate_config(_config()), "traces", fetcher=fetch))
    assert seen[0]["records"] == [{"id": "t-1"}]
    assert any("partial error" in warning for warning in seen[0]["warnings"])


def test_missing_data_list_is_schema_error_non_retryable():
    def fetch(url, token):
        return {"unexpected": True}

    with pytest.raises(langfuse.LangfuseSourceError) as excinfo:
        list(langfuse.iter_http_pages(langfuse.validate_config(_config()), "traces", fetcher=fetch))
    assert excinfo.value.code == "source_schema_error"
    assert excinfo.value.retryable is False


def test_cancel_stops_iteration_between_pages():
    calls = []

    def fetch(url, token):
        calls.append(url)
        return {"data": [{"id": "t-1"}], "meta": {"totalPages": 5}}

    normalized = langfuse.validate_config(_config())
    seen = list(
        langfuse.iter_http_pages(normalized, "traces", fetcher=fetch, should_cancel=lambda: len(calls) >= 2)
    )
    assert len(calls) == 2  # third page never requested
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------


def test_http_status_classification():
    assert langfuse.classify_http_status(429) == ("source_rate_limited", True)
    assert langfuse.classify_http_status(408) == ("source_timeout", True)
    assert langfuse.classify_http_status(503) == ("source_unavailable", True)
    assert langfuse.classify_http_status(401) == ("source_unauthorized", False)
    assert langfuse.classify_http_status(403) == ("source_unauthorized", False)
    assert langfuse.classify_http_status(400) == ("source_schema_error", False)


def test_fetcher_error_is_redacted_and_retryable(monkeypatch):
    monkeypatch.setenv("AUDITRAIL_TEST_LF_TOKEN", "lf-secret-token")

    def fetch(url, token):
        raise RuntimeError("dial tcp: token=%s" % token)

    with pytest.raises(langfuse.LangfuseSourceError) as excinfo:
        list(langfuse.iter_http_pages(langfuse.validate_config(_config()), "traces", fetcher=fetch))
    assert excinfo.value.retryable is True
    assert "lf-secret-token" not in str(excinfo.value)
    assert "token=" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# normalization tolerance
# ---------------------------------------------------------------------------


def test_broken_generation_does_not_drop_trace():
    raw = {
        "id": "t-1",
        "observations": [
            {"id": "g-1", "name": "ok", "type": "GENERATION", "parentObservationId": "span-0"},
            "not-a-dict",
            {"name": "no id, still kept"},
        ],
    }
    trace = langfuse.normalize_trace(raw)
    assert trace["source_id"] == "t-1"
    assert len(trace["generations"]) == 2
    assert trace["generations"][0]["type"] == "GENERATION"
    assert trace["generations"][0]["parent_id"] == "span-0"
    assert trace["warnings"]  # the skipped generation is recorded, not silent


def test_metadata_session_id_variant_links():
    trace = langfuse.normalize_trace({"id": "t-9", "metadata": {"session_id": "sess-9"}})
    context = langfuse.new_run_context()
    context["sessions"]["sess-9"] = "run-9"
    result = langfuse.link_trace(trace, context)
    assert result["method"] == "session_id"
    assert result["run_ids"] == ["run-9"]


def test_report_carries_compat_version(tmp_path):
    path = tmp_path / "imports" / "traces.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"id": "t-1"}) + "\n", encoding="utf-8")
    report = langfuse.import_traces(tmp_path / "platform", {"kind": "file", "path": str(path)}, allowed_roots=[tmp_path])
    assert report["adapter_compat"] == langfuse.LANGFUSE_COMPAT_VERSION
