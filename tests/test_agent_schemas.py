import json

from auditrail.agents.schemas import (
    DIMENSION_DEFINITIONS,
    extract_json_object,
    missing_judge_dimensions,
    normalize_judge_report,
    render_judge_markdown,
    resolve_judge_report,
)
from tests.conftest import full_judge_dimensions


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


def test_extract_json_object_falls_back_to_report_shaped_body_without_status():
    # deepseek-flash sometimes drops overall_status but still emits the report
    # body; extraction must degrade to overall_status "unknown" rather than
    # fail the whole judge run.
    body = {"run_path": "runs/x", "meta": {"title": "t"}, "findings": [], "dimensions": {"efficiency": {"score": 50}}}
    message = {"messages": [{"role": "assistant", "content": "```json\n%s\n```" % json.dumps(body)}]}
    report = normalize_judge_report(extract_json_object(message))
    assert report["overall_status"] == "unknown"
    assert "efficiency" in report["dimensions"]


def test_extract_json_object_from_unknown_envelope_key():
    # Models sometimes wrap the report in an envelope whose nesting key is not
    # one of the well-known ones (observed: deepseek emits {"schema_version":
    # "auditrail-judge/1.0", "judge_report": {...}}).
    payload = {"overall_status": "partial", "completion_score": 42, "confidence": "low", "summary": "ok"}
    envelope = {
        "schema_version": "auditrail-judge/1.0",
        "run_path": "runs/x",
        "judge_report": payload,
    }
    message = {"messages": [{"role": "assistant", "content": json.dumps(envelope)}]}
    assert extract_json_object(message)["completion_score"] == 42

    # also works when the envelope arrives as an already-parsed dict
    assert extract_json_object(envelope)["overall_status"] == "partial"


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
    assert "# Auditrail Agent Judge" in markdown
    assert "最终回答没有交付物" in markdown
    assert "session.json#20" in markdown


