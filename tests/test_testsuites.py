"""Phase 1 core: TestSuite normalization and the SuiteJob/CaseAttempt contract."""

import pytest

from auditrail import testsuites as suite_mod


def _suite_payload(**overrides):
    payload = {
        "suite_id": "suite-001",
        "version": 1,
        "title": "工具恢复回归集",
        "description": "回归",
        "case_ids": ["case-001", "case-002"],
        "enabled": True,
    }
    payload.update(overrides)
    return payload


def _agent():
    return {"agent_id": "agent-1", "version": "git:abc", "config_hash": "deadbeef"}


def _snapshots():
    return [
        {"case_id": "case-001", "title": "用例一", "version": 1},
        {"case_id": "case-002", "title": "用例二", "version": 2},
    ]


# ------------------------------------------------------------- suite config


def test_normalize_test_suite_happy_path():
    suite = suite_mod.normalize_test_suite(_suite_payload())
    assert suite["suite_id"] == "suite-001"
    assert suite["version"] == 1
    assert suite["case_ids"] == ["case-001", "case-002"]
    assert suite["enabled"] is True
    assert suite["created_at"] and suite["updated_at"]


def test_normalize_test_suite_rejects_bad_payloads():
    for payload in (
        {},
        {"suite_id": "x"},
        _suite_payload(suite_id="../escape"),
        _suite_payload(suite_id="a/b"),
        _suite_payload(case_ids="case-001"),
        _suite_payload(case_ids=["ok", ""]),
        _suite_payload(enabled="yes"),
        _suite_payload(version=0),
    ):
        with pytest.raises(ValueError):
            suite_mod.normalize_test_suite(payload)


def test_normalize_test_suite_update_bumps_version_and_keeps_created_at():
    existing = suite_mod.normalize_test_suite(_suite_payload())
    updated = suite_mod.normalize_test_suite(
        _suite_payload(title="新标题", version=None), existing=existing
    )
    assert updated["version"] == 2
    assert updated["title"] == "新标题"
    assert updated["created_at"] == existing["created_at"]


# --------------------------------------------------------------- suite jobs


def test_new_suite_job_creates_queued_attempts_in_order():
    suite = suite_mod.normalize_test_suite(_suite_payload())
    record = suite_mod.new_suite_job(
        "suitejob-1", suite=suite, agent=_agent(), judge=True, case_snapshots=_snapshots()
    )
    assert record["status"] == "queued"
    assert record["judge"] is True
    assert record["suite_ref"]["id"] == "suite-001"
    assert record["agent_ref"]["id"] == "agent-1"
    assert [a["case_id"] for a in record["attempts"]] == ["case-001", "case-002"]
    assert [a["order"] for a in record["attempts"]] == [0, 1]
    assert all(a["status"] == "queued" and a["run_id"] is None for a in record["attempts"])
    assert len({a["attempt_id"] for a in record["attempts"]}) == 2


def test_job_status_enums_are_distinct_and_validated():
    assert set(suite_mod.SUITE_JOB_STATUSES) == {"queued", "running", "completed", "failed", "cancelled"}
    with pytest.raises(ValueError):
        suite_mod.validate_job_status("skipped")  # skipped is attempt-only
    with pytest.raises(ValueError):
        suite_mod.validate_attempt_status("nope")
    assert suite_mod.validate_attempt_status("skipped") == "skipped"


