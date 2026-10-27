import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "compare_judge", Path(__file__).resolve().parents[1] / "evals" / "compare_judge.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


compare_judge = _load_module()


def _write_sample(samples_dir, sample_id, run_dir, dims, review_status="reviewed", overall="success"):
    sample = {
        "schema_version": 1,
        "sample_id": sample_id,
        "created_at": "2026-10-28T00:00:00Z",
        "run_ref": {"run_id": Path(run_dir).name, "run_dir": str(run_dir), "source_type": "dsh"},
        "task": {"input_summary": "t", "acceptance_criteria": "c", "criteria_note": "n"},
        "labels": {
            "overall_status": overall,
            "dimensions": [{"key": key, "status": status} for key, status in dims.items()],
        },
        "labeling": {
            "method": "human",
            "review_status": review_status,
            "labeled_by": "labeler",
            "reviewed_by": "reviewer" if review_status == "reviewed" else None,
            "reviewed_at": None,
        },
    }
    path = samples_dir / ("%s.json" % sample_id)
    path.write_text(json.dumps(sample, ensure_ascii=False), encoding="utf-8")
    return sample


def _write_run(runs_root, name, judge_dims, judge_overall="success"):
    run = runs_root / name
    run.mkdir(parents=True)
    report = {
        "overall_status": judge_overall,
        "dimensions": {key: {"status": status} for key, status in judge_dims.items()},
    }
    (run / "agent_judge.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return run


@pytest.fixture
def samples_dir(tmp_path):
    path = tmp_path / "evals" / "judge-samples"
    path.mkdir(parents=True)
    return path


def test_aggregate_reviewed_samples_counts(samples_dir, tmp_path):
    runs = tmp_path / "runs"
    run_a = _write_run(runs, "run-a", {"intent_recognition": "pass", "tool_selection": "pass"})
    run_b = _write_run(runs, "run-b", {"intent_recognition": "fail"}, judge_overall="failed")
    run_c = _write_run(
        runs,
        "run-c",
        {"intent_recognition": "pass", "task_planning": "pass", "safety_permission": "pass"},
    )
    _write_sample(samples_dir, "jrs-a", run_a, {"intent_recognition": "pass", "tool_selection": "fail"})
    _write_sample(samples_dir, "jrs-b", run_b, {"intent_recognition": "partial"}, overall="failed")
    _write_sample(
        samples_dir,
        "jrs-c",
        run_c,
        {"intent_recognition": "unknown", "task_planning": "partial", "parameter_generation": "pass"},
    )

    result = compare_judge.aggregate_samples(samples_dir)

    assert result["totals"] == {"samples": 3, "reviewed": 3, "provisional": 0, "skipped": 0}
    intent = result["dimensions"]["intent_recognition"]
    # a: agree(pass,pass); b: false_negative(fail,partial); c: disagree(pass,unknown)
    assert intent["counts"]["agree"] == 1
    assert intent["counts"]["false_negative"] == 1
    assert intent["counts"]["disagree"] == 1
    assert intent["total"] == 3
    assert intent["agreement"] == pytest.approx(1 / 3)
    tool = result["dimensions"]["tool_selection"]
    # a: false_positive(pass,fail); b/c: judge has no label -> judge_missing... b/c baseline
    # also has no label, and the existing classify() reports judge_missing for that pair.
    assert tool["counts"]["false_positive"] == 1
    assert tool["counts"]["judge_missing"] == 2
    planning = result["dimensions"]["task_planning"]
    assert planning["counts"]["false_positive"] == 1  # c: judge pass, baseline partial
    assert planning["counts"]["judge_missing"] == 2  # a/b: unlabeled on both sides
    param = result["dimensions"]["parameter_generation"]
    # c: baseline pass, judge unlabeled; a/b unlabeled on both sides
    assert param["counts"]["judge_missing"] == 3
    safety = result["dimensions"]["safety_permission"]
    assert safety["counts"]["baseline_missing"] == 1  # c: judge pass, baseline unlabeled
    assert safety["counts"]["judge_missing"] == 2  # a/b: unlabeled on both sides
    overall = result["overall_status"]
    # a: success==success; b: failed==failed; c: judge success vs baseline success
    assert overall["agree"] == 3
    assert overall["total"] == 3
    assert overall["agreement"] == 1.0


def test_aggregate_excludes_provisional_from_metrics(samples_dir, tmp_path):
    runs = tmp_path / "runs"
    run_a = _write_run(runs, "run-a", {"intent_recognition": "pass"})
    run_b = _write_run(runs, "run-b", {"intent_recognition": "fail"})
    _write_sample(samples_dir, "jrs-a", run_a, {"intent_recognition": "pass"})
    _write_sample(
        samples_dir, "jrs-b", run_b, {"intent_recognition": "pass"}, review_status="pending_human_review"
    )

    result = compare_judge.aggregate_samples(samples_dir)

    assert result["totals"]["reviewed"] == 1
    assert result["totals"]["provisional"] == 1
    assert result["provisional_samples"] == ["jrs-b"]
    intent = result["dimensions"]["intent_recognition"]
    assert intent["total"] == 1
    assert intent["counts"]["agree"] == 1
    assert intent["counts"].get("false_negative", 0) == 0


def test_aggregate_missing_run_or_report_skipped(samples_dir, tmp_path):
    runs = tmp_path / "runs"
    run_a = _write_run(runs, "run-a", {"intent_recognition": "pass"})
    _write_sample(samples_dir, "jrs-ok", run_a, {"intent_recognition": "pass"})
    _write_sample(samples_dir, "jrs-no-run", runs / "missing-run", {"intent_recognition": "pass"})
    no_report = runs / "run-no-report"
    no_report.mkdir(parents=True)
    _write_sample(samples_dir, "jrs-no-report", no_report, {"intent_recognition": "pass"})

    result = compare_judge.aggregate_samples(samples_dir)

    assert result["totals"]["reviewed"] == 3
    assert result["totals"]["skipped"] == 2
    skipped_ids = {row["sample_id"] for row in result["skipped_samples"]}
    assert skipped_ids == {"jrs-no-run", "jrs-no-report"}
    # 跳过的样本不进入聚合指标
    assert result["dimensions"]["intent_recognition"]["total"] == 1


def test_aggregate_empty_samples_dir(samples_dir):
    result = compare_judge.aggregate_samples(samples_dir)
    assert result["totals"] == {"samples": 0, "reviewed": 0, "provisional": 0, "skipped": 0}
    assert result["overall_status"]["total"] == 0


def test_main_samples_dir_json_and_fail_below(samples_dir, tmp_path, capsys):
    runs = tmp_path / "runs"
    run_a = _write_run(runs, "run-a", {"intent_recognition": "pass"})
    _write_sample(samples_dir, "jrs-a", run_a, {"intent_recognition": "fail"})

    code = compare_judge.main(["--samples-dir", str(samples_dir), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["totals"]["reviewed"] == 1

    # intent_recognition agreement = 0 -> 阈值 0.5 触发退出码 2
    code = compare_judge.main(["--samples-dir", str(samples_dir), "--fail-below", "0.5"])
    assert code == 2
    # 阈值 0.0 不触发
    code = compare_judge.main(["--samples-dir", str(samples_dir), "--fail-below", "0"])
    assert code == 0


def test_main_single_sample_mode_unchanged(samples_dir, tmp_path, capsys):
    runs = tmp_path / "runs"
    run_a = _write_run(runs, "run-a", {"intent_recognition": "pass"})
    sample = _write_sample(samples_dir, "jrs-a", run_a, {"intent_recognition": "pass"})
    sample_path = samples_dir / "jrs-a.json"

    code = compare_judge.main(["--sample", str(sample_path), "--run", str(run_a)])
    assert code == 0
    out = capsys.readouterr().out
    assert out.startswith("# Judge vs baseline comparison: jrs-a")
    assert "dimension agreement: 1/13" in out

    code = compare_judge.main(["--sample", str(sample_path), "--run", str(run_a), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sample_id"] == "jrs-a"
    assert payload["counts"]["agree"] == 1