def test_render_judge_markdown_includes_dimensions_and_reliability():
    markdown = render_judge_markdown(
        {
            "overall_status": "partial",
            "completion_score": 60,
            "confidence": "medium",
            "summary": "部分完成",
            "dimensions": {
                "intent_recognition": {
                    "score": 80,
                    "status": "pass",
                    "summary": "理解目标",
                    "recommendation": "保持",
                    "source": "judge",
                }
            },
            "judge_reliability": {
                "evidence_coverage": 0.75,
                "unsupported_claim_count": 1,
                "invalid_evidence_count": 0,
                "fact_inference_mismatch_count": 1,
            },
            "findings": [],
        }
    )
    assert "### Evaluation Dimensions" in markdown
    assert "意图识别" in markdown
    assert "### Judge Reliability" in markdown
    assert "0.75" in markdown


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
    # Phase 0 contract: a dimension with no judge evidence and no deterministic
    # result is unknown with a null score — never a fabricated 0.
    assert report["dimensions"]["intent_recognition"] == {
        "score": None,
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


def test_normalize_judge_report_discards_malformed_metrics_and_charts():
    # Regression: judge models sometimes emit metrics/charts as arrays or
    # strings; dict() on those crashed the whole judge run with an opaque
    # ValueError ("dictionary update sequence element ...") after the model
    # quota was already spent. Malformed shapes must degrade to empty objects.
    report = normalize_judge_report(
        {
            "overall_status": "success",
            "metrics": ["abc"],
            "charts": "token",
        }
    )

    assert report["metrics"]["completion_score"] is None
    assert report["metrics"]["message_count"] == 0
    assert report["charts"]["finding_severity"] == []
    assert report["charts"]["tool_distribution"] == []


def test_normalize_judge_report_keeps_mapping_metrics_and_charts():
    report = normalize_judge_report(
        {
            "overall_status": "success",
            "metrics": {"custom_kpi": 3},
            "charts": {"tool_distribution": [{"tool": "bash", "count": 1}]},
        }
    )

    assert report["metrics"]["custom_kpi"] == 3
    assert report["charts"]["tool_distribution"] == [{"tool": "bash", "count": 1}]


def test_normalize_judge_report_flags_structurally_incomplete_judge():
    # Dimensions the proposal never provided (source=missing) mean the judge
    # output was structurally incomplete — flag it instead of treating the
    # skeleton as a normal report.
    report = normalize_judge_report({"overall_status": "success"})

    assert report["judge_incomplete"] is True
    assert all(row["source"] == "missing" for row in report["dimensions"].values())
    assert any("judge_incomplete" in note for note in report["judge_reliability"]["notes"])


def test_normalize_judge_report_not_incomplete_when_judge_assessed_unknown():
    # A judge that assessed every dimension and honestly reported "insufficient
    # evidence" (status=unknown, source=judge) is complete — unknown is a
    # legitimate verdict, only source=missing indicates a structural gap.
    dimensions = {
        "intent_recognition": {"score": None, "status": "unknown", "summary": "证据不足", "source": "judge"},
    }
    report = normalize_judge_report({"overall_status": "unknown", "dimensions": dimensions})

    assert report["dimensions"]["intent_recognition"]["status"] == "unknown"
    assert report["dimensions"]["intent_recognition"]["source"] == "judge"
    assert report["judge_incomplete"] is True  # other 12 dimensions still missing


def test_normalize_judge_report_complete_when_all_dimensions_provided():
    dimensions = {
        key: {"score": 80, "status": "pass", "summary": "s", "source": "judge"}
        for key in (
            "intent_recognition", "task_planning", "tool_selection", "parameter_generation",
            "evidence_citation", "state_tracking", "final_response", "context_management",
            "execution_closure", "error_recovery", "verification", "safety_permission", "efficiency",
        )
    }
    report = normalize_judge_report({"overall_status": "success", "completion_score": 90, "dimensions": dimensions})

    assert report["judge_incomplete"] is False
    assert report["completion_score"] == 90


def test_extract_json_object_prefers_complete_report_over_later_incomplete():
    # Regression: the judge submitted a complete 13-dimension report through the
    # write_judge_report tool call, but the final assistant message carried a
    # terse summary JSON without dimensions. Extraction must prefer the most
    # complete candidate, not the last one.
    full = {
        "overall_status": "partial",
        "completion_score": 55,
        "summary": "完整报告",
        "findings": [],
        "dimensions": full_judge_dimensions(),
    }
    thin = {"overall_status": "failed", "summary": "简略总结", "findings": []}
    message = {
        "messages": [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"name": "write_judge_report", "args": {"run_path": "/tmp/run", "report": full}}
                ],
            },
            {"role": "tool", "content": json.dumps({"json_path": "/tmp/run/agent_judge.json"})},
            {"role": "assistant", "content": json.dumps(thin, ensure_ascii=False)},
        ]
    }

    report = normalize_judge_report(extract_json_object(message))

    assert report["summary"] == "完整报告"
    assert report["judge_incomplete"] is False
    assert all(row["source"] != "missing" for row in report["dimensions"].values())


def test_extract_json_object_keeps_latest_preference_among_equal_candidates():
    # 完整度相同时维持「更接近最终回答」的稳定偏好。
    first = {"overall_status": "failed", "completion_score": 10, "summary": "first", "findings": []}
    second = {"overall_status": "success", "completion_score": 90, "summary": "second", "findings": []}
    message = {
        "messages": [
            {"role": "assistant", "content": json.dumps(first)},
            {"role": "assistant", "content": json.dumps(second)},
        ]
    }
    assert extract_json_object(message)["summary"] == "second"


def test_missing_judge_dimensions_distinguish_absent_from_honest_unknown():
    # 合法 unknown（status=unknown、score=null、source=judge）是完整提交，
    # 只有字段缺失（source=missing）才算结构不完整。
    dimensions = full_judge_dimensions()
    assert missing_judge_dimensions({"overall_status": "partial", "dimensions": dimensions}) == []

    del dimensions["verification"]
    del dimensions["efficiency"]
    assert missing_judge_dimensions({"dimensions": dimensions}) == ["verification", "efficiency"]


def test_missing_judge_dimensions_accepts_chinese_aliases():
    dimensions = full_judge_dimensions()
    dimensions[DIMENSION_DEFINITIONS["intent_recognition"]] = dimensions.pop("intent_recognition")
    assert missing_judge_dimensions({"dimensions": dimensions}) == []


