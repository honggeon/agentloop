import json

import pytest

from auditrail.agents.tools import (
    assess_run_quality,
    build_chart_data,
    get_tool_calls,
    load_trajectory_slice,
    search_trajectory,
    write_judge_report,
)
from auditrail.judges.proposals import JudgeProposalError
from tests.conftest import full_judge_dimensions


def _write_run(tmp_path):
    run = tmp_path / "runs" / "20260807-test"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
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
        {"index": 0, "role": "user", "content": "make ppt"},
        {"index": 1, "role": "tool", "content": "ModuleNotFoundError"},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return run


def _write_incomplete_ppt_run(tmp_path):
    run = tmp_path / "runs" / "20260807-ppt-missing-deliverable"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "title": "请生成 10 页中文学术汇报 PPT，并输出完整 PPTX 文件。",
                "message_count": 5,
                "tool_call_count": 2,
                "tool_result_count": 2,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    audit = {
        "message_count": 5,
        "tool_call_count": 2,
        "tool_result_count": 2,
        "findings": [{"severity": "error", "code": "tool_error", "message": "ModuleNotFoundError: No module named 'pptx'"}],
        "tool_calls": [
            {
                "index": 2,
                "tool": "terminal",
                "status": "error",
                "args_preview": "python export_pptx.py",
                "result_preview": "ModuleNotFoundError: No module named 'pptx'",
            }
        ],
        "phases": [{"phase": "execute", "tool_count": 1, "tools": ["terminal"]}],
    }
    (run / "audit.json").write_text(json.dumps(audit, ensure_ascii=False), encoding="utf-8")
    rows = [
        {"index": 0, "role": "user", "content": "请生成 10 页中文学术汇报 PPT，并输出完整 PPTX 文件。"},
        {"index": 1, "role": "assistant", "content": "我将生成 PPTX。"},
        {"index": 2, "role": "tool", "tool_name": "terminal", "content": "ModuleNotFoundError: No module named 'pptx'"},
        {"index": 3, "role": "assistant", "content": "已生成 10 个 SVG 页面，并写好了 export_pptx.py。请安装 python-pptx 后手动运行。"},
        {"index": 4, "role": "assistant", "content": "最终交付：没有 PPTX 文件，需手动安装依赖。"},
    ]
    (run / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows),
        encoding="utf-8",
    )
    return run


def test_load_and_search_trajectory(tmp_path):
    run = _write_run(tmp_path)
    assert load_trajectory_slice(str(run), 0, 1)[0]["role"] == "user"
    matches = search_trajectory(str(run), "ModuleNotFoundError")
    assert matches[0]["index"] == 1


def test_load_trajectory_slice_tolerates_null_numeric_args(tmp_path):
    # LLMs routinely emit null for optional tool parameters; the tool must not crash.
    run = _write_run(tmp_path)
    assert load_trajectory_slice(str(run), None, None)[0]["role"] == "user"
    assert len(load_trajectory_slice(str(run), None, 2)) == 2
    assert len(load_trajectory_slice(str(run), 1, None)) >= 1


def test_get_tool_calls_filters_by_tool_and_status(tmp_path):
    run = _write_run(tmp_path)
    calls = get_tool_calls(str(run), tool_name="terminal", status="error")
    assert len(calls) == 1
    assert calls[0]["result_preview"] == "timeout"


def test_build_chart_data_uses_audit_details(tmp_path):
    run = _write_run(tmp_path)
    charts = build_chart_data(str(run))
    assert charts["tool_distribution"] == [{"tool": "read_file", "count": 1}, {"tool": "terminal", "count": 1}]
    assert charts["phase_timeline"][0]["phase"] == "execute"


