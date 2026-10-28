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


# -- deterministic score traces and evidence chains -----------------------


def _write_recovery_run(tmp_path, statuses=("error", "ok")):
    """A run whose audit trail contains the given tool-call statuses in order."""
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"message_count": 4, "tool_call_count": len(statuses)}), encoding="utf-8")
    calls = []
    previews = {"error": "[exit code: 1]", "ok": "file written"}
    for offset, status in enumerate(statuses):
        calls.append(
            {
                "index": offset + 1,
                "tool": "bash",
                "status": status,
                "args_preview": "cat x.py",
                "result_preview": previews[status],
            }
        )
    (run / "audit.json").write_text(
        json.dumps({"tool_call_count": len(calls), "tool_calls": calls, "findings": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    rows = [
        {"index": 0, "role": "user", "content": "读取文件"},
        {"index": 1, "role": "tool", "tool_name": "bash", "content": "[stderr] cat: x.py: No such file [exit code: 1]"},
        {"index": 2, "role": "tool", "tool_name": "bash", "content": "file written"},
        {"index": 3, "role": "assistant", "content": "已完成修复。"},
    ]
    (run / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )
    return run


def _report_with_judge_scores(scores):
    return normalize_judge_report(
        {
            "overall_status": "partial",
            "completion_score": 60,
            "summary": "x",
            "dimensions": {
                key: {"score": score, "status": "pass", "summary": "judge", "source": "judge"}
                for key, score in scores.items()
            },
        }
    )


def _assert_generated_evidence_valid(run, dimensions):
    report = normalize_judge_report({"overall_status": "partial", "dimensions": dimensions})
    reliability = validate_judge_evidence(run, report)
    assert reliability["invalid_evidence_count"] == 0, reliability["invalid_evidence"]


def test_error_recovery_cap_records_judge_score_and_trace(tmp_path):
    # Judge 85 + deterministic cap 60: the canonical traceable case.
    run = _write_recovery_run(tmp_path)
    report = _report_with_judge_scores({"error_recovery": 85})
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)
    dimension = dimensions["error_recovery"]

    assert dimension["score"] == 60
    assert dimension["status"] == "partial"
    assert dimension["source"] == "deterministic+judge"
    chain = dimension["evidence_chain"]
    trace = chain["score_trace"]
    assert trace["judge_score"] == 85
    assert trace["judge_status"] == "pass"
    assert trace["deterministic_score"] == 60
    assert trace["final_score"] == 60
    rule = trace["rules"][0]
    assert rule["id"] == "error_recovery_cap"
    assert rule["action"] == "cap"
    assert rule["before"] == 85
    assert rule["candidate"] == 60
    assert rule["after"] == 60
    assert rule["reason"]
    assert rule["evidence"]
    check_ids = [check["id"] for check in chain["checks"]]
    assert check_ids == ["failure_detected", "recovery_attempted", "recovery_verified"]
    assert [check["result"] for check in chain["checks"]] == ["fail", "pass", "pass"]
    # The compatibility view carries the supporting references, deduplicated.
    assert dimension["evidence"]
    _assert_generated_evidence_valid(run, dimensions)


def test_error_recovery_attempted_but_unverified_does_not_claim_success(tmp_path):
    run = _write_recovery_run(tmp_path, statuses=("error", "error"))
    report = _report_with_judge_scores({"error_recovery": 90})
    reliability = validate_judge_evidence(run, report)

    dimension = assess_dimensions(run, report, reliability)["error_recovery"]

    assert dimension["score"] == 45
    assert dimension["status"] == "fail"
    checks = {check["id"]: check for check in dimension["evidence_chain"]["checks"]}
    assert checks["recovery_attempted"]["result"] == "pass"
    assert checks["recovery_verified"]["result"] == "fail"
    assert "恢复未被验证" in checks["recovery_verified"]["conclusion"]
    _assert_generated_evidence_valid(run, assess_dimensions(run, report, reliability))


def test_error_recovery_failure_without_recovery(tmp_path):
    run = _write_recovery_run(tmp_path, statuses=("error",))
    report = _report_with_judge_scores({"error_recovery": 80})
    reliability = validate_judge_evidence(run, report)

    dimension = assess_dimensions(run, report, reliability)["error_recovery"]

    assert dimension["score"] == 30
    assert dimension["status"] == "fail"
    checks = {check["id"]: check for check in dimension["evidence_chain"]["checks"]}
    assert checks["failure_detected"]["result"] == "fail"
    assert checks["recovery_attempted"]["result"] == "fail"
    assert checks["recovery_verified"]["result"] == "fail"


def test_error_recovery_no_failure_stays_unknown_without_chain(tmp_path):
    run = _write_recovery_run(tmp_path, statuses=("ok",))
    report = _report_with_judge_scores({})
    reliability = validate_judge_evidence(run, report)

    dimension = assess_dimensions(run, report, reliability)["error_recovery"]

    assert dimension["status"] == "unknown"
    assert dimension["score"] is None
    assert "evidence_chain" not in dimension


def test_deterministic_set_and_keep_actions_are_recorded(tmp_path):
    run = _write_recovery_run(tmp_path)
    # execution_closure: judge left it unknown -> set.
    # tool_selection: judge 30 <= deterministic candidate 50 -> keep.
    report = normalize_judge_report(
        {
            "overall_status": "success",
            "completion_score": 80,
            "summary": "x",
            "dimensions": {
                "execution_closure": {"score": None, "status": "unknown", "source": "judge"},
                "tool_selection": {"score": 30, "status": "fail", "source": "judge"},
            },
        }
    )
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    closure = dimensions["execution_closure"]
    assert closure["score"] == 90
    assert closure["status"] == "pass"
    closure_rule = closure["evidence_chain"]["score_trace"]["rules"][0]
    assert closure_rule["action"] == "set"
    assert closure_rule["before"] is None
    assert closure_rule["after"] == 90

    selection = dimensions["tool_selection"]
    assert selection["score"] == 30  # judge score kept, never raised
    assert selection["status"] == "fail"
    keep_rule = selection["evidence_chain"]["score_trace"]["rules"][0]
    assert keep_rule["action"] == "keep"
    assert keep_rule["before"] == 30
    assert keep_rule["after"] == 30
    _assert_generated_evidence_valid(run, dimensions)


def test_score_and_status_stay_consistent(tmp_path):
    run = _write_recovery_run(tmp_path, statuses=("error", "ok"))
    report = _report_with_judge_scores({"error_recovery": 85, "tool_selection": 95, "execution_closure": 88})
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)
    for key in ("error_recovery", "tool_selection", "execution_closure", "evidence_citation"):
        dimension = dimensions[key]
        score, status = dimension["score"], dimension["status"]
        if score is None:
            assert status == "unknown"
        elif score >= 80:
            assert status == "pass"
        elif score >= 50:
            assert status == "partial"
        else:
            assert status == "fail"


# -- M3.2 expectation 驱动 intent_recognition 封顶（契约 C4）----------------


def _write_envelope_run(tmp_path, results=None, status="completed"):
    """A tool run plus an evaluation.json envelope with the given expectation_results."""
    run = _write_tool_run(tmp_path)
    from auditrail.evaluation import new_envelope, write_evaluation

    envelope = new_envelope(status)
    envelope["expectation_results"] = list(results or [])
    write_evaluation(run, envelope)
    return run


def _expectation_result(expectation_id, required, status):
    from auditrail.evaluation import make_expectation_result

    return make_expectation_result(expectation_id, required=required, status=status, message="断言未通过")


def _intent_dimension(run, judge_score=90):
    report = _report_with_judge_scores({"intent_recognition": judge_score})
    reliability = validate_judge_evidence(run, report)
    return assess_dimensions(run, report, reliability)


def test_intent_recognition_required_failure_caps_at_50(tmp_path):
    run = _write_envelope_run(tmp_path, [_expectation_result("req-output-orders", True, "fail")])
    report = _report_with_judge_scores({"intent_recognition": 90})
    reliability = validate_judge_evidence(run, report)

    dimension = assess_dimensions(run, report, reliability)["intent_recognition"]

    assert dimension["score"] == 50
    assert dimension["status"] == "partial"
    assert dimension["source"] == "deterministic+judge"
    trace = dimension["evidence_chain"]["score_trace"]
    rule = trace["rules"][0]
    assert rule["id"] == "intent_recognition_cap"
    assert rule["action"] == "cap"
    assert rule["before"] == 90
    assert rule["candidate"] == 50
    assert rule["after"] == 50
    assert "req-output-orders" in rule["reason"]
    assert rule["evidence"]
    _assert_generated_evidence_valid(run, assess_dimensions(run, report, reliability))


def test_intent_recognition_optional_failure_caps_at_70(tmp_path):
    run = _write_envelope_run(
        tmp_path,
        [_expectation_result("req-ok", True, "pass"), _expectation_result("opt-format", False, "fail")],
    )
    dimension = _intent_dimension(run, judge_score=95)["intent_recognition"]

    assert dimension["score"] == 70
    assert dimension["status"] == "partial"
    rule = dimension["evidence_chain"]["score_trace"]["rules"][0]
    assert rule["action"] == "cap"
    assert rule["candidate"] == 70
    assert "opt-format" in rule["reason"]
    _assert_generated_evidence_valid(run, _intent_dimension(run, judge_score=95))


def test_intent_recognition_lower_judge_score_is_kept(tmp_path):
    run = _write_envelope_run(tmp_path, [_expectation_result("req-1", True, "fail")])
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "completion_score": 60,
            "summary": "x",
            "dimensions": {
                "intent_recognition": {"score": 40, "status": "fail", "summary": "judge", "source": "judge"},
            },
        }
    )
    reliability = validate_judge_evidence(run, report)
    dimension = assess_dimensions(run, report, reliability)["intent_recognition"]

    assert dimension["score"] == 40  # judge score kept, never raised
    assert dimension["status"] == "fail"
    rule = dimension["evidence_chain"]["score_trace"]["rules"][0]
    assert rule["action"] == "keep"
    assert rule["before"] == 40
    assert rule["after"] == 40