def test_missing_judge_dimensions_requires_an_explicit_source():
    # 维度条目缺少 source 字段时无法与「未提交」区分，按结构不完整处理。
    dimensions = full_judge_dimensions()
    dimensions["efficiency"] = {"score": 80, "status": "pass", "summary": "x"}
    assert missing_judge_dimensions({"dimensions": dimensions}) == ["efficiency"]
    assert len(missing_judge_dimensions({"overall_status": "success"})) == 13
    assert len(missing_judge_dimensions("not a report")) == 13


# -- evidence_chain schema ------------------------------------------------


def _chain_dimension():
    return {
        "score": 60,
        "status": "partial",
        "summary": "检测到工具失败，随后进行了纠正操作。",
        "evidence": [{"source": "trajectory.jsonl", "message_index": 17, "quote": "[exit code: 1]"}],
        "recommendation": "在最终回答中明确说明失败、重试和验证结果。",
        "source": "deterministic+judge",
        "evidence_chain": {
            "checks": [
                {
                    "id": "failure_detected",
                    "criterion": "是否出现明确的工具执行失败",
                    "result": "fail",
                    "conclusion": "bash 调用以非零退出码结束。",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 17, "quote": "[exit code: 1]"}],
                    "counter_evidence": [],
                },
                {
                    "id": "recovery_verified",
                    "criterion": "纠正操作是否有成功结果证明",
                    "result": "pass",
                    "conclusion": "修正命令返回了成功结果。",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 19, "quote": "ok"}],
                    "counter_evidence": [{"source": "audit.json", "quote": "err"}],
                },
            ],
            "score_trace": {
                "judge_score": 85,
                "judge_status": "pass",
                "deterministic_score": 60,
                "final_score": 60,
                "rules": [
                    {
                        "id": "error_recovery_cap",
                        "action": "cap",
                        "before": 85,
                        "candidate": 60,
                        "after": 60,
                        "reason": "检测到失败和恢复动作，按确定性规则最高为 partial/60。",
                        "evidence": [{"source": "trajectory.jsonl", "message_index": 17, "quote": "[exit code: 1]"}],
                    }
                ],
            },
            "gaps": ["最终回答未说明恢复结果。"],
        },
    }


def test_normalize_dimension_evidence_chain_roundtrip():
    report = normalize_judge_report(
        {"overall_status": "partial", "dimensions": {"error_recovery": _chain_dimension()}}
    )
    dimension = report["dimensions"]["error_recovery"]

    chain = dimension["evidence_chain"]
    assert [check["id"] for check in chain["checks"]] == ["failure_detected", "recovery_verified"]
    assert chain["checks"][0]["result"] == "fail"
    assert chain["checks"][0]["type"] == "fact"
    assert chain["checks"][1]["counter_evidence"] == [{"source": "audit.json", "quote": "err"}]
    trace = chain["score_trace"]
    assert trace["judge_score"] == 85
    assert trace["judge_status"] == "pass"
    assert trace["deterministic_score"] == 60
    assert trace["final_score"] == 60
    assert trace["rules"][0]["action"] == "cap"
    assert trace["rules"][0]["before"] == 85
    assert trace["rules"][0]["after"] == 60
    assert chain["gaps"] == ["最终回答未说明恢复结果。"]

    # Top-level evidence is the deduplicated compatibility view of the chain's
    # supporting references (checks evidence + score-rule evidence). Counter
    # evidence stays inside the chain — it rebuts, it does not support.
    keys = {(row["source"], row.get("message_index"), row["quote"]) for row in dimension["evidence"]}
    assert keys == {
        ("trajectory.jsonl", 17, "[exit code: 1]"),
        ("trajectory.jsonl", 19, "ok"),
    }


def test_normalize_dimension_evidence_chain_is_stable_under_renormalization():
    report = normalize_judge_report(
        {"overall_status": "partial", "dimensions": {"error_recovery": _chain_dimension()}}
    )
    twice = normalize_judge_report(report)
    assert twice["dimensions"]["error_recovery"] == report["dimensions"]["error_recovery"]

    # JSON round trip loses nothing.
    loaded = json.loads(json.dumps(report, ensure_ascii=False))
    thrice = normalize_judge_report(loaded)
    assert thrice["dimensions"]["error_recovery"] == report["dimensions"]["error_recovery"]