def test_assess_run_quality_ignores_system_reminder_injections(tmp_path):
    # DSH injects the skill catalog into user-role rows as <system-reminder>;
    # its ".pptx"/"markdown" mentions are not the user's request.
    run = tmp_path / "runs" / "trivial-answer"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(
        json.dumps({"session_id": "s1", "title": "只回复两个字:收到", "message_count": 4}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "audit.json").write_text(json.dumps({"findings": [], "tool_calls": []}, ensure_ascii=False), encoding="utf-8")
    rows = [
        {"index": 0, "role": "user", "content": "只回复两个字:收到"},
        {"index": 1, "role": "user", "content": "<system-reminder> skills: lark-slides 创建 .pptx 演示文稿, lark-doc 编辑 .docx, lark-sheets .xlsx, lark-markdown .md </system-reminder>"},
        {"index": 2, "role": "assistant", "content": "收到"},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")

    quality = assess_run_quality(str(run))
    assert quality["deliverables"]["requested"] == []
    assert quality["completion"]["status"] != "failed"


def test_assess_run_quality_does_not_count_auditrail_reports_as_deliverables(tmp_path):
    # The run dir always holds auditrail's own tool_audit.md; that must not
    # satisfy a requested .md deliverable.
    run = tmp_path / "runs" / "md-deliverable"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(
        json.dumps({"session_id": "s1", "title": "输出 markdown 报告文件", "message_count": 2}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "audit.json").write_text(json.dumps({"findings": [], "tool_calls": []}, ensure_ascii=False), encoding="utf-8")
    (run / "trajectory.jsonl").write_text(
        json.dumps({"index": 0, "role": "user", "content": "请输出一个 markdown 文件"}) + "\n"
        + json.dumps({"index": 1, "role": "assistant", "content": "好的"}),
        encoding="utf-8",
    )
    (run / "tool_audit.md").write_text("# audit\n", encoding="utf-8")

    quality = assess_run_quality(str(run))
    assert quality["deliverables"]["requested"] == [".md"]
    assert quality["deliverables"]["produced"] == []
    assert quality["deliverables"]["missing"] == [".md"]

    # a real deliverable file does count
    (run / "report.md").write_text("# real\n", encoding="utf-8")
    quality = assess_run_quality(str(run))
    assert quality["deliverables"]["produced"] == [".md"]


def test_assess_run_quality_flags_missing_requested_deliverable(tmp_path):
    run = _write_incomplete_ppt_run(tmp_path)
    quality = assess_run_quality(str(run))

    # Inferred deliverable requests are not authoritative: the agent may have
    # written the file in its own (untrusted, unscannable) workspace, so an
    # empty run bundle means "unverifiable" — no cap, no hard failure. Only
    # evaluation.json artifact checks may declare a deliverable missing.
    assert quality["completion"]["status"] == "unknown"
    assert quality["completion"]["score_cap"] == 100
    assert quality["deliverables"]["requested"] == [".pptx"]
    assert quality["deliverables"]["produced"] == []
    assert quality["deliverables"]["verification"] == "unverifiable"
    assert quality["evidence_chain"][0]["source"] == "summary.json"
    assert quality["evidence_chain"][1]["message_index"] == 2
    delivery = [item for item in quality["findings"] if item["category"] == "delivery"]
    assert delivery and delivery[0]["severity"] == "medium"


def test_write_judge_report_adds_dimensions_and_reliability(tmp_path):
    run = _write_run(tmp_path)
    write_judge_report(
        str(run),
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "工具失败",
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

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    markdown = (run / "agent_judge.md").read_text(encoding="utf-8")
    charts = json.loads((run / "chart_data.json").read_text(encoding="utf-8"))

    assert "tool_selection" in report["dimensions"]
    # The final reliability covers the full published report: the judge
    # finding, the merged quality findings and the deterministic dimension
    # evidence chains are all validated, so the valid count exceeds the single
    # judge-supplied reference.
    assert report["judge_reliability"]["valid_evidence_count"] >= 1
    assert report["judge_reliability"]["invalid_evidence_count"] == 0
    assert report["judge_reliability"]["invalid_evidence"] == []
    assert any(finding["title"] == "Requested deliverable cannot be verified from the run bundle" for finding in report["findings"])
    assert any(row["dimension"] == "tool_selection" for row in report["charts"]["dimension_scores"])
    assert any(row["dimension"] == "tool_selection" for row in charts["dimension_scores"])
    assert "### Evaluation Dimensions" in markdown
    assert "### Judge Reliability" in markdown


def test_write_judge_report_writes_three_files(tmp_path):
    run = _write_run(tmp_path)
    result = write_judge_report(
        str(run),
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "failed",
            "findings": [],
            "dimensions": full_judge_dimensions(),
        },
    )
    assert result["json_path"].endswith("agent_judge.json")
    assert (run / "agent_judge.json").exists()
    assert (run / "agent_judge.md").exists()
    assert (run / "chart_data.json").exists()


def test_write_judge_report_downgrades_fact_findings_without_valid_evidence(tmp_path):
    # A judge finding typed as fact whose evidence quotes cannot be traced back
    # to the run artifacts must be retyped as inference, not presented as fact.
    run = _write_run(tmp_path)
    write_judge_report(
        str(run),
        {
            "overall_status": "failed",
            "completion_score": 18,
            "summary": "幻觉 finding 测试",
            "dimensions": full_judge_dimensions(),
            "findings": [
                {
                    "severity": "critical",
                    "category": "delivery",
                    "title": "Requested deliverables .pptx were never produced",
                    "type": "fact",
                    "evidence": [{"source": "summary.json", "quote": "requested deliverables: .pptx, .docx"}],
                    "recommendation": "block success",
                },
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "terminal failed",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 1, "quote": "ModuleNotFoundError"}],
                    "recommendation": "fix dependency",
                },
            ],
        },
    )

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    by_title = {finding["title"]: finding for finding in report["findings"]}
    assert by_title["Requested deliverables .pptx were never produced"]["type"] == "inference"
    assert by_title["terminal failed"]["type"] == "fact"
    assert any("downgraded 1 fact findings" in note for note in report["judge_reliability"]["notes"])
    # the written report must survive re-normalization
    from auditrail.agents.schemas import normalize_judge_report

    renormalized = normalize_judge_report(report)
    assert {f["title"]: f["type"] for f in renormalized["findings"]}["Requested deliverables .pptx were never produced"] == "inference"