def test_intent_recognition_all_pass_does_not_trigger(tmp_path):
    run = _write_envelope_run(tmp_path, [_expectation_result("req-1", True, "pass")])
    dimension = _intent_dimension(run, judge_score=90)["intent_recognition"]

    assert dimension["score"] == 90
    assert dimension["source"] == "judge"
    assert "evidence_chain" not in dimension


def test_intent_recognition_all_unknown_does_not_trigger(tmp_path):
    run = _write_envelope_run(tmp_path, [_expectation_result("req-1", True, "unknown")])
    dimension = _intent_dimension(run, judge_score=90)["intent_recognition"]

    assert dimension["score"] == 90
    assert "evidence_chain" not in dimension


def test_intent_recognition_missing_or_unusable_envelope_does_not_trigger(tmp_path):
    # 无信封
    run = _write_tool_run(tmp_path)
    dimension = _intent_dimension(run, judge_score=90)["intent_recognition"]
    assert dimension["score"] == 90
    assert "evidence_chain" not in dimension

    # 非 completed 信封（pending）
    other = tmp_path / "other"
    other.mkdir()
    run2 = _write_envelope_run(other, [_expectation_result("req-1", True, "fail")], status="pending")
    dimension2 = _intent_dimension(run2, judge_score=90)["intent_recognition"]
    assert dimension2["score"] == 90
    assert "evidence_chain" not in dimension2