def test_advance_attempt_and_stats(tmp_path):
    suite = suite_mod.normalize_test_suite(_suite_payload())
    record = suite_mod.new_suite_job(
        "suitejob-1", suite=suite, agent=_agent(), judge=False, case_snapshots=_snapshots()
    )
    suite_mod.mark_job_running(record)
    first, second = record["attempts"]
    suite_mod.advance_attempt(record, first["attempt_id"], "running", run_id="run-1")
    suite_mod.advance_attempt(record, first["attempt_id"], "failed", status_reason="agent_unreachable: x")
    suite_mod.advance_attempt(record, second["attempt_id"], "completed", run_id="run-2")
    suite_mod.finalize_job(record, "completed")

    path = tmp_path / "job.json"
    suite_mod.write_suite_job(path, record)
    loaded = suite_mod.read_suite_job(path)
    assert loaded["status"] == "completed"
    assert loaded["finished_at"]
    assert loaded["attempts"][0]["status"] == "failed"
    assert loaded["attempts"][0]["run_id"] == "run-1"
    assert loaded["attempts"][0]["status_reason"] == "agent_unreachable: x"
    assert loaded["attempts"][1]["status"] == "completed"
    stats = loaded["stats"]
    assert stats["total"] == 2
    assert stats["completed"] == 1
    assert stats["failed"] == 1
    assert stats["skipped"] == 0


def test_skip_pending_attempts_only_marks_queued():
    suite = suite_mod.normalize_test_suite(_suite_payload())
    record = suite_mod.new_suite_job(
        "suitejob-1", suite=suite, agent=_agent(), judge=False, case_snapshots=_snapshots()
    )
    first = record["attempts"][0]
    suite_mod.advance_attempt(record, first["attempt_id"], "completed", run_id="run-1")
    skipped = suite_mod.skip_pending_attempts(record, reason="suite job cancelled")
    assert skipped == 1
    assert record["attempts"][0]["status"] == "completed"  # finished evidence preserved
    assert record["attempts"][1]["status"] == "skipped"
    assert record["attempts"][1]["run_id"] is None
    stats = suite_mod.compute_job_stats(record)
    assert stats["completed"] == 1 and stats["skipped"] == 1


