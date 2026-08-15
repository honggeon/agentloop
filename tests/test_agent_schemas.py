import json

from agentloop.agents.schemas import (
    extract_json_object,
    normalize_judge_report,
    render_judge_markdown,
    resolve_judge_report,
)


def test_normalize_judge_report_fills_defaults_and_counts_findings():
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 12,
            "confidence": "high",
            "summary": "未交付 pptx。",
            "findings": [
                {
                    "severity": "critical",
                    "category": "delivery",
                    "title": "没有生成目标文件",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 10, "quote": "ModuleNotFoundError"}],
                    "recommendation": "补齐依赖后重跑导出。",
                }
            ],
            "metrics": {"message_count": 3, "tool_call_count": 1, "tool_error_count": 1},
        }
    )

    assert report["overall_status"] == "failed"
    assert report["metrics"]["completion_score"] == 12
    assert report["charts"]["finding_severity"] == [{"severity": "critical", "count": 1}]


def test_normalize_judge_report_rejects_invalid_conclusion_type():
    try:
        normalize_judge_report(
            {
                "overall_status": "unknown",
                "completion_score": 0,
                "confidence": "low",
                "summary": "x",
                "findings": [{"severity": "low", "category": "tool", "title": "x", "type": "guess"}],
            }
        )
    except ValueError as exc:
        assert "type" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_normalize_judge_report_tolerates_non_list_evidence():
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 30,
            "confidence": "low",
            "summary": "x",
            "findings": [
                {"severity": "low", "category": "tool", "title": "a", "type": "fact", "evidence": "单条字符串证据"},
                {
                    "severity": "low",
                    "category": "tool",
                    "title": "b",
                    "type": "inference",
                    "evidence": {"source": "audit.json", "quote": "timeout"},
                },
            ],
        }
    )
    assert report["findings"][0]["evidence"][0]["quote"] == "单条字符串证据"
    assert report["findings"][1]["evidence"][0] == {"source": "audit.json", "quote": "timeout"}


def test_normalize_judge_report_expands_multi_message_indexes():
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "low",
            "summary": "x",
            "findings": [
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "多次工具失败",
                    "type": "fact",
                    "evidence": [
                        {
                            "source": "trajectory.jsonl",
                            "message_index": "6,9,12,23,27",
                            "quote": "tool error",
                        },
                        {
                            "source": "trajectory.jsonl",
                            "message_index": [1, "2", 3],
                            "quote": "retry",
                        },
                    ],
                    "recommendation": "收敛失败点。",
                }
            ],
        }
    )
    evidence = report["findings"][0]["evidence"]
    assert [row["message_index"] for row in evidence] == [6, 9, 12, 23, 27, 1, 2, 3]
    assert all(row["quote"] == "tool error" for row in evidence[:5])
    assert all(row["quote"] == "retry" for row in evidence[5:])


def test_normalize_judge_report_tolerates_annotated_message_indexes():
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "low",
            "summary": "x",
            "findings": [
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "搜索过多",
                    "type": "fact",
                    "evidence": [
                        {
                            "source": "trajectory.jsonl",
                            "message_index": "5-26 (web_search×16)",
                            "quote": "web_search",
                        },
                        {
                            "source": "trajectory.jsonl",
                            "message_index": "approx message twelve",
                            "quote": "no digits",
                        },
                    ],
                    "recommendation": "减少搜索。",
                }
            ],
        }
    )
    evidence = report["findings"][0]["evidence"]
    assert [row.get("message_index") for row in evidence] == [5, 26, None]
    assert evidence[2]["quote"] == "no digits"


def test_normalize_judge_report_maps_chinese_enum_values():
    report = normalize_judge_report(
        {
            "overall_status": "失败",
            "completion_score": 30,
            "confidence": "中",
            "summary": "x",
            "findings": [
                {"severity": "严重", "category": "工具", "title": "a", "type": "事实", "evidence": []},
                {"severity": "High", "category": "交付", "title": "b", "type": "推断", "evidence": []},
            ],
        }
    )
    assert report["overall_status"] == "failed"
    assert report["confidence"] == "medium"
    assert report["findings"][0]["severity"] == "critical"
    assert report["findings"][0]["category"] == "tool"
    assert report["findings"][0]["type"] == "fact"
    assert report["findings"][1]["severity"] == "high"


def test_normalize_judge_report_falls_back_on_unknown_severity():
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 10,
            "confidence": "low",
            "summary": "x",
            "findings": [{"severity": "warning", "category": "tool", "title": "a", "type": "fact", "evidence": []}],
        }
    )
    assert report["findings"][0]["severity"] == "low"


def test_extract_json_object_from_agent_message_content():
    payload = {"overall_status": "success", "completion_score": 90, "confidence": "medium", "summary": "ok"}
    message = {"messages": [{"role": "assistant", "content": "prefix\n```json\n%s\n```" % json.dumps(payload)}]}
    assert extract_json_object(message)["overall_status"] == "success"


def test_extract_json_object_skips_trailing_non_json_message():
    payload = {
        "overall_status": "failed",
        "completion_score": 40,
        "confidence": "medium",
        "summary": "未完成",
        "findings": [],
    }
    message = {
        "messages": [
            {"role": "assistant", "content": json.dumps(payload)},
            {"role": "assistant", "content": "报告已写入 agent_judge.json"},
        ]
    }
    assert extract_json_object(message)["overall_status"] == "failed"