# -- evidence_citation 反证契约（契约 C-fix，基线报告 §7.5 ①）----------------


def _counter_evidence_report(counter_quote, support_quote="查一下 agent 评测", support_index=0):
    """evidence_citation 维度：一条支撑引用 + 一条反证引用。"""
    return normalize_judge_report(
        {
            "overall_status": "partial",
            "completion_score": 60,
            "summary": "x",
            "dimensions": {
                "evidence_citation": {
                    "score": 90,
                    "status": "pass",
                    "source": "judge",
                    "evidence": [],
                    "evidence_chain": {
                        "checks": [
                            {
                                "id": "citation_quality",
                                "criterion": "引用是否逐字真实",
                                "result": "pass",
                                "conclusion": "支撑结论的引用逐字真实。",
                                "type": "fact",
                                "evidence": [
                                    {"source": "trajectory.jsonl", "message_index": support_index, "quote": support_quote}
                                ],
                                "counter_evidence": [
                                    {"source": "trajectory.jsonl", "message_index": 2, "quote": counter_quote}
                                ],
                            }
                        ]
                    },
                }
            },
        }
    )


def test_evidence_citation_counter_only_invalid_does_not_zero(tmp_path):
    # 反证（counter_evidence）的 quote 是转述散文、未通过逐字校验，但支撑侧
    # 引用干净：维度不清零，按 coverage 计分，并在 gaps 中记录反证问题。
    run = _write_tool_run(tmp_path)
    report = _counter_evidence_report(counter_quote="转述：最终回答内容空泛")
    reliability = validate_judge_evidence(run, report)
    assert reliability["invalid_counter_evidence_count"] == 1
    assert reliability["invalid_support_evidence_count"] == 0

    dimensions = assess_dimensions(run, report, reliability)
    dimension = dimensions["evidence_citation"]

    # 归一化会把 check 的支撑引用并入维度顶层 evidence，total=3（2 支撑 + 1
    # 反证），valid=2 → coverage 0.6667 → 67 分。
    assert dimension["score"] == 67
    assert dimension["status"] == "partial"
    gaps = dimension["evidence_chain"]["gaps"]
    assert any("反证" in gap and "逐字校验" in gap for gap in gaps), gaps


