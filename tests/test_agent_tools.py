import json

from agentloop.agents.tools import (
    assess_run_quality,
    build_chart_data,
    get_tool_calls,
    load_trajectory_slice,
    search_trajectory,
    write_judge_report,
)


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


def test_assess_run_quality_flags_missing_requested_deliverable(tmp_path):
    run = _write_incomplete_ppt_run(tmp_path)
    quality = assess_run_quality(str(run))

    assert quality["completion"]["status"] == "failed"
    assert quality["completion"]["score_cap"] == 40
    assert quality["deliverables"]["requested"] == [".pptx"]
    assert quality["deliverables"]["produced"] == []
    assert quality["evidence_chain"][0]["source"] == "summary.json"
    assert quality["evidence_chain"][1]["message_index"] == 2
    assert any(item["category"] == "delivery" for item in quality["findings"])


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
        },
    )
    assert result["json_path"].endswith("agent_judge.json")
    assert (run / "agent_judge.json").exists()
    assert (run / "agent_judge.md").exists()
    assert (run / "chart_data.json").exists()


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
        },
    )

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    assert report["overall_status"] == "failed"
    assert report["completion_score"] == 40
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

    assert severity == {"critical": 1, "high": 1, "medium": 1}


def test_tools_resolve_partial_run_path(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    monkeypatch.setenv("AGENTLOOP_RUNS_DIR", str(run.parent))
    assert load_trajectory_slice("20260807-test", 0, 1)[0]["role"] == "user"
    assert load_trajectory_slice("20260807", 0, 1)[0]["role"] == "user"


def test_tools_return_error_for_unknown_run(tmp_path, monkeypatch):
    _write_run(tmp_path)
    monkeypatch.delenv("AGENTLOOP_RUNS_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    result = load_trajectory_slice("no-such-run")
    assert "error" in result
    assert "list_runs" in result["error"]
    assert "error" in search_trajectory("no-such-run", "x")
    assert "error" in get_tool_calls("no-such-run")