def test_write_judge_report_merges_quality_checks_and_caps_completion(tmp_path):
    run = _write_incomplete_ppt_run(tmp_path)
    write_judge_report(
        str(run),
        {
            "overall_status": "success",
            "completion_score": 95,
            "confidence": "high",
            "summary": "模型声称任务完成。",
            "findings": [],
            "dimensions": full_judge_dimensions(),
        },
    )

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    # Inferred-missing deliverables are unverifiable, not failures: the gate no
    # longer caps the score or forces overall_status=failed (only an
    # authoritative evaluation.json artifact check may do that).
    assert report["overall_status"] == "success"
    assert report["completion_score"] == 95
    assert report["metrics"]["requested_deliverables"] == [".pptx"]
    assert report["metrics"]["produced_deliverables"] == []
    assert report["charts"]["quality_summary"]["missing_deliverables"] == 1
    assert any(finding["category"] == "delivery" for finding in report["findings"])


def test_write_judge_report_recomputes_finding_severity_after_quality_merge(tmp_path):
    run = _write_incomplete_ppt_run(tmp_path)
    write_judge_report(
        str(run),
        {
            "overall_status": "success",
            "completion_score": 95,
            "confidence": "high",
            "summary": "模型声称任务完成。",
            "dimensions": full_judge_dimensions(),
            "findings": [
                {
                    "severity": "medium",
                    "category": "completion",
                    "title": "claim",
                    "type": "fact",
                    "recommendation": "verify",
                }
            ],
            "charts": {"finding_severity": [{"severity": "medium", "count": 1}]},
        },
    )

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    severity = {item["severity"]: item["count"] for item in report["charts"]["finding_severity"]}

    # gate adds one medium delivery finding (unverifiable, no longer critical)
    # and one high tool-failure finding on top of the proposed medium finding
    assert severity == {"high": 1, "medium": 2}


