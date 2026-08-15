import json

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import app
from backend.app.routes.runs import _render_judge_markdown


def _write_run(tmp_path):
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
    (run / "audit.json").write_text(json.dumps({"tool_calls": [], "findings": [], "phases": []}), encoding="utf-8")
    (run / "trajectory.jsonl").write_text("", encoding="utf-8")
    return run


def test_render_judge_markdown_includes_dimensions_and_reliability():
    markdown = _render_judge_markdown(
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


def test_health():
    client = TestClient(app)
    assert client.get("/api/health").json()["status"] == "ok"


def test_list_runs(tmp_path, monkeypatch):
    _write_run(tmp_path)
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: Settings(runs_dir=tmp_path / "runs"))
    client = TestClient(app)
    data = client.get("/api/runs").json()
    assert data["runs"][0]["name"] == "run-1"


def test_get_run_summary(tmp_path, monkeypatch):
    _write_run(tmp_path)
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)
    client = TestClient(app)
    assert client.get("/api/runs/run-1").json()["summary"]["session_id"] == "s1"


def test_export_run_markdown_report_includes_runtime_and_judge(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "summary.json").write_text(
        json.dumps({"session_id": "s1", "model": "gpt-5", "message_count": 2, "assistant_count": 1, "tool_call_count": 1}),
        encoding="utf-8",
    )
    (run / "audit.json").write_text(
        json.dumps(
            {
                "tool_call_count": 1,
                "tool_calls": [{"tool": "search", "status": "ok"}],
                "findings": [{"severity": "warning", "code": "missing_evidence", "message": "missing proof"}],
                "phases": [],
            }
        ),
        encoding="utf-8",
    )
    (run / "agent_judge.json").write_text(
        json.dumps(
            {
                "overall_status": "success",
                "completion_score": 92,
                "confidence": "high",
                "summary": "任务完成质量较高。",
                "findings": [{"severity": "info", "title": "Evidence", "recommendation": "保留导出记录。"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)

    client = TestClient(app)
    response = client.get("/api/runs/run-1/export.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert 'filename="run-1-evaluation.md"' in response.headers["content-disposition"]
    assert "# Evaluation Report: run-1" in response.text
    assert "任务完成质量较高。" in response.text
    assert "| Status | success |" in response.text
    assert "| Tool Calls | 1 |" in response.text
    assert "保留导出记录。" in response.text


def test_get_run_judge_recomputes_stale_finding_severity(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "agent_judge.json").write_text(
        json.dumps(
            {
                "overall_status": "failed",
                "completion_score": 20,
                "confidence": "high",
                "summary": "failed",
                "findings": [
                    {"severity": "critical", "title": "a"},
                    {"severity": "critical", "title": "b"},
                    {"severity": "high", "title": "c"},
                ],
                "charts": {"finding_severity": [{"severity": "critical", "count": 1}]},
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)

    client = TestClient(app)
    data = client.get("/api/runs/run-1/judge").json()

    assert data["charts"]["finding_severity"] == [
        {"severity": "critical", "count": 2},
        {"severity": "high", "count": 1},
    ]


def test_get_runtime_metrics_derives_lifecycle_health_and_errors(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "summary.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "model": "m1",
                "message_count": 4,
                "assistant_count": 2,
                "tool_call_count": 2,
                "total_tokens": 23000,
            }
        ),
        encoding="utf-8",
    )
    (run / "audit.json").write_text(
        json.dumps(
            {
                "message_count": 4,
                "assistant_count": 2,
                "tool_call_count": 2,
                "tool_calls": [
                    {"index": 1, "tool_call_id": "call-1", "tool": "search", "status": "ok"},
                    {"index": 2, "tool_call_id": "call-2", "tool": "terminal", "status": "error"},
                ],
                "findings": [
                    {"severity": "error", "code": "tool_error", "message": "terminal failed"},
                    {"severity": "error", "code": "schema_validation", "message": "invalid output"},
                ],
                "phases": [{"phase": "use tools", "tool_count": 2, "tools": ["search", "terminal"]}],
            }
        ),
        encoding="utf-8",
    )
    (run / "trajectory.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "timestamp": 1000, "content": "start"}),
                json.dumps({"role": "assistant", "timestamp": 4000, "tool_calls": [{"id": "call-1"}]}),
                json.dumps({"role": "tool", "timestamp": 9000, "tool_call_id": "call-1", "content": "ok"}),
                json.dumps({"role": "assistant", "timestamp": 11000, "content": "done"}),
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)

    client = TestClient(app)
    data = client.get("/api/runs/run-1/runtime-metrics").json()

    assert data["traceId"] == "s1"
    assert data["status"] == "failed"
    assert data["duration"] == 10
    assert data["executionMetrics"]["total_steps"] == 4
    assert data["llmMetrics"]["calls"] == 2
    assert data["llmMetrics"]["totalTokens"] == 23000
    assert data["toolMetrics"]["calls"] == 2
    assert data["toolMetrics"]["failed"] == 1
    assert data["errorMetrics"]["toolError"] == 1
    assert data["errorMetrics"]["validationFailed"] == 1
    assert data["healthScore"]["total"] < 90
    assert len(data["lifecycleStages"]) == 7
    assert data["lifecycleStages"][3]["name"] == "Tool Execution"
    assert data["performanceBreakdown"][0]["label"] == "Tool Execution"


def test_get_runtime_metrics_keeps_recovered_tool_error_completed(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "summary.json").write_text(
        json.dumps({"session_id": "s1", "message_count": 4, "assistant_count": 2, "tool_call_count": 2}),
        encoding="utf-8",
    )
    (run / "audit.json").write_text(
        json.dumps(
            {
                "message_count": 4,
                "assistant_count": 2,
                "tool_call_count": 2,
                "tool_calls": [
                    {"index": 1, "tool_call_id": "call-1", "tool": "terminal", "status": "error"},
                    {"index": 1, "tool_call_id": "call-2", "tool": "terminal", "status": "ok"},
                ],
                "findings": [{"severity": "error", "code": "tool_error", "message": "terminal failed"}],
                "phases": [{"phase": "produce artifacts", "tool_count": 2, "tools": ["terminal"]}],
            }
        ),
        encoding="utf-8",
    )
    (run / "trajectory.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "timestamp": 1000, "content": "start"}),
                json.dumps({"role": "assistant", "timestamp": 2000, "tool_calls": [{"id": "call-1"}, {"id": "call-2"}]}),
                json.dumps({"role": "tool", "timestamp": 3000, "tool_call_id": "call-1", "content": "{\"exit_code\": 2}"}),
                json.dumps({"role": "tool", "timestamp": 4000, "tool_call_id": "call-2", "content": "{\"exit_code\": 0}"}),
                json.dumps({"role": "assistant", "timestamp": 5000, "content": "done"}),
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)

    client = TestClient(app)
    data = client.get("/api/runs/run-1/runtime-metrics").json()

    assert data["status"] == "success"
    assert data["toolMetrics"]["failed"] == 1
    assert data["duration"] == 4


def test_get_runtime_metrics_builds_llm_dashboard_from_structured_calls(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "summary.json").write_text(
        json.dumps(
            {
                "session_id": "trace-1",
                "model": "gpt-5",
                "llm_calls": [
                    {
                        "callId": "llm-1",
                        "traceId": "trace-1",
                        "name": "Planner",
                        "model": "gpt-5",
                        "provider": "OpenAI",
                        "temperature": 0.7,
                        "maxTokens": 4096,
                        "startTime": "2026-08-11T01:00:00Z",
                        "endTime": "2026-08-11T01:00:04.5Z",
                        "latency": 4.5,
                        "queueTime": 0.3,
                        "generationTime": 4.2,
                        "tokenUsage": {"inputTokens": 1200, "outputTokens": 350, "totalTokens": 1550},
                        "prompt": {
                            "system": "Plan carefully",
                            "user": "Build a PPT",
                            "context": "slides context",
                            "version": "v1.2.3",
                            "systemPromptHash": "hash-a",
                            "template": "research-agent",
                        },
                        "response": "Plan complete",
                        "status": "success",
                        "cost": 0.025,
                        "quality": {
                            "responseLength": 13,
                            "formatCompliance": 0.96,
                            "hallucinationRisk": 0.12,
                            "instructionFollowing": 0.9,
                            "selfEvaluationScore": 92,
                        },
                    },
                    {
                        "call_id": "llm-2",
                        "trace_id": "trace-1",
                        "stage": "Final Answer",
                        "model": "deepseek-chat",
                        "provider": "DeepSeek",
                        "latency_ms": 12_300,
                        "token_usage": {"input_tokens": 800, "output_tokens": 150, "total_tokens": 950},
                        "prompt": {"version": "v1.2.4", "system_prompt_hash": "hash-b", "template": "research-agent"},
                        "status": "failed",
                        "error": {"type": "JSON Parse Error", "message": "invalid json"},
                        "cost": 0.004,
                        "quality": {"format_compliance": 0.2, "hallucination_risk": 0.4, "instruction_following": 0.45},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)

    client = TestClient(app)
    data = client.get("/api/runs/run-1/runtime-metrics").json()

    summary = data["llmDashboard"]["summary"]
    assert summary["calls"] == 2
    assert summary["models"] == ["deepseek-chat", "gpt-5"]
    assert summary["inputTokens"] == 2000
    assert summary["outputTokens"] == 500
    assert summary["totalTokens"] == 2500
    assert summary["averageLatency"] == 8.4
    assert summary["maxLatency"] == 12.3
    assert summary["estimatedCost"] == 0.029
    assert summary["errorRate"] == 0.5

    timeline = data["llmDashboard"]["timeline"]
    assert timeline[0]["callId"] == "llm-1"
    assert timeline[0]["name"] == "Planner"
    assert timeline[1]["status"] == "failed"
    assert timeline[1]["error"]["type"] == "JSON Parse Error"

    detail = data["llmDashboard"]["calls"][0]
    assert detail["prompt"]["system"] == "Plan carefully"
    assert detail["prompt"]["version"] == "v1.2.3"
    assert detail["latency"]["queueTime"] == 0.3
    assert detail["latency"]["generationTime"] == 4.2

    assert data["llmDashboard"]["tokenAnalytics"]["averageInputTokens"] == 1000
    assert data["llmDashboard"]["tokenAnalytics"]["averageOutputTokens"] == 250
    assert data["llmDashboard"]["tokenAnalytics"]["maxContext"] == 1550
    assert data["llmDashboard"]["modelPerformance"][0]["model"] == "deepseek-chat"
    assert data["llmDashboard"]["modelPerformance"][1]["successRate"] == 1
    assert data["llmDashboard"]["promptVersions"][0]["version"] == "v1.2.3"
    assert data["llmDashboard"]["qualityMetrics"]["qualityScore"] == 66
    assert data["llmDashboard"]["errorAnalysis"]["jsonParseError"] == 1


def test_get_runtime_trends_summarizes_recent_runs(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "summary.json").write_text(
        json.dumps({"session_id": "s1", "message_count": 2, "assistant_count": 1, "tool_call_count": 0, "total_tokens": 1000}),
        encoding="utf-8",
    )
    (run / "trajectory.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "timestamp": 1000, "content": "start"}),
                json.dumps({"role": "assistant", "timestamp": 6000, "content": "done"}),
            ]
        ),
        encoding="utf-8",
    )
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)

    client = TestClient(app)
    data = client.get("/api/runtime-trends").json()

    assert data["sampleSize"] == 1
    assert data["successRate"] == 1
    assert data["averageDuration"] == 5
    assert data["averageTokens"] == 1000


def test_get_batch_summary(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"

    success = runs_dir / "success-run"
    success.mkdir(parents=True)
    (success / "summary.json").write_text(
        json.dumps({"session_id": "s1", "model": "m1", "tool_call_count": 4}),
        encoding="utf-8",
    )
    (success / "audit.json").write_text(
        json.dumps(
            {
                "tool_call_count": 4,
                "tool_calls": [
                    {"tool": "read_file", "status": "ok"},
                    {"tool": "terminal", "status": "ok"},
                    {"tool": "write_file", "status": "ok"},
                    {"tool": "patch", "status": "ok"},
                ],
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    (success / "trajectory.jsonl").write_text("", encoding="utf-8")
    (success / "agent_judge.json").write_text(
        json.dumps({"overall_status": "success", "completion_score": 92, "findings": []}),
        encoding="utf-8",
    )

    failed = runs_dir / "failed-run"
    failed.mkdir(parents=True)
    (failed / "summary.json").write_text(
        json.dumps({"session_id": "s2", "model": "m1", "tool_call_count": 2}),
        encoding="utf-8",
    )
    (failed / "audit.json").write_text(
        json.dumps(
            {
                "tool_call_count": 2,
                "tool_calls": [
                    {"tool": "terminal", "status": "error"},
                    {"tool": "read_file", "status": "missing"},
                ],
                "findings": [{"severity": "error", "code": "tool_error", "message": "tool failed"}],
            }
        ),
        encoding="utf-8",
    )
    (failed / "trajectory.jsonl").write_text("", encoding="utf-8")
    (failed / "agent_judge.json").write_text(
        json.dumps(
            {
                "overall_status": "failed",
                "completion_score": 35,
                "findings": [
                    {"category": "tool", "title": "Dependency wall"},
                    {"category": "tool", "title": "Dependency wall"},
                ],
            }
        ),
        encoding="utf-8",
    )

    settings = Settings(runs_dir=runs_dir)
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)
    client = TestClient(app)

    data = client.get("/api/runs/batch-summary").json()

    assert data["total_runs"] == 2
    assert data["successful_runs"] == 1
    assert data["success_rate"] == 0.5
    assert data["tool_error_rate"] == 2 / 6
    assert data["average_tool_calls"] == 3
    assert data["root_causes"][0] == {"label": "tool: Dependency wall", "count": 2}
    assert data["runs"][0]["run_id"] == "failed-run"
    assert data["runs"][0]["failure_root_cause"] == "tool: Dependency wall"


def test_import_remote_session_writes_run(tmp_path, monkeypatch):
    runs_dir = tmp_path / "runs"
    settings = Settings(runs_dir=runs_dir)
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)

    class FakeAgentClient:
        def __init__(self, base_url, user_id, token=None, timeout=60.0):
            assert base_url == "http://agent.example"
            assert user_id == "u1"
            assert token == "secret"
            assert timeout == 12

        def fetch_session(self, session_id):
            assert session_id == "s1"
            return {
                "session": {"session_id": "s1", "title": "remote", "model": "m1"},
                "messages": [{"role": "assistant", "content": "done"}],
            }

    monkeypatch.setattr("backend.app.routes.runs.AgentClient", FakeAgentClient)
    client = TestClient(app)

    data = client.post(
        "/api/runs/import-session",
        json={
            "base_url": "http://agent.example",
            "user_id": "u1",
            "session_id": "s1",
            "token": "secret",
            "timeout": 12,
        },
    ).json()

    assert data["run"]["has_summary"] is True
    assert data["run"]["has_audit"] is True
    assert data["run"]["server_host"] == "agent.example"
    run_dir = runs_dir / data["run"]["name"]
    assert (run_dir / "session.json").exists()
    assert (run_dir / "source.json").exists()
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["session_id"] == "s1"
    assert summary["server_host"] == "agent.example"
    source = json.loads((run_dir / "source.json").read_text(encoding="utf-8"))
    assert source["source_base_url"] == "http://agent.example"


def test_list_runs_includes_server_host(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "source.json").write_text(
        json.dumps(
            {
                "source_base_url": "http://192.168.1.238:8788",
                "server_host": "192.168.1.238",
                "server_netloc": "192.168.1.238:8788",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: Settings(runs_dir=tmp_path / "runs"))
    client = TestClient(app)
    data = client.get("/api/runs").json()
    assert data["runs"][0]["server_host"] == "192.168.1.238"
    assert data["runs"][0]["source_base_url"] == "http://192.168.1.238:8788"


def test_delete_run(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)
    client = TestClient(app)

    response = client.delete("/api/runs/run-1")
    assert response.status_code == 200
    assert response.json()["deleted"] == "run-1"
    assert response.json()["runs"] == []
    assert not run.exists()
    assert client.delete("/api/runs/run-1").status_code == 404


def test_backend_judge_includes_acceptance_criteria(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "expectations.json").write_text(
        json.dumps({"acceptance_criteria": "必须调用 ppt-master"}, ensure_ascii=False),
        encoding="utf-8",
    )
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.judge.get_settings", lambda: settings)

    async def fake_invoke(messages, config=None):
        assert "Agent Judge acceptance criteria" in messages[0]["content"]
        assert "必须调用 ppt-master" in messages[0]["content"]
        return {"overall_status": "success", "completion_score": 88, "confidence": "high", "summary": "ok"}

    monkeypatch.setattr("agentloop.agents.agent.invoke_agent", fake_invoke)
    client = TestClient(app)

    response = client.post("/api/runs/run-1/judge")
    assert response.status_code == 200
    assert response.json()["report"]["completion_score"] == 88