def test_evidence_citation_support_invalid_still_zeroes(tmp_path):
    # 支撑侧引用无效（check evidence 转述散文）：维持原行为，维度清零判 fail。
    run = _write_tool_run(tmp_path)
    report = _counter_evidence_report(counter_quote="最终回答", support_quote="转述的支撑引用")
    reliability = validate_judge_evidence(run, report)
    # 归一化并入使无效支撑引用在顶层与 check 中各计一次。
    assert reliability["invalid_support_evidence_count"] == 2
    assert reliability["invalid_counter_evidence_count"] == 0

    dimensions = assess_dimensions(run, report, reliability)
    dimension = dimensions["evidence_citation"]

    assert dimension["score"] == 0
    assert dimension["status"] == "fail"


def test_evidence_citation_mixed_invalid_zeroes(tmp_path):
    # 支撑侧与反证同时无效：清零，reason 同时提及两侧。
    run = _write_tool_run(tmp_path)
    report = _counter_evidence_report(counter_quote="转述的反证", support_quote="转述的支撑引用")
    reliability = validate_judge_evidence(run, report)
    assert reliability["invalid_support_evidence_count"] == 2
    assert reliability["invalid_counter_evidence_count"] == 1

    dimensions = assess_dimensions(run, report, reliability)
    dimension = dimensions["evidence_citation"]

    assert dimension["score"] == 0
    assert dimension["status"] == "fail"
    rule = dimension["evidence_chain"]["score_trace"]["rules"][-1]
    assert "支撑" in rule["reason"] and "反证" in rule["reason"]


def test_evidence_citation_legacy_reliability_without_split_zeroes(tmp_path):
    # 旧可靠性数据（无 support/counter 拆分字段）：任一无效引用仍清零，向后兼容。
    run = _write_tool_run(tmp_path)
    report = _counter_evidence_report(counter_quote="转述的反证")
    reliability = validate_judge_evidence(run, report)
    assert reliability["invalid_evidence_count"] == 1
    del reliability["invalid_support_evidence_count"]
    del reliability["invalid_counter_evidence_count"]

    dimensions = assess_dimensions(run, report, reliability)
    dimension = dimensions["evidence_citation"]

    assert dimension["score"] == 0
    assert dimension["status"] == "fail"