def test_normalize_dimension_without_chain_keeps_legacy_shape():
    report = normalize_judge_report({"overall_status": "success"})
    dimension = report["dimensions"]["intent_recognition"]
    assert "evidence_chain" not in dimension
    assert dimension == {
        "score": None,
        "status": "unknown",
        "summary": "",
        "evidence": [],
        "recommendation": "",
        "source": "missing",
    }


def test_normalize_dimension_chain_sanitizes_bad_types_and_enums():
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "dimensions": {
                "verification": {
                    "score": 50,
                    "status": "partial",
                    "source": "judge",
                    "evidence_chain": {
                        "checks": [
                            "not-a-dict",
                            {
                                "id": "c1",
                                "criterion": "标准",
                                "result": "bogus",
                                "conclusion": "结论",
                                "type": "guess",
                                "evidence": [42, {"source": "evaluation", "quote": "q"}],
                                "counter_evidence": {"source": "expectations.json", "quote": "e"},
                            },
                        ],
                        "score_trace": {
                            "judge_score": "not-a-number" and 80,
                            "rules": [{"id": "r", "action": "bogus", "before": "x", "after": 60}],
                        },
                        "gaps": ["", "缺证据", 42],
                    },
                }
            },
        }
    )
    chain = report["dimensions"]["verification"]["evidence_chain"]
    assert len(chain["checks"]) == 1
    check = chain["checks"][0]
    assert check["result"] == "unknown"  # invalid enum falls back
    assert check["type"] == "inference"  # invalid type never becomes fact
    assert check["evidence"] == [{"source": "evaluation.json", "quote": "q"}]
    assert check["counter_evidence"] == [{"source": "expectations.json", "quote": "e"}]
    rule = chain["score_trace"]["rules"][0]
    assert rule["action"] == "keep"  # invalid action normalized
    assert rule["before"] is None
    assert rule["after"] == 60
    assert chain["gaps"] == ["缺证据", "42"]


def test_normalize_dimension_chain_enforces_length_limits():
    checks = [
        {"id": "c%s" % i, "criterion": "x" * 1000, "result": "pass", "conclusion": "y" * 1000, "type": "fact"}
        for i in range(30)
    ]
    gaps = ["g%s" % i for i in range(30)]
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "dimensions": {"efficiency": {"score": 50, "status": "partial", "source": "judge",
                                          "evidence_chain": {"checks": checks, "gaps": gaps}}},
        }
    )
    chain = report["dimensions"]["efficiency"]["evidence_chain"]
    assert len(chain["checks"]) == 20
    assert len(chain["checks"][0]["criterion"]) == 500
    assert len(chain["checks"][0]["conclusion"]) == 500
    assert len(chain["gaps"]) == 20


def test_render_judge_markdown_shows_evidence_chain_and_legacy_fallback():
    markdown = render_judge_markdown(
        {"overall_status": "partial", "dimensions": {"error_recovery": _chain_dimension()}}
    )
    assert "#### 错误恢复（error_recovery）" in markdown
    assert "原子检查项" in markdown
    assert "failure_detected" in markdown
    assert "反证" in markdown
    assert "分数推导" in markdown
    assert "error_recovery_cap" in markdown
    assert "85" in markdown and "60" in markdown
    assert "证据缺口" in markdown
    assert "机械匹配" in markdown

    legacy = render_judge_markdown(
        {
            "overall_status": "partial",
            "dimensions": {
                "intent_recognition": {"score": 80, "status": "pass", "summary": "s", "source": "judge"}
            },
        }
    )
    assert "旧报告未记录完整证据链" in legacy


def test_render_reliability_markdown_shows_dimension_metrics():
    markdown = render_judge_markdown(
        {
            "overall_status": "partial",
            "dimensions": full_judge_dimensions(),
            "judge_reliability": {
                "unsupported_dimension_count": 2,
                "dimension_evidence_mismatch_count": 1,
                "dimensions_without_valid_evidence": ["verification", "efficiency"],
            },
        }
    )
    assert "Unsupported Dimensions" in markdown
    assert "Dimension Evidence Mismatch" in markdown
    assert "verification" in markdown