def test_tools_resolve_partial_run_path(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    monkeypatch.setenv("AUDITRAIL_RUNS_DIR", str(run.parent))
    assert load_trajectory_slice("20260807-test", 0, 1)[0]["role"] == "user"
    assert load_trajectory_slice("20260807", 0, 1)[0]["role"] == "user"


def test_tools_return_error_for_unknown_run(tmp_path, monkeypatch):
    _write_run(tmp_path)
    monkeypatch.delenv("AUDITRAIL_RUNS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    result = load_trajectory_slice("no-such-run")
    assert "error" in result
    assert "list_runs" in result["error"]
    assert "error" in search_trajectory("no-such-run", "x")
    assert "error" in get_tool_calls("no-such-run")


def _complete_proposal(**overrides):
    proposal = {
        "overall_status": "partial",
        "completion_score": 55,
        "confidence": "medium",
        "summary": "完整提案",
        "findings": [],
        "dimensions": full_judge_dimensions(),
    }
    proposal.update(overrides)
    return proposal


def test_write_judge_report_rejects_incomplete_proposal_without_creating_files(tmp_path):
    # 发布前门禁：裁判没有提交全部 13 个维度时不得写出任何报告文件。
    run = _write_run(tmp_path)
    with pytest.raises(JudgeProposalError):
        write_judge_report(str(run), {"overall_status": "success", "summary": "空 dimensions"})
    assert not (run / "agent_judge.json").exists()
    assert not (run / "agent_judge.md").exists()
    assert not (run / "chart_data.json").exists()
    assert not (run / "judges").exists()


def test_write_judge_report_rejects_incomplete_proposal_and_preserves_active_report(tmp_path):
    # 示例缺陷形态：proposal 只提交了两个维度，其余 11 个 source=missing。
    # 门禁必须抛出包含缺失维度 key 的明确错误，且完全不动已发布的有效报告。
    run = _write_run(tmp_path)
    write_judge_report(str(run), _complete_proposal(summary="有效旧报告"))
    before = {
        name: (run / name).read_text(encoding="utf-8")
        for name in ("agent_judge.json", "agent_judge.md", "chart_data.json")
    }

    incomplete = {
        "overall_status": "success",
        "completion_score": 90,
        "summary": "只提交了两个维度",
        "dimensions": {
            "evidence_citation": {"score": 100, "status": "pass", "summary": "s", "source": "judge"},
            "execution_closure": {"score": 65, "status": "partial", "summary": "s", "source": "judge"},
        },
    }
    with pytest.raises(JudgeProposalError) as excinfo:
        write_judge_report(str(run), incomplete)

    message = str(excinfo.value)
    assert "11" in message
    assert "intent_recognition" in message
    assert "efficiency" in message
    # 确定性维度 assess_dimensions 本可为这两个维度补分，但门禁必须先于补分拦截。
    assert "evidence_citation" not in message
    assert "execution_closure" not in message
    for name, content in before.items():
        assert (run / name).read_text(encoding="utf-8") == content
    assert not (run / "judges").exists()


def test_write_judge_report_accepts_honest_unknown_dimensions(tmp_path):
    # 合法 unknown：裁判提交了全部维度但部分证据不足（status=unknown、
    # score=null、source=judge），报告结构完整，可以发布，且不伪造分数。
    run = _write_run(tmp_path)
    write_judge_report(str(run), _complete_proposal())

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    assert report["judge_incomplete"] is False
    verification = report["dimensions"]["verification"]
    assert verification["source"] == "judge"
    assert verification["status"] == "unknown"
    assert verification["score"] is None


def test_first_tool_failure_ignores_keyword_lookalikes(tmp_path):
    # Regression: a bare-keyword text scan reported the MIT LICENSE line
    # "Permission is hereby granted" as the run's first tool failure. Only the
    # audit's structured status may report failures now.
    run = tmp_path / "runs" / "license-read"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps(
            {
                "message_count": 2,
                "tool_call_count": 1,
                "tool_result_count": 1,
                "findings": [],
                "tool_calls": [{"index": 1, "tool": "read", "status": "ok", "args_preview": "LICENSE"}],
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {"index": 0, "role": "user", "content": "read the license"},
        {"index": 1, "role": "tool", "content": "MIT License\n\nPermission is hereby granted, free of charge, to deal in the Software without restriction..."},
        {"index": 2, "role": "assistant", "content": "The license is MIT."},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    quality = assess_run_quality(str(run))

    assert all(finding["category"] != "tool" for finding in quality["findings"])


# -- full-report evidence chain contract ----------------------------------


def _write_envelope_run(tmp_path):
    run = tmp_path / "runs" / "envelope-run"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "title": "生成报告"}), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps({"tool_calls": [{"index": 1, "tool": "bash", "status": "ok", "result_preview": "done"}], "findings": []}),
        encoding="utf-8",
    )
    (run / "trajectory.jsonl").write_text(
        json.dumps({"index": 0, "role": "user", "content": "生成报告"}) + "\n"
        + json.dumps({"index": 1, "role": "assistant", "content": "完成"}),
        encoding="utf-8",
    )
    return run


def test_write_judge_report_validates_quality_findings_evidence(tmp_path):
    # Ordering contract: quality findings (from evaluation.json envelope
    # checks) are merged BEFORE evidence validation, so their references are
    # part of the persisted reliability instead of bypassing it.
    from auditrail.evaluation import make_artifact_check, new_envelope, write_evaluation

    run = _write_envelope_run(tmp_path)
    envelope = new_envelope("completed")
    envelope["artifact_checks"] = [
        make_artifact_check("report.pptx", required=True, status="fail", exists=False, valid=False)
    ]
    write_evaluation(run, envelope)

    write_judge_report(str(run), _complete_proposal())

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    quality_finding = [
        finding for finding in report["findings"] if finding["title"] == "Required artifact missing: report.pptx"
    ]
    assert quality_finding, report["findings"]
    evidence = quality_finding[0]["evidence"][0]
    assert evidence["source"] == "evaluation.json"
    # The quote is verbatim envelope content (the artifact path), not a
    # synthesized sentence; it therefore passes mechanical validation...
    assert evidence["quote"] == "report.pptx"
    assert evidence["quote"] in (run / "evaluation.json").read_text(encoding="utf-8")
    # ...and the finding keeps its fact type because its evidence validated.
    assert quality_finding[0]["type"] == "fact"
    assert report["judge_reliability"]["invalid_evidence_count"] == 0
    assert report["judge_reliability"]["valid_evidence_count"] > 0


def test_write_judge_report_flags_invalid_quality_finding_evidence(tmp_path):
    # Proof that quality findings cannot bypass validation: an envelope check
    # whose evidence_ref points at an artifact that does not contain the quote
    # lands in the persisted invalid_evidence list.
    from auditrail.evaluation import make_check, new_envelope, write_evaluation

    run = _write_envelope_run(tmp_path)
    envelope = new_envelope("completed")
    envelope["deterministic_checks"] = [
        make_check(
            "chk-1",
            "tool_errors",
            "fail",
            blocking=True,
            blocking_source="structured_audit",
            evidence_refs=["trajectory.jsonl"],
            message="this message never appears in trajectory",
        )
    ]
    write_evaluation(run, envelope)

    write_judge_report(str(run), _complete_proposal())

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    invalid = report["judge_reliability"]["invalid_evidence"]
    assert any("this message never appears" in row.get("quote", "") for row in invalid)
    # The fabricated fact is downgraded to inference in the published report.
    blocking = [finding for finding in report["findings"] if finding["title"].startswith("Blocking deterministic check")]
    assert blocking and blocking[0]["type"] == "inference"


def test_write_judge_report_normalizes_unevidenced_judge_dimension_to_unknown(tmp_path):
    run = _write_envelope_run(tmp_path)
    proposal = _complete_proposal(
        dimensions=full_judge_dimensions(
            intent_recognition={"score": 90, "status": "pass", "summary": "猜的", "evidence": [], "source": "judge"},
            verification={
                "score": 40,
                "status": "fail",
                "summary": "只有无效引用",
                "evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "不存在的内容"}],
                "source": "judge",
            },
        )
    )
    write_judge_report(str(run), proposal)

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    for key in ("intent_recognition", "verification"):
        dimension = report["dimensions"][key]
        assert dimension["score"] is None
        assert dimension["status"] == "unknown"
        assert dimension["source"] == "judge"
        assert any("缺少有效证据" in gap for gap in dimension["evidence_chain"]["gaps"])
    reliability = report["judge_reliability"]
    assert sorted(reliability["dimensions_without_valid_evidence"]) == ["intent_recognition", "verification"]
    assert reliability["unsupported_dimension_count"] == 1
    assert reliability["dimension_evidence_mismatch_count"] == 1
    # The invalid quote is still recorded with its failure reason.
    assert any(row.get("location") == "dimensions.verification" for row in reliability["invalid_evidence"])


def test_write_judge_report_evidenced_judge_dimension_keeps_score(tmp_path):
    run = _write_envelope_run(tmp_path)
    proposal = _complete_proposal(
        dimensions=full_judge_dimensions(
            intent_recognition={
                "score": 90,
                "status": "pass",
                "summary": "识别正确",
                "evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "生成报告"}],
                "source": "judge",
            }
        )
    )
    write_judge_report(str(run), proposal)

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    dimension = report["dimensions"]["intent_recognition"]
    assert dimension["score"] == 90
    assert dimension["status"] == "pass"
    assert report["judge_reliability"]["dimensions_without_valid_evidence"] == []


def test_write_judge_report_records_deterministic_score_trace(tmp_path):
    run = _write_run(tmp_path)  # one failed terminal call, one ok read_file call
    write_judge_report(
        str(run),
        _complete_proposal(
            dimensions=full_judge_dimensions(
                error_recovery={"score": 85, "status": "pass", "summary": "恢复了", "source": "judge"}
            )
        ),
    )

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    dimension = report["dimensions"]["error_recovery"]
    assert dimension["score"] == 60
    assert dimension["status"] == "partial"
    assert dimension["source"] == "deterministic+judge"
    trace = dimension["evidence_chain"]["score_trace"]
    assert trace["judge_score"] == 85
    assert trace["deterministic_score"] == 60
    assert trace["final_score"] == 60
    assert trace["rules"][0]["action"] == "cap"
    # Generated chain evidence survives the full-pipeline validation.
    assert report["judge_reliability"]["invalid_evidence_count"] == 0
    markdown = (run / "agent_judge.md").read_text(encoding="utf-8")
    assert "分数推导" in markdown
    assert "error_recovery_cap" in markdown


def test_write_judge_report_engine_archive_matches_root_contract(tmp_path):
    run = _write_envelope_run(tmp_path)
    write_judge_report(
        str(run),
        _complete_proposal(),
        judge_metadata={"engine": "dsh", "runtime": "dsh"},
        judge_engine="dsh",
        published_at="2026-10-13T00:00:00+00:00",
    )

    root = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    archived = json.loads((run / "judges" / "dsh" / "agent_judge.json").read_text(encoding="utf-8"))
    assert root == archived
    assert "evidence_chain" in json.dumps(root["dimensions"])
