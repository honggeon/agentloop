import json

from auditrail.agents.dimensions import assess_dimensions
from auditrail.agents.evidence import validate_judge_evidence
from auditrail.agents.schemas import normalize_judge_report


def _write_tool_run(tmp_path, status="ok"):
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"message_count": 3, "tool_call_count": 1}), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps(
            {
                "message_count": 3,
                "tool_call_count": 1,
                "tool_result_count": 1,
                "tool_calls": [{"index": 1, "tool": "search", "status": status, "args_preview": '{"q":"agent"}'}],
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {"index": 0, "role": "user", "content": "查一下 agent 评测"},
        {"index": 1, "role": "assistant", "content": "调用搜索工具"},
        {"index": 2, "role": "assistant", "content": "最终回答"},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return run


def test_assess_dimensions_keeps_semantic_unknown_and_does_not_auto_pass_tool_selection(tmp_path):
    run = _write_tool_run(tmp_path)
    report = normalize_judge_report({"overall_status": "success", "completion_score": 90, "confidence": "high", "summary": "ok"})
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    assert dimensions["tool_selection"]["status"] == "unknown"
    assert dimensions["state_tracking"]["status"] == "unknown"
    assert dimensions["safety_permission"]["status"] == "unknown"
    assert dimensions["efficiency"]["status"] == "unknown"
    assert dimensions["execution_closure"]["status"] == "pass"
    assert dimensions["error_recovery"]["status"] == "unknown"
    assert dimensions["evidence_citation"]["status"] == "fail"


def test_assess_dimensions_caps_tool_selection_and_invalid_evidence(tmp_path):
    run = _write_tool_run(tmp_path, status="error")
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 40,
            "confidence": "medium",
            "summary": "x",
            "dimensions": {
                "tool_selection": {"score": 90, "status": "pass", "source": "judge"},
                "evidence_citation": {
                    "score": 90,
                    "status": "pass",
                    "source": "judge",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "missing quote"}],
                },
            },
        }
    )
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    assert dimensions["tool_selection"]["status"] == "fail"
    assert dimensions["tool_selection"]["score"] < 90
    assert dimensions["error_recovery"]["status"] in {"partial", "fail"}
    assert dimensions["evidence_citation"]["status"] == "fail"
    assert dimensions["evidence_citation"]["score"] == 0


def _judge_unknown_dimensions():
    return {
        key: {"score": 0, "status": "unknown", "source": "judge"}
        for key in ("execution_closure", "evidence_citation", "tool_selection", "error_recovery")
    }


def test_assess_dimensions_writes_deterministic_when_judge_left_unknown_success(tmp_path):
    run = _write_tool_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "success",
            "completion_score": 90,
            "confidence": "high",
            "summary": "ok",
            "dimensions": _judge_unknown_dimensions(),
        }
    )
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    assert dimensions["execution_closure"]["status"] == "pass"
    assert dimensions["execution_closure"]["source"] == "deterministic+judge"
    assert dimensions["tool_selection"]["status"] == "unknown"
    assert dimensions["tool_selection"]["source"] == "judge"
    assert dimensions["error_recovery"]["status"] == "unknown"
    assert dimensions["error_recovery"]["source"] == "judge"
    assert dimensions["evidence_citation"]["status"] == "fail"
    assert dimensions["evidence_citation"]["score"] == 0
    assert dimensions["evidence_citation"]["source"] == "deterministic+judge"


def test_assess_dimensions_writes_deterministic_when_judge_left_unknown_failure(tmp_path):
    run = _write_tool_run(tmp_path, status="error")
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 40,
            "confidence": "medium",
            "summary": "x",
            "dimensions": {
                **_judge_unknown_dimensions(),
                "evidence_citation": {
                    "score": 0,
                    "status": "unknown",
                    "source": "judge",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "missing quote"}],
                },
            },
        }
    )
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    assert dimensions["tool_selection"]["status"] in {"fail", "partial"}
    assert dimensions["tool_selection"]["score"] < 100
    assert dimensions["tool_selection"]["source"] == "deterministic+judge"
    assert dimensions["error_recovery"]["status"] in {"partial", "fail"}
    assert dimensions["error_recovery"]["source"] == "deterministic+judge"
    assert dimensions["evidence_citation"]["status"] == "fail"
    assert dimensions["evidence_citation"]["score"] == 0
    assert dimensions["evidence_citation"]["source"] == "deterministic+judge"
