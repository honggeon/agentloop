import json

from auditrail.agents.evidence import validate_judge_evidence
from auditrail.agents.schemas import normalize_judge_report


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
    assert reliability["fact_findings_without_valid_evidence"] == []


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
    assert reliability["fact_findings_without_valid_evidence"] == ["findings.0", "findings.1", "findings.2"]
    reasons = {row["reason"] for row in reliability["invalid_evidence"]}
    assert reasons == {"quote_not_found", "missing_quote"}


# -- extended sources and dimension evidence chains -----------------------


def _write_full_run(tmp_path):
    run = _write_evidence_run(tmp_path)
    (run / "expectations.json").write_text(
        json.dumps({"expectations": [{"id": "exp-1", "description": "必须生成报告文件"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "evaluation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "completed",
                "deterministic_checks": [],
                "expectation_results": [
                    {"id": "exp-1", "required": True, "status": "fail", "evidence_refs": [], "message": "未生成报告文件"}
                ],
                "artifact_checks": [],
                "external_evaluations": [],
                "runtime_metrics": None,
                "final_quality": {"status": "failed", "score_cap": 60, "source": "deterministic"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return run


def test_expectations_and_evaluation_are_controlled_sources(tmp_path):
    run = _write_full_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "summary": "x",
            "findings": [
                {
                    "severity": "high",
                    "category": "completion",
                    "title": "验收失败",
                    "type": "fact",
                    "evidence": [
                        {"source": "expectations", "quote": "必须生成报告文件"},
                        {"source": "evaluation.json", "quote": "未生成报告文件"},
                    ],
                    "recommendation": "修复",
                }
            ],
        }
    )

    reliability = validate_judge_evidence(run, report)

    assert reliability["valid_evidence_count"] == 2
    assert reliability["invalid_evidence_count"] == 0


def test_unknown_source_and_bad_message_index_and_missing_quote_fail(tmp_path):
    run = _write_full_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "summary": "x",
            "findings": [
                {
                    "severity": "low",
                    "category": "tool",
                    "title": "t",
                    "type": "fact",
                    "evidence": [
                        {"source": "../etc/passwd", "quote": "root"},
                        {"source": "evaluation.json", "quote": "synthesized sentence not in file"},
                    ],
                    "recommendation": "r",
                }
            ],
            "dimensions": {
                "verification": {
                    "score": 40,
                    "status": "fail",
                    "source": "judge",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 99, "quote": "timeout"}],
                }
            },
        }
    )

    reliability = validate_judge_evidence(run, report)
    reasons = {row["reason"] for row in reliability["invalid_evidence"]}
    assert "source_not_found" in reasons
    assert "quote_not_found" in reasons
    assert "message_index_not_found" in reasons
    # The non-unknown dimension has only invalid references: recorded as a
    # dimension-level evidence mismatch.
    assert reliability["dimensions_without_valid_evidence"] == ["verification"]
    assert reliability["unsupported_dimension_count"] == 0
    assert reliability["dimension_evidence_mismatch_count"] == 1


def test_dimension_chain_evidence_is_collected(tmp_path):
    run = _write_full_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "summary": "x",
            "dimensions": {
                "error_recovery": {
                    "score": 60,
                    "status": "partial",
                    "source": "deterministic+judge",
                    "evidence": [],
                    "evidence_chain": {
                        "checks": [
                            {
                                "id": "failure_detected",
                                "criterion": "c",
                                "result": "fail",
                                "conclusion": "c",
                                "type": "fact",
                                "evidence": [{"source": "trajectory.jsonl", "message_index": 1, "quote": "timeout"}],
                                "counter_evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "not-real"}],
                            }
                        ],
                        "score_trace": {
                            "judge_score": 85,
                            "deterministic_score": 60,
                            "final_score": 60,
                            "rules": [
                                {
                                    "id": "error_recovery_cap",
                                    "action": "cap",
                                    "before": 85,
                                    "candidate": 60,
                                    "after": 60,
                                    "reason": "r",
                                    "evidence": [{"source": "audit.json", "quote": "timeout"}],
                                }
                            ],
                        },
                    },
                }
            },
        }
    )

    reliability = validate_judge_evidence(run, report)
    locations = {row["location"] for row in reliability["invalid_evidence"]}
    # check evidence + rule evidence merged into top-level are valid; the
    # counter evidence quote does not exist and is reported.
    assert locations == {"dimensions.error_recovery.checks.0.counter"}
    assert reliability["invalid_evidence_count"] == 1
    # Counter evidence never counts as support, but check/rule/top-level
    # references do, so the dimension is supported.
    assert reliability["dimensions_without_valid_evidence"] == []


def test_scored_dimension_without_any_evidence_is_unsupported(tmp_path):
    run = _write_full_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "summary": "x",
            "dimensions": {
                "intent_recognition": {"score": 80, "status": "pass", "source": "judge", "evidence": []},
                "task_planning": {"score": None, "status": "unknown", "source": "judge", "evidence": []},
                "final_response": {"score": 30, "status": "fail", "source": "judge"},
            },
        }
    )

    reliability = validate_judge_evidence(run, report)

    assert reliability["unsupported_dimension_count"] == 2
    assert sorted(reliability["dimensions_without_valid_evidence"]) == ["final_response", "intent_recognition"]
    # unknown/null is an honest verdict, never flagged as unsupported.
    assert "task_planning" not in reliability["dimensions_without_valid_evidence"]
    assert any("scored dimensions" in note for note in reliability["notes"])


def test_synthetic_quote_not_in_artifact_is_invalid(tmp_path):
    # Regression contract for the old "artifact xxx missing" synthesis: a
    # quote that was never written to the artifact fails verbatim validation.
    run = _write_full_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "summary": "x",
            "findings": [
                {
                    "severity": "critical",
                    "category": "delivery",
                    "title": "Required artifact missing: report.pptx",
                    "type": "fact",
                    "evidence": [{"source": "evaluation.json", "quote": "artifact report.pptx missing"}],
                    "recommendation": "produce it",
                }
            ],
        }
    )

    reliability = validate_judge_evidence(run, report)
    assert reliability["invalid_evidence_count"] == 1
    assert reliability["invalid_evidence"][0]["reason"] == "quote_not_found"
