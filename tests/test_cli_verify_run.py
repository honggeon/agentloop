import json

import pytest

from auditrail.agents.tools import write_judge_report
from auditrail.cli import main as cli_main
from auditrail.verify import verify_run
from tests.conftest import full_judge_dimensions


def _write_run(tmp_path):
    run = tmp_path / "runs" / "20261028-verify"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(
        json.dumps({"session_id": "s1", "title": "核对订单数据"}, ensure_ascii=False),
        encoding="utf-8",
    )
    audit = {
        "message_count": 3,
        "tool_call_count": 2,
        "tool_result_count": 2,
        "findings": [{"severity": "error", "code": "tool_error", "message": "failed"}],
        "tool_calls": [
            {"index": 1, "tool": "terminal", "status": "error", "args_preview": "pip", "result_preview": "timeout"},
            {"index": 2, "tool": "read_file", "status": "ok", "args_preview": "a", "result_preview": "b"},
        ],
        "phases": [{"phase": "execute", "tool_count": 2, "tools": ["terminal", "read_file"]}],
    }
    (run / "audit.json").write_text(json.dumps(audit), encoding="utf-8")
    rows = [
        {"index": 0, "role": "user", "content": "核对订单数据"},
        {"index": 1, "role": "tool", "content": "ModuleNotFoundError"},
        {"index": 2, "role": "assistant", "content": "已用替代方案完成核对。"},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return run


def _judged_run(tmp_path):
    """A run with a real report written by write_judge_report (self-consistent)."""
    run = _write_run(tmp_path)
    write_judge_report(
        str(run),
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "工具失败后恢复。",
            "dimensions": full_judge_dimensions(),
            "findings": [
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "terminal failed",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 1, "quote": "ModuleNotFoundError"}],
                    "recommendation": "fix dependency",
                }
            ],
        },
    )
    return run


def _check_map(result):
    return {check["item"]: check for check in result["checks"]}


def test_verify_run_self_consistent_report_all_match(tmp_path):
    run = _judged_run(tmp_path)
    result = verify_run(str(run))
    assert result["ok"] is True
    checks = _check_map(result)
    # reliability, 4 deterministic rules and the quality cap are all compared
    assert checks["judge_reliability.evidence_coverage"]["status"] == "match"
    assert checks["judge_reliability.invalid_evidence_count"]["status"] == "match"
    for key in ("tool_selection", "execution_closure", "error_recovery", "evidence_citation"):
        assert checks["dimensions.%s.score_trace.final_score" % key]["status"] == "match"
    assert checks["metrics.quality_score_cap"]["status"] == "match"


def test_verify_run_flags_judge_dimensions_out_of_scope(tmp_path):
    run = _judged_run(tmp_path)
    result = verify_run(str(run))
    skipped = {item["item"]: item for item in result["skipped"]}
    # judge-sourced semantic dimensions are explicitly marked out of scope
    judge_sourced = [
        item for item in result["skipped"] if item.get("reason", "").find("judge") >= 0
    ]
    assert judge_sourced, "expected judge-sourced dimensions to be listed as out of scope"
    assert "dimensions.intent_recognition" in skipped


def test_verify_run_detects_tampered_dimension_score(tmp_path):
    run = _judged_run(tmp_path)
    report_path = run / "agent_judge.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    chain = report["dimensions"]["evidence_citation"]["evidence_chain"]
    original = chain["score_trace"]["final_score"]
    chain["score_trace"]["final_score"] = 100 - original
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    result = verify_run(str(run))
    assert result["ok"] is False
    check = _check_map(result)["dimensions.evidence_citation.score_trace.final_score"]
    assert check["status"] == "mismatch"
    assert check["expected"] == 100 - original  # stored (tampered) value
    assert check["actual"] == original  # replayed deterministic value


def test_verify_run_detects_tampered_reliability(tmp_path):
    run = _judged_run(tmp_path)
    report_path = run / "agent_judge.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["judge_reliability"]["invalid_evidence_count"] = 99
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    result = verify_run(str(run))
    assert result["ok"] is False
    check = _check_map(result)["judge_reliability.invalid_evidence_count"]
    assert check["status"] == "mismatch"
    assert check["actual"] == 0


def test_verify_run_missing_report_raises(tmp_path):
    run = _write_run(tmp_path)
    with pytest.raises(FileNotFoundError):
        verify_run(str(run))


def test_verify_run_legacy_report_skips_judge_dimensions(tmp_path):
    # 旧格式报告：无 evidence_chain / judge_reliability / metrics.quality_score_cap。
    # 确定性比对按可得字段执行，judge 维度标注跳过，不得报错。
    run = _write_run(tmp_path)
    legacy = {
        "overall_status": "success",
        "completion_score": 90,
        "summary": "旧格式报告。",
        "dimensions": {
            key: {"score": 80, "status": "pass", "summary": "旧报告", "source": "judge"}
            for key in full_judge_dimensions()
        },
    }
    (run / "agent_judge.json").write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")

    result = verify_run(str(run))
    assert result["ok"] is True
    assert result["checks"] == []
    skipped_items = {item["item"] for item in result["skipped"]}
    assert "judge_reliability.evidence_coverage" in skipped_items
    assert "metrics.quality_score_cap" in skipped_items
    assert "dimensions.tool_selection.score_trace.final_score" in skipped_items


def test_cli_verify_run_match_exit_zero(tmp_path, capsys):
    run = _judged_run(tmp_path)
    code = cli_main(["verify-run", "--run", str(run), "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True


def test_cli_verify_run_mismatch_exit_two(tmp_path, capsys):
    run = _judged_run(tmp_path)
    report_path = run / "agent_judge.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["dimensions"]["tool_selection"]["evidence_chain"]["score_trace"]["final_score"] = 99
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    code = cli_main(["verify-run", "--run", str(run)])
    assert code == 2
    out = capsys.readouterr().out
    assert "mismatch" in out


def test_cli_verify_run_missing_report_clean_error(tmp_path, capsys):
    run = _write_run(tmp_path)
    code = cli_main(["verify-run", "--run", str(run)])
    assert code == 2
    captured = capsys.readouterr()
    assert "agent_judge.json" in captured.err
