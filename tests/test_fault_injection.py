import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_fault_injection", Path(__file__).resolve().parents[1] / "evals" / "run_fault_injection.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fi = _load_module()


def _write_fi_sample(fi_dir, sample_id, run_dir, faults, review_status="reviewed"):
    sample = {
        "schema_version": 1,
        "sample_id": sample_id,
        "created_at": "2026-10-28T00:00:00Z",
        "run_ref": {"run_dir": str(run_dir), "source_type": "dsh"},
        "injection": {
            "method": "authored",
            "description": "test injection",
            "faults": faults,
        },
        "labeling": {
            "review_status": review_status,
            "labeled_by": "labeler",
            "reviewed_by": "reviewer" if review_status == "reviewed" else None,
        },
    }
    path = fi_dir / ("%s.json" % sample_id)
    path.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    return sample


def _write_run(runs_root, name, dims, completion_score=100):
    run = runs_root / name
    run.mkdir(parents=True)
    report = {
        "overall_status": "success",
        "completion_score": completion_score,
        "dimensions": {key: {"status": status} for key, status in dims.items()},
    }
    (run / "agent_judge.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return run


@pytest.fixture
def fi_dir(tmp_path):
    path = tmp_path / "evals" / "fault-injection"
    path.mkdir(parents=True)
    return path


def test_status_band_semantics(fi_dir, tmp_path):
    runs = tmp_path / "runs"
    # expect at most partial: fail/partial caught, pass missed
    run_fail = _write_run(runs, "run-fail", {"error_recovery": "fail"})
    run_partial = _write_run(runs, "run-partial", {"error_recovery": "partial"})
    run_pass = _write_run(runs, "run-pass", {"error_recovery": "pass"})
    fault = {"id": "fake_recovery", "expect_dimension": "error_recovery", "expect_status_at_most": "partial"}
    _write_fi_sample(fi_dir, "fi-fail", run_fail, [fault])
    _write_fi_sample(fi_dir, "fi-partial", run_partial, [fault])
    _write_fi_sample(fi_dir, "fi-pass", run_pass, [fault])

    result = fi.run_fault_injection(fi_dir)

    assert result["totals"]["faults"] == 3
    assert result["totals"]["caught"] == 2
    assert result["totals"]["missed"] == 1
    assert result["recall"] == pytest.approx(2 / 3)
    missed = result["missed_faults"]
    assert len(missed) == 1
    assert missed[0]["sample_id"] == "fi-pass"
    assert missed[0]["fault_id"] == "fake_recovery"
    assert missed[0]["dimension"] == "error_recovery"
    assert missed[0]["expect_status_at_most"] == "partial"
    assert missed[0]["actual_status"] == "pass"


def test_completion_score_ceiling_and_null_unverifiable(fi_dir, tmp_path):
    runs = tmp_path / "runs"
    run_ok = _write_run(runs, "run-ok", {}, completion_score=55)
    run_high = _write_run(runs, "run-high", {}, completion_score=90)
    run_null = _write_run(runs, "run-null", {}, completion_score=None)
    fault = {
        "id": "silent_error",
        "expect_dimension": "execution_closure",
        "expect_completion_score_at_most": 60,
    }
    _write_fi_sample(fi_dir, "fi-ok", run_ok, [fault])
    _write_fi_sample(fi_dir, "fi-high", run_high, [fault])
    _write_fi_sample(fi_dir, "fi-null", run_null, [fault])

    result = fi.run_fault_injection(fi_dir)

    assert result["totals"]["caught"] == 1
    assert result["totals"]["missed"] == 1
    assert result["totals"]["unverifiable"] == 1
    # null completion_score 记 unverifiable，不算 caught 也不算 missed
    assert result["recall"] == pytest.approx(1 / 3)
    assert result["unverifiable_faults"][0]["sample_id"] == "fi-null"


def test_multiple_faults_partial_hit(fi_dir, tmp_path):
    runs = tmp_path / "runs"
    run = _write_run(runs, "run-a", {"error_recovery": "fail", "evidence_citation": "pass"}, completion_score=80)
    faults = [
        {"id": "fake_recovery", "expect_dimension": "error_recovery", "expect_status_at_most": "partial"},
        {"id": "fabricated_citation", "expect_dimension": "evidence_citation", "expect_status_at_most": "partial"},
        {"id": "score_ceiling", "expect_dimension": "execution_closure", "expect_completion_score_at_most": 90},
    ]
    _write_fi_sample(fi_dir, "fi-a", run, faults)

    result = fi.run_fault_injection(fi_dir)

    assert result["totals"]["faults"] == 3
    assert result["totals"]["caught"] == 2
    assert result["totals"]["missed"] == 1
    assert [row["fault_id"] for row in result["missed_faults"]] == ["fabricated_citation"]


def test_fault_requires_at_least_one_assertion(fi_dir, tmp_path):
    runs = tmp_path / "runs"
    run = _write_run(runs, "run-a", {"error_recovery": "fail"})
    _write_fi_sample(fi_dir, "fi-bad", run, [{"id": "empty", "expect_dimension": "error_recovery"}])

    result = fi.run_fault_injection(fi_dir)
    assert result["totals"]["skipped"] == 1
    assert result["skipped_samples"][0]["sample_id"] == "fi-bad"


def test_missing_run_or_report_skipped(fi_dir, tmp_path):
    runs = tmp_path / "runs"
    run_ok = _write_run(runs, "run-ok", {"error_recovery": "fail"})
    fault = {"id": "f1", "expect_dimension": "error_recovery", "expect_status_at_most": "partial"}
    _write_fi_sample(fi_dir, "fi-ok", run_ok, [fault])
    _write_fi_sample(fi_dir, "fi-no-run", runs / "missing", [fault])
    no_report = runs / "run-no-report"
    no_report.mkdir(parents=True)
    _write_fi_sample(fi_dir, "fi-no-report", no_report, [fault])

    result = fi.run_fault_injection(fi_dir)

    assert result["totals"]["faults"] == 1
    assert result["totals"]["caught"] == 1
    assert result["totals"]["skipped"] == 2
    assert {row["sample_id"] for row in result["skipped_samples"]} == {"fi-no-run", "fi-no-report"}


def test_non_reviewed_sample_skipped(fi_dir, tmp_path):
    runs = tmp_path / "runs"
    run = _write_run(runs, "run-a", {"error_recovery": "pass"})
    fault = {"id": "f1", "expect_dimension": "error_recovery", "expect_status_at_most": "partial"}
    _write_fi_sample(fi_dir, "fi-pending", run, [fault], review_status="pending_human_review")

    result = fi.run_fault_injection(fi_dir)

    assert result["totals"]["faults"] == 0
    assert result["totals"]["reviewed"] == 0
    assert result["non_reviewed_samples"] == ["fi-pending"]


def test_empty_dir_zero_zero_exit_zero(fi_dir, capsys):
    code = fi.main(["--dir", str(fi_dir)])
    assert code == 0
    out = capsys.readouterr().out
    assert "0/0" in out


def test_main_json_output(fi_dir, tmp_path, capsys):
    runs = tmp_path / "runs"
    run = _write_run(runs, "run-a", {"error_recovery": "fail"})
    fault = {"id": "f1", "expect_dimension": "error_recovery", "expect_status_at_most": "partial"}
    _write_fi_sample(fi_dir, "fi-a", run, [fault])

    code = fi.main(["--dir", str(fi_dir), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["caught"] == 1
    assert payload["recall"] == 1.0