def test_read_suite_job_rejects_malformed(tmp_path):
    assert suite_mod.read_suite_job(tmp_path / "missing.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert suite_mod.read_suite_job(bad) is None
    wrong = tmp_path / "wrong.json"
    wrong.write_text('{"status": "bogus", "attempts": []}', encoding="utf-8")
    assert suite_mod.read_suite_job(wrong) is None


# ----------------------------------------------------- Phase 1.1 job options


def test_normalize_job_options_defaults_and_valid_values():
    options = suite_mod.normalize_job_options(None)
    assert options == {
        "max_retries": 0,
        "timeout_seconds": None,
        "budget": {"max_attempts": None, "max_total_tokens": None},
    }
    options = suite_mod.normalize_job_options(
        {
            "agent_id": "ignored",  # unrelated payload keys are ignored
            "max_retries": 2,
            "timeout_seconds": 300,
            "budget": {"max_attempts": 10, "max_total_tokens": 5000},
        }
    )
    assert options["max_retries"] == 2
    assert options["timeout_seconds"] == 300.0
    assert options["budget"] == {"max_attempts": 10, "max_total_tokens": 5000}


def test_normalize_job_options_rejects_invalid_values():
    for payload in (
        "nope",
        {"max_retries": -1},
        {"max_retries": suite_mod.MAX_JOB_RETRIES + 1},
        {"max_retries": True},
        {"max_retries": "many"},
        {"timeout_seconds": 0},
        {"timeout_seconds": -5},
        {"timeout_seconds": suite_mod.MAX_JOB_TIMEOUT_SECONDS + 1},
        {"budget": "nope"},
        {"budget": {"max_attempts": 0}},
        {"budget": {"max_attempts": -3}},
        {"budget": {"max_total_tokens": "lots"}},
        {"budget": {"max_total_tokens": False}},
    ):
        with pytest.raises(ValueError):
            suite_mod.normalize_job_options(payload)


def test_new_suite_job_carries_options_limits_and_recovered_flag():
    suite = suite_mod.normalize_test_suite(_suite_payload())
    record = suite_mod.new_suite_job(
        "suitejob-1",
        suite=suite,
        agent=_agent(),
        judge=False,
        case_snapshots=_snapshots(),
        options={"max_retries": 2, "timeout_seconds": 600, "budget": {"max_attempts": 5, "max_total_tokens": None}},
        limits={"global": 4, "agent": 1},
    )
    assert record["max_retries"] == 2
    assert record["timeout_seconds"] == 600.0
    assert record["budget"] == {"max_attempts": 5, "max_total_tokens": None}
    assert record["budget_used"] == {"attempts": 0, "total_tokens": 0}
    assert record["limits"] == {"global": 4, "agent": 1}
    assert record["recovered"] is False
    assert all(a["retry_index"] == 0 for a in record["attempts"])


# ----------------------------------------------------- Phase 1.1 retry model


def test_append_retry_attempt_creates_fresh_queued_attempt():
    suite = suite_mod.normalize_test_suite(_suite_payload())
    record = suite_mod.new_suite_job(
        "suitejob-1", suite=suite, agent=_agent(), judge=False, case_snapshots=_snapshots()
    )
    first = record["attempts"][0]
    suite_mod.advance_attempt(record, first["attempt_id"], "failed", run_id="run-1", status_reason="agent_timeout: x")
    retry = suite_mod.append_retry_attempt(record, first)
    assert retry["order"] == first["order"]
    assert retry["case_id"] == first["case_id"]
    assert retry["retry_index"] == 1
    assert retry["attempt_id"] != first["attempt_id"]
    assert retry["status"] == "queued" and retry["run_id"] is None
    # the failed attempt and its Run stay untouched as evidence
    assert record["attempts"][0]["status"] == "failed"
    assert record["attempts"][0]["run_id"] == "run-1"
    retry2 = suite_mod.append_retry_attempt(record, retry)
    assert retry2["retry_index"] == 2


def test_compute_job_stats_counts_cases_by_final_attempt_with_retries():
    suite = suite_mod.normalize_test_suite(_suite_payload())
    record = suite_mod.new_suite_job(
        "suitejob-1", suite=suite, agent=_agent(), judge=False, case_snapshots=_snapshots()
    )
    first, second = record["attempts"]
    # case 1: failed once, then the retry completed -> case counts completed
    suite_mod.advance_attempt(record, first["attempt_id"], "failed", run_id="run-1")
    retry = suite_mod.append_retry_attempt(record, first)
    suite_mod.advance_attempt(record, retry["attempt_id"], "completed", run_id="run-2")
    # case 2: still queued
    stats = suite_mod.compute_job_stats(record)
    assert stats["total"] == 2
    assert stats["completed"] == 1
    assert stats["failed"] == 0  # superseded attempt does not fail the case
    assert stats["queued"] == 1
    assert stats["runs"] == 2
    assert stats["retries"] == 1
    # old flat jobs without retry_index still count one attempt per case
    legacy = suite_mod.new_suite_job(
        "suitejob-2", suite=suite, agent=_agent(), judge=False, case_snapshots=_snapshots()
    )
    for attempt in legacy["attempts"]:
        del attempt["retry_index"]
        suite_mod.advance_attempt(legacy, attempt["attempt_id"], "skipped", status_reason="x")
    legacy_stats = suite_mod.compute_job_stats(legacy)
    assert legacy_stats["total"] == 2 and legacy_stats["skipped"] == 2 and legacy_stats["retries"] == 0


def test_is_retryable_failure():
    assert suite_mod.is_retryable_failure("timed_out", None, None) is True
    assert suite_mod.is_retryable_failure("failed", "agent_unreachable: x", None) is True
    assert suite_mod.is_retryable_failure("failed", "executor_interrupted", None) is True
    assert suite_mod.is_retryable_failure("failed", "endpoint_rejected: x", None) is False
    assert suite_mod.is_retryable_failure("failed", "test_case_not_found", None) is False
    assert suite_mod.is_retryable_failure("cancelled", None, None) is False
    assert suite_mod.is_retryable_failure("skipped", None, None) is False
    assert suite_mod.is_retryable_failure("completed", None, None) is False
    # error.json retryable flag wins over the reason prefix
    assert suite_mod.is_retryable_failure("failed", "agent_http_error: HTTP 500", {"retryable": True}) is True
    assert suite_mod.is_retryable_failure("failed", "agent_unreachable: x", {"retryable": False}) is False
