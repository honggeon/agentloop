"""Agent detail workspace API: aggregation, compat, filters, fault isolation."""

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path):
    return Settings(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
    )


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr("backend.app.routes.platform.get_settings", lambda: settings)
    return settings


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _make_run(
    runs_dir,
    run_id,
    *,
    agent_id="agent-001",
    status="completed",
    started_at="2026-01-05T10:00:00Z",
    finished_at="2026-01-05T10:00:02Z",
    case_id="case-001",
    suite_ref=None,
    source_host="agent.internal",
    audit=None,
    judge=None,
    llm_calls=None,
    with_run_json=True,
    with_trace=True,
):
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if with_run_json:
        record = {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "status_reason": None,
            "started_at": started_at,
            "finished_at": finished_at,
            "agent_ref": {"id": agent_id, "version": "", "config_hash": "h"} if agent_id else None,
            "test_case_ref": {"id": case_id, "version": 1} if case_id else None,
            "suite_ref": suite_ref,
            "experiment_ref": None,
            "prompt_ref": None,
            "model_ref": None,
            "source_ref": {"system": "platform", "source_id": None},
            "artifact_manifest": [],
        }
        _write(run_dir / "run.json", record)
    if source_host:
        _write(run_dir / "source.json", {"source_base_url": "http://%s" % source_host, "server_host": source_host})
    if with_trace:
        (run_dir / "trajectory.jsonl").write_text('{"index": 0, "role": "user"}\n', encoding="utf-8")
    if audit is not None:
        _write(run_dir / "audit.json", audit)
    if judge is not None:
        _write(run_dir / "agent_judge.json", judge)
    if llm_calls is not None:
        _write(run_dir / "llm_calls.json", {"llm_calls": llm_calls})
    return run_dir


