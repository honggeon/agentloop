import json

from agentloop.agents.evidence import validate_judge_evidence
from agentloop.agents.schemas import normalize_judge_report


def _write_evidence_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"title": "生成报告"}, ensure_ascii=False), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps({"tool_calls": [{"index": 1, "tool": "terminal", "status": "error", "result_preview": "timeout"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    rows = [
        {"index": 0, "role": "user", "content": "请生成报告"},
        {"index": 1, "role": "tool", "content": "timeout"},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return run


def test_validate_judge_evidence_counts_valid_quotes_and_aliases(tmp_path):
    run = _write_evidence_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "工具超时",
            "findings": [
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "工具超时",
                    "type": "fact",
                    "evidence": [{"source": "trajectory", "message_index": 1, "quote": "timeout"}],
                    "recommendation": "重试或换工具",
                }
            ],
        }
    )

    reliability = validate_judge_evidence(run, report)

    assert reliability["valid_evidence_count"] == 1
    assert reliability["total_evidence_count"] == 1
    assert reliability["evidence_coverage"] == 1
    assert reliability["invalid_evidence_count"] == 0
    assert reliability["unsupported_claim_count"] == 0
    assert reliability["fact_inference_mismatch_count"] == 0


def test_validate_judge_evidence_flags_missing_quote_empty_quote_and_unsupported_fact(tmp_path):
    run = _write_evidence_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "x",
            "findings": [
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "无证据事实",
                    "type": "fact",
                    "evidence": [],
                    "recommendation": "补证据",
                },
                {
                    "severity": "medium",
                    "category": "context",
                    "title": "引用不存在内容",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "not in row"}],
                    "recommendation": "修正引用",
                },
                {
                    "severity": "low",
                    "category": "tool",
                    "title": "空引用",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 1, "quote": ""}],
                    "recommendation": "补 quote",
                },
            ],
        }
    )

    reliability = validate_judge_evidence(run, report)

    assert reliability["unsupported_claim_count"] == 1
    assert reliability["invalid_evidence_count"] == 2
    assert reliability["fact_inference_mismatch_count"] == 3
    assert reliability["evidence_coverage"] == 0
    reasons = {row["reason"] for row in reliability["invalid_evidence"]}
    assert reasons == {"quote_not_found", "missing_quote"}