def test_extract_json_object_from_content_blocks():
    payload = {"overall_status": "partial", "completion_score": 60, "confidence": "low", "summary": "ok"}
    message = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "分析完成。"},
                    {"type": "text", "text": json.dumps(payload)},
                ],
            }
        ]
    }
    assert extract_json_object(message)["completion_score"] == 60


def test_extract_json_object_from_nested_fenced_json():
    payload = {
        "overall_status": "failed",
        "completion_score": 20,
        "confidence": "high",
        "summary": "x",
        "findings": [
            {
                "severity": "high",
                "category": "tool",
                "title": "a",
                "type": "fact",
                "evidence": [{"source": "audit.json", "quote": "err"}],
                "recommendation": "fix",
            }
        ],
    }
    message = {"messages": [{"role": "assistant", "content": "```json\n%s\n```" % json.dumps(payload)}]}
    assert extract_json_object(message)["findings"][0]["title"] == "a"


def test_extract_json_object_from_write_judge_report_tool_call():
    payload = {
        "overall_status": "partial",
        "completion_score": 55,
        "confidence": "medium",
        "summary": "工具参数中的报告",
        "findings": [],
    }
    message = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "name": "write_judge_report",
                        "args": {"run_path": "/tmp/run", "report": payload},
                    }
                ],
            },
            {"role": "tool", "content": json.dumps({"json_path": "/tmp/run/agent_judge.json"})},
            {"role": "assistant", "content": "已完成评估。"},
        ]
    }
    assert extract_json_object(message)["summary"] == "工具参数中的报告"


def test_extract_json_object_from_tool_use_content_blocks():
    payload = {"overall_status": "success", "completion_score": 80, "confidence": "high", "summary": "ok"}
    message = {
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "write_judge_report",
                        "input": {"run_path": "/tmp/run", "report": payload},
                    }
                ],
            }
        ]
    }
    assert extract_json_object(message)["overall_status"] == "success"


def test_resolve_judge_report_falls_back_to_written_file(tmp_path):
    payload = {"overall_status": "failed", "completion_score": 10, "confidence": "low", "summary": "disk"}
    (tmp_path / "agent_judge.json").write_text(json.dumps(payload), encoding="utf-8")
    report = resolve_judge_report({"messages": [{"role": "assistant", "content": "done"}]}, tmp_path)
    assert report["summary"] == "disk"


def test_render_judge_markdown_includes_evidence():
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "completion_score": 55,
            "confidence": "medium",
            "summary": "部分完成。",
            "findings": [
                {
                    "severity": "high",
                    "category": "completion",
                    "title": "最终回答没有交付物",
                    "type": "fact",
                    "evidence": [{"source": "session.json", "message_index": 20, "quote": "请安装依赖"}],
                    "recommendation": "将交付文件作为完成条件。",
                }
            ],
        }
    )

    markdown = render_judge_markdown(report)
    assert "# Agentloop Agent Judge" in markdown
    assert "最终回答没有交付物" in markdown
    assert "session.json#20" in markdown


def test_normalize_judge_report_adds_default_dimensions_and_reliability():
    report = normalize_judge_report(
        {
            "overall_status": "success",
            "completion_score": 90,
            "confidence": "high",
            "summary": "ok",
            "findings": [],
        }
    )

    assert "intent_recognition" in report["dimensions"]
    assert "verification" in report["dimensions"]
    assert report["dimensions"]["intent_recognition"] == {
        "score": 0,
        "status": "unknown",
        "summary": "",
        "evidence": [],
        "recommendation": "",
        "source": "missing",
    }
    assert report["judge_reliability"]["evidence_coverage"] == 0
    assert report["judge_reliability"]["unsupported_claim_count"] == 0
    assert report["judge_reliability"]["invalid_evidence"] == []
    assert report["charts"]["dimension_scores"][0]["dimension"] == "intent_recognition"
    assert report["charts"]["dimension_scores"][0]["label"] == "意图识别"
    assert report["charts"]["dimension_scores"][0]["status"] == "unknown"


def test_normalize_judge_report_maps_dimension_aliases_and_bounds_scores():
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "completion_score": 60,
            "confidence": "medium",
            "summary": "x",
            "dimensions": {
                "意图识别": {"score": 120, "status": "通过", "summary": "识别正确", "source": "judge"},
                "工具选择": {"score": -3, "status": "失败", "summary": "工具错了", "source": "deterministic"},
            },
            "judge_reliability": {
                "evidence_coverage": 0.5,
                "unsupported_claim_count": 1,
                "invalid_evidence_count": 1,
                "fact_inference_mismatch_count": 2,
                "valid_evidence_count": 1,
                "total_evidence_count": 2,
                "notes": ["one invalid quote"],
                "invalid_evidence": [{"location": "findings.0", "reason": "quote_not_found", "quote": "nope"}],
            },
        }
    )

    assert report["dimensions"]["intent_recognition"]["score"] == 100
    assert report["dimensions"]["intent_recognition"]["status"] == "pass"
    assert report["dimensions"]["tool_selection"]["score"] == 0
    assert report["dimensions"]["tool_selection"]["status"] == "fail"
    assert report["judge_reliability"]["invalid_evidence"][0]["reason"] == "quote_not_found"
    assert report["judge_reliability"]["fact_inference_mismatch_count"] == 2