def _register_agent(client, **overrides):
    payload = {
        "agent_id": "agent-001",
        "name": "Demo Agent",
        "adapter": "http_session",
        "endpoint": "http://agent.internal",
        "capabilities": ["session"],
        "metadata": {"environment": "local"},
    }
    payload.update(overrides)
    response = client.post("/api/agents", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["agent"]


def _client(monkeypatch, tmp_path):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    return TestClient(app), settings


# ---------------------------------------------------------------------------
# /api/agents compatibility
# ---------------------------------------------------------------------------


def test_list_agents_preserves_config_semantics(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    stored = _register_agent(client)

    response = client.get("/api/agents")
    assert response.status_code == 200
    body = response.json()
    agent = body["agents"][0]
    # original fields untouched (endpoint is SSRF-normalized at creation)
    for key in ("agent_id", "name", "version", "adapter", "endpoint", "capabilities", "metadata", "config_hash"):
        assert agent[key] == stored[key]
    # additive keys
    assert agent["derived"] is False
    assert agent["stats"]["run_count"] == 0
    assert body["derived_agents"] == []


def test_create_agent_still_works(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    response = client.post(
        "/api/agents",
        json={"agent_id": "a2", "endpoint": "http://agent.internal", "adapter": "http_session"},
    )
    assert response.status_code == 201
    assert response.json()["agent"]["agent_id"] == "a2"


def test_derived_agents_appear_separately(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _make_run(settings.runs_dir, "run-x1", agent_id="ghost-agent")

    body = client.get("/api/agents").json()
    assert body["agents"] == []
    derived = body["derived_agents"]
    assert [item["agent_id"] for item in derived] == ["ghost-agent"]
    assert derived[0]["derived"] is True
    assert derived[0]["stats"]["run_count"] == 1


# ---------------------------------------------------------------------------
# agent detail
# ---------------------------------------------------------------------------


def test_agent_detail_aggregates_from_runs(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-a", agent_id="agent-001")

    response = client.get("/api/agents/agent-001")
    assert response.status_code == 200
    body = response.json()
    assert body["agent"]["agent_id"] == "agent-001"
    assert body["agent"]["name"] == "Demo Agent"
    assert body["agent"]["type"] == "http_session"
    assert body["agent"]["environment"] == "local"
    assert body["agent"]["derived"] is False
    assert body["agent"]["last_run_status"] == "completed"
    assert body["capabilities"] == ["session"]
    assert body["run_count"] == 1


def test_agent_detail_derived_from_runs_without_config(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _make_run(settings.runs_dir, "run-b", agent_id="ghost")

    body = client.get("/api/agents/ghost").json()
    assert body["agent"]["derived"] is True
    assert body["agent"]["name"] == "ghost"  # never the source host
    assert body["config"] is None


def test_agent_detail_404_for_unknown(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.get("/api/agents/nope").status_code == 404


def test_invalid_agent_id_rejected(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    response = client.get("/api/agents/..%2F..%2Fetc")
    assert response.status_code in (400, 404, 422)
    if response.status_code == 400:
        assert response.json()["detail"]["code"] == "invalid_agent_id"


def test_legacy_run_without_run_json_aggregated_via_source_host(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(
        settings.runs_dir,
        "run-legacy",
        agent_id=None,
        with_run_json=False,
        source_host="agent.internal",
        audit={"tool_call_count": 1, "tool_calls": [{"tool": "search", "status": "ok"}], "findings": []},
    )

    overview = client.get("/api/agents/agent-001/overview?range=all").json()
    assert overview["kpis"]["run_count"] == 1
    run_row = client.get("/api/agents/agent-001/runs?range=all").json()["items"][0]
    assert run_row["run_id"] == "run-legacy"
    assert run_row["status"] == "completed"  # legacy_inferred


def test_source_host_never_used_as_agent_name(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-legacy2", agent_id=None, with_run_json=False, source_host="agent.internal")
    body = client.get("/api/agents/agent-001").json()
    assert body["agent"]["name"] == "Demo Agent"


def test_host_derived_agent_for_unattributed_legacy_runs(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    # no configured agent claims this host
    _make_run(settings.runs_dir, "run-h1", agent_id=None, with_run_json=False, source_host="legacy.internal")
    _make_run(settings.runs_dir, "run-h2", agent_id=None, with_run_json=False, source_host="legacy.internal")

    body = client.get("/api/agents").json()
    derived = {item["agent_id"]: item for item in body["derived_agents"]}
    assert "host:legacy.internal" in derived
    assert derived["host:legacy.internal"]["derived"] is True
    assert derived["host:legacy.internal"]["stats"]["run_count"] == 2

    detail = client.get("/api/agents/host:legacy.internal").json()
    assert detail["agent"]["derived"] is True
    assert detail["agent"]["name"] == "legacy.internal"
    assert detail["agent"]["source_host"] == "legacy.internal"
    assert detail["run_count"] == 2
    runs = client.get("/api/agents/host:legacy.internal/runs?range=all").json()
    assert runs["total"] == 2


def test_host_derived_agent_skips_host_claimed_by_config(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)  # endpoint host agent.internal
    _make_run(settings.runs_dir, "run-claimed", agent_id=None, with_run_json=False, source_host="agent.internal")

    body = client.get("/api/agents").json()
    assert all(item["agent_id"] != "host:agent.internal" for item in body["derived_agents"])
    assert body["agents"][0]["stats"]["run_count"] == 1


# ---------------------------------------------------------------------------
# overview
# ---------------------------------------------------------------------------


def test_overview_empty_state_without_runs(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _register_agent(client)

    body = client.get("/api/agents/agent-001/overview").json()
    assert body["agent"]["agent_id"] == "agent-001"
    assert body["models"] == []
    assert body["tools"] == []
    assert body["traces"] == []
    assert body["recent_runs"] == []
    assert body["kpis"]["run_count"] == 0
    assert body["kpis"]["completion_score"] is None
    assert body["kpis"]["total_cost"] is None
    assert body["kpis"]["judge_evidence_coverage"] is None
    for metric in body["health"].values():
        assert metric["status"] == "unknown"
        assert metric["label"] == "未评估"
        assert metric["source"] == "missing"


def test_overview_full_aggregation(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(
        settings.runs_dir,
        "run-full",
        agent_id="agent-001",
        audit={
            "tool_call_count": 2,
            "tool_calls": [
                {"tool": "search", "status": "ok"},
                {"tool": "shell", "status": "error"},
            ],
            "findings": [
                {"severity": "error", "code": "tool_error", "message": "boom"},
                {"severity": "warning", "code": "dangerous_tool_used", "message": "Potentially sensitive tool used: shell"},
            ],
        },
        judge={
            "overall_status": "success",
            "completion_score": 92,
            "dimensions": {"task_completion": {"score": 92, "status": "pass", "source": "judge", "evidence": [{"source": "trajectory.jsonl", "quote": "q"}]}},
            "judge_reliability": {"total_evidence_count": 4, "valid_evidence_count": 3},
        },
        llm_calls=[
            {
                "model": "deepseek-chat",
                "provider": "deepseek",
                "tokenUsage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
                "latency": 1.5,
                "status": "success",
            },
            {
                "model": "deepseek-chat",
                "provider": "deepseek",
                "tokenUsage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
                "latency": 0.5,
                "status": "failed",
                "cost": 0.001,
            },
        ],
    )

    body = client.get("/api/agents/agent-001/overview?range=all").json()
    assert body["data_range"] == "all"

    model = body["models"][0]
    assert model["model"] == "deepseek-chat"
    assert model["llm_call_count"] == 2
    assert model["input_tokens"] == 110
    assert model["output_tokens"] == 55
    assert model["total_tokens"] == 165
    assert model["average_latency_ms"] == 1000.0
    assert model["error_count"] == 1
    # one observed cost; no price table configured -> no estimation, the
    # aggregate stays observed and never invents a number for the second call
    assert model["cost_source"] == "observed"
    assert model["cost"] == 0.001

    tools = {item["tool_name"]: item for item in body["tools"]}
    assert tools["search"]["call_count"] == 1
    assert tools["search"]["error_count"] == 0
    assert tools["shell"]["error_count"] == 1
    assert tools["shell"]["error_rate"] == 1.0
    assert tools["shell"]["dangerous_count"] == 1
    assert tools["shell"]["audit_source"] == "deterministic"

    kpis = body["kpis"]
    assert kpis["run_count"] == 1
    assert kpis["trace_count"] == 1
    assert kpis["error_rate"] == 0.0  # run completed; tool errors do not leak into run failure rate
    assert kpis["completion_score"] == 92
    assert kpis["token_count"] == 165
    assert kpis["total_cost"] == 0.001
    assert kpis["total_cost_source"] == "observed"
    assert kpis["judge_evidence_coverage"] == 0.75

    health = body["health"]
    assert health["task_completion"]["status"] == "pass"
    assert health["task_completion"]["source"] == "observed"
    assert health["tool_stability"]["value"] == 50.0
    assert health["high_severity_finding_count"]["value"] == 1
    assert health["high_severity_finding_count"]["status"] == "fail"

    trace = body["traces"][0]
    assert trace["date"] == "2026-01-05"
    assert trace["run_count"] == 1
    assert trace["completed_count"] == 1
    assert trace["error_count"] == 0


def test_tool_error_rate_differs_from_run_error_rate(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    # completed run whose only tool call errored
    _make_run(
        settings.runs_dir,
        "run-te",
        agent_id="agent-001",
        audit={"tool_call_count": 1, "tool_calls": [{"tool": "search", "status": "error"}], "findings": []},
    )
    body = client.get("/api/agents/agent-001/overview?range=all").json()
    assert body["tools"][0]["error_rate"] == 1.0
    assert body["kpis"]["error_rate"] == 0.0


def test_missing_judge_yields_null_score_and_unknown_health(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-nojudge", agent_id="agent-001")

    body = client.get("/api/agents/agent-001/overview?range=all").json()
    assert body["kpis"]["completion_score"] is None
    assert body["kpis"]["judge_evidence_coverage"] is None
    assert body["health"]["task_completion"]["status"] == "unknown"
    row = client.get("/api/agents/agent-001/runs?range=all").json()["items"][0]
    assert row["judge_overall_status"] is None
    assert row["completion_score"] is None


def test_missing_evaluation_json_does_not_crash(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-noeval", agent_id="agent-001")
    assert client.get("/api/agents/agent-001/overview?range=all").status_code == 200


def test_unknown_judge_status_not_counted_as_failure(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(
        settings.runs_dir,
        "run-unknown",
        agent_id="agent-001",
        judge={"overall_status": "unknown", "completion_score": None, "dimensions": {}, "judge_reliability": {}},
    )
    body = client.get("/api/agents/agent-001/overview?range=all").json()
    assert body["kpis"]["error_rate"] == 0.0
    row = client.get("/api/agents/agent-001/runs?range=all").json()["items"][0]
    assert row["judge_overall_status"] == "unknown"


def test_estimated_cost_requires_price_table(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(
        settings.runs_dir,
        "run-cost",
        agent_id="agent-001",
        llm_calls=[{"model": "gpt-4o", "tokenUsage": {"inputTokens": 1000, "outputTokens": 500, "totalTokens": 1500}}],
    )
    # No price table configured: no estimate is invented.
    model = client.get("/api/agents/agent-001/overview?range=all").json()["models"][0]
    assert model["cost_source"] == "missing"
    assert model["cost"] is None

    # With a confirmed price the estimate is produced and stays "estimated".
    _write(
        settings.platform_dir / "price_table.json",
        {
            "schema_version": 1,
            "price_version": "2026-01",
            "models": {
                "gpt-4o": {
                    "currency": "USD",
                    "input_cost_per_1m_tokens": 2.5,
                    "output_cost_per_1m_tokens": 10.0,
                    "effective_at": "2026-01-01T00:00:00Z",
                }
            },
        },
    )
    model = client.get("/api/agents/agent-001/overview?range=all").json()["models"][0]
    assert model["cost_source"] == "estimated"
    assert model["cost"] is not None and model["cost"] > 0


def test_cost_missing_without_llm_data(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-nollm", agent_id="agent-001")
    body = client.get("/api/agents/agent-001/overview?range=all").json()
    assert body["models"] == []
    assert body["kpis"]["total_cost"] is None
    assert body["kpis"]["total_cost_source"] == "missing"


# ---------------------------------------------------------------------------
# ranges & status filters
# ---------------------------------------------------------------------------


def _seed_dated_runs(settings):
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    old = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")
    very_old = "2020-01-01T00:00:00Z"
    _make_run(settings.runs_dir, "run-recent", agent_id="agent-001", started_at=recent, finished_at=recent)
    _make_run(settings.runs_dir, "run-mid", agent_id="agent-001", started_at=old, finished_at=old)
    _make_run(settings.runs_dir, "run-old", agent_id="agent-001", started_at=very_old, finished_at="2020-01-01T00:00:01Z")


def test_range_filters(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _seed_dated_runs(settings)

    assert client.get("/api/agents/agent-001/runs?range=7d").json()["total"] == 1
    assert client.get("/api/agents/agent-001/runs?range=30d").json()["total"] == 2
    assert client.get("/api/agents/agent-001/runs?range=all").json()["total"] == 3
    overview = client.get("/api/agents/agent-001/overview?range=all").json()
    assert len(overview["traces"]) == 3
    overview_7d = client.get("/api/agents/agent-001/overview?range=7d").json()
    assert len(overview_7d["traces"]) == 1


def test_invalid_range_rejected(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    _register_agent(client)
    assert client.get("/api/agents/agent-001/runs?range=bogus").status_code == 400
    assert client.get("/api/agents/agent-001/overview?range=bogus").status_code == 400


def test_status_filter(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-ok", agent_id="agent-001", status="completed")
    _make_run(settings.runs_dir, "run-bad", agent_id="agent-001", status="failed")
    _make_run(settings.runs_dir, "run-timeout", agent_id="agent-001", status="timed_out")
    _make_run(settings.runs_dir, "run-stop", agent_id="agent-001", status="cancelled")

    assert client.get("/api/agents/agent-001/runs?range=all&status=failed").json()["total"] == 1
    assert client.get("/api/agents/agent-001/runs?range=all&status=timed_out").json()["total"] == 1
    assert client.get("/api/agents/agent-001/runs?range=all&status=cancelled").json()["total"] == 1
    assert client.get("/api/agents/agent-001/runs?range=all&status=all").json()["total"] == 4
    assert client.get("/api/agents/agent-001/runs?range=all&status=bogus").status_code == 400


def test_stale_status_filter(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    # queued run nobody executes in this process -> stale
    _make_run(settings.runs_dir, "run-stale", agent_id="agent-001", status="queued", started_at=None, finished_at=None)
    body = client.get("/api/agents/agent-001/runs?range=all&status=stale").json()
    assert body["total"] == 1
    assert body["items"][0]["run_id"] == "run-stale"


def test_runs_pagination(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    for index in range(5):
        _make_run(settings.runs_dir, "run-p%d" % index, agent_id="agent-001")
    page1 = client.get("/api/agents/agent-001/runs?range=all&limit=2&offset=0").json()
    page2 = client.get("/api/agents/agent-001/runs?range=all&limit=2&offset=2").json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 2
    assert page1["items"][0]["run_id"] != page2["items"][0]["run_id"]


# ---------------------------------------------------------------------------
# Phase 1.1 linkage
# ---------------------------------------------------------------------------


def test_runs_expose_job_attempt_retry_linkage(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    suite_ref = {"id": "suite-1", "version": 1, "job_id": "job-1", "attempt_id": "attempt-1-r2-abc"}
    _make_run(settings.runs_dir, "run-retry", agent_id="agent-001", suite_ref=suite_ref)
    _write(
        settings.platform_dir / "suite_jobs" / "job-1.json",
        {
            "schema_version": 1,
            "job_id": "job-1",
            "status": "completed",
            "attempts": [{"attempt_id": "attempt-1-r2-abc", "retry_index": 2, "run_id": "run-retry", "status": "completed"}],
        },
    )

    row = client.get("/api/agents/agent-001/runs?range=all").json()["items"][0]
    assert row["job_id"] == "job-1"
    assert row["attempt_id"] == "attempt-1-r2-abc"
    assert row["retry_index"] == 2


def test_multiple_retry_runs_all_visible(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    for index in range(3):
        suite_ref = {"id": "suite-1", "version": 1, "job_id": "job-1", "attempt_id": "attempt-%d" % index}
        _make_run(settings.runs_dir, "run-r%d" % index, agent_id="agent-001", case_id="case-001", suite_ref=suite_ref)
    body = client.get("/api/agents/agent-001/runs?range=all").json()
    assert body["total"] == 3
    assert {item["case_id"] for item in body["items"]} == {"case-001"}


# ---------------------------------------------------------------------------
# fault isolation & leakage
# ---------------------------------------------------------------------------


def test_single_broken_run_does_not_break_aggregation(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-good", agent_id="agent-001")
    broken = settings.runs_dir / "run-broken"
    broken.mkdir(parents=True)
    (broken / "run.json").write_text("{not json", encoding="utf-8")
    (broken / "audit.json").write_text("{also not json", encoding="utf-8")

    body = client.get("/api/agents/agent-001/overview?range=all").json()
    good = [row for row in body["recent_runs"] if row["run_id"] == "run-good"]
    assert len(good) == 1
    # broken run is not silently fabricated into healthy data
    assert client.get("/api/agents/agent-001/runs?range=all").status_code == 200


def test_no_secrets_or_absolute_paths_leaked(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(
        settings.runs_dir,
        "run-leak",
        agent_id="agent-001",
        judge={"overall_status": "success", "completion_score": 80, "dimensions": {}, "judge_reliability": {}},
    )
    text = client.get("/api/agents/agent-001/overview?range=all").text
    assert str(tmp_path) not in text
    assert "sk-" not in text
    assert "Bearer" not in text
    text = client.get("/api/agents/agent-001/runs?range=all").text
    assert str(tmp_path) not in text


def test_run_row_shape(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-shape", agent_id="agent-001")
    row = client.get("/api/agents/agent-001/runs?range=all").json()["items"][0]
    for key in (
        "run_id",
        "agent_id",
        "case_id",
        "job_id",
        "attempt_id",
        "status",
        "judge_overall_status",
        "completion_score",
        "started_at",
        "finished_at",
        "duration_ms",
        "tool_error_count",
        "deterministic_finding_count",
        "has_evidence",
    ):
        assert key in row
    assert row["duration_ms"] == 2000
    assert not any(key.startswith("_") for key in row)
