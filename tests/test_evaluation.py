"""EvaluationEnvelope v1: strict enums, blocking_source, nullable scores, invalid handling."""

import json

from auditrail.evaluation import (
    compute_final_quality,
    gating_envelope,
    load_evaluation,
    make_artifact_check,
    make_check,
    make_expectation_result,
    new_envelope,
    validate_envelope,
    write_evaluation,
)


def test_new_envelope_defaults_are_valid():
    envelope = new_envelope("pending")
    assert validate_envelope(envelope) == []
    assert envelope["schema_version"] == 1
    assert envelope["final_quality"] == {"status": "unknown", "score_cap": None, "source": "none"}


def test_validate_rejects_bad_enums_and_types():
    envelope = new_envelope("completed")
    envelope["status"] = "success"  # judge enum is not an envelope status
    errors = validate_envelope(envelope)
    assert any("status" in error for error in errors)


def test_validate_rejects_blocking_without_source():
    envelope = new_envelope("completed")
    check = make_check("c1", "tool_errors", "fail", blocking=True, blocking_source="none")
    envelope["deterministic_checks"] = [check]
    errors = validate_envelope(envelope)
    assert any("blocking_source" in error for error in errors)


def test_validate_rejects_non_boolean_blocking_and_bad_score_cap():
    envelope = new_envelope("completed")
    check = make_check("c1", "tool_errors", "fail")
    check["blocking"] = "yes"
    envelope["deterministic_checks"] = [check]
    envelope["final_quality"]["score_cap"] = 140
    errors = validate_envelope(envelope)
    assert any("blocking must be a boolean" in error for error in errors)
    assert any("score_cap" in error for error in errors)


def test_nullable_score_cap_is_accepted():
    envelope = new_envelope("completed")
    envelope["final_quality"] = {"status": "unknown", "score_cap": None, "source": "none"}
    assert validate_envelope(envelope) == []


def test_write_evaluation_forces_invalid_status_on_bad_payload(tmp_path):
    envelope = new_envelope("completed")
    envelope["deterministic_checks"] = [{"id": "x"}]  # missing kind/status/...
    written = write_evaluation(tmp_path, envelope)
    assert written["status"] == "invalid"
    assert written["error"]
    loaded = load_evaluation(tmp_path)
    assert loaded["status"] == "invalid"
    assert gating_envelope(tmp_path) is None


def test_load_evaluation_malformed_json_is_invalid_not_fatal(tmp_path):
    (tmp_path / "evaluation.json").write_text("{broken", encoding="utf-8")
    loaded = load_evaluation(tmp_path)
    assert loaded["status"] == "invalid"
    assert "not parseable" in loaded["error"]
    assert gating_envelope(tmp_path) is None


def test_load_evaluation_missing_file_is_none(tmp_path):
    assert load_evaluation(tmp_path) is None


def test_compute_final_quality_caps_combine_by_min():
    envelope = new_envelope("completed")
    envelope["deterministic_checks"] = [
        make_check("audit.tool_errors", "tool_errors", "fail"),  # blocking -> 60
    ]
    envelope["expectation_results"] = [
        make_expectation_result("e1", required=True, status="fail"),  # -> 60
    ]
    envelope["artifact_checks"] = [
        make_artifact_check("report.md", required=True, status="fail", exists=False, valid=False),  # -> 40
    ]
    final = compute_final_quality(envelope)
    assert final["status"] == "failed"
    assert final["score_cap"] == 40
    assert len(final["cap_sources"]) == 3


def test_compute_final_quality_unknown_never_fails():
    envelope = new_envelope("completed")
    envelope["expectation_results"] = [
        make_expectation_result("e1", required=True, status="unknown"),
    ]
    final = compute_final_quality(envelope)
    assert final["status"] == "unknown"
    assert final["score_cap"] is None


def test_compute_final_quality_all_pass_is_success_no_cap():
    envelope = new_envelope("completed")
    envelope["deterministic_checks"] = [make_check("audit.tool_errors", "tool_errors", "pass")]
    final = compute_final_quality(envelope)
    assert final["status"] == "success"
    assert final["score_cap"] is None


def test_envelope_roundtrip_through_disk(tmp_path):
    envelope = new_envelope("completed")
    envelope["deterministic_checks"] = [make_check("audit.final_answer", "final_answer", "pass")]
    envelope["final_quality"] = compute_final_quality(envelope)
    write_evaluation(tmp_path, envelope)
    loaded = load_evaluation(tmp_path)
    assert loaded["status"] == "completed"
    assert loaded["deterministic_checks"][0]["blocking"] is False
    assert loaded["deterministic_checks"][0]["blocking_source"] == "none"
    assert gating_envelope(tmp_path) is not None
    raw = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
