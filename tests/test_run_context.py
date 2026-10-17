"""Phase 2 run linkage: /api/runs/{id}/context, suite-job runs & attempts."""

import json

from fastapi.testclient import TestClient

from auditrail import testsuites as suite_mod
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path):
    return Settings(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
    )


def _client(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    monkeypatch.setattr("backend.app.routes.platform.get_settings", lambda: settings)
    return TestClient(app), settings


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _register_agent(client):
    response = client.post(
        "/api/agents",
        json={
            "agent_id": "agent-001",
            "name": "Demo Agent",
            "adapter": "http_session",
            "endpoint": "http://agent.internal",
            "capabilities": ["session"],
            "environment": "staging",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["agent"]


def _make_run(runs_dir, run_id, *, agent_id="agent-001", suite_ref=None, with_run_json=True):
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if with_run_json:
        _write(
            run_dir / "run.json",
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": "completed",
                "status_reason": None,
                "started_at": "2026-01-05T10:00:00Z",
                "finished_at": "2026-01-05T10:00:02Z",
                "agent_ref": {"id": agent_id, "name": "Demo Agent", "environment": "staging"} if agent_id else None,
                "test_case_ref": {"id": "case-001", "case_id": "case-001", "version": 1},
                "suite_ref": suite_ref,
                "source_ref": {"system": "platform", "source_id": None},
                "artifact_manifest": [],
            },
        )
    return run_dir


def _make_job(settings, job_id="job-1", suite_id="suite-1", attempts=None):
    if attempts is None:
        attempts = [
            {
                "attempt_id": "attempt-1-aaa",
                "case_execution_id": "job-1-case-1",
                "order": 0,
                "case_id": "case-001",
                "case_title": "样例",
                "case_version": 1,
                "retry_index": 0,
                "status": "failed",
                "run_id": "run-a0",
                "status_reason": "agent_unreachable",
                "started_at": "2026-01-05T10:00:00Z",
                "finished_at": "2026-01-05T10:01:00Z",
            },
            {
                "attempt_id": "attempt-1-r1-bbb",
                "case_execution_id": "job-1-case-1",
                "order": 0,
                "case_id": "case-001",
                "case_title": "样例",
                "case_version": 1,
                "retry_index": 1,
                "status": "completed",
                "run_id": "run-a1",
                "status_reason": None,
                "started_at": "2026-01-05T10:02:00Z",
                "finished_at": "2026-01-05T10:03:00Z",
            },
        ]
    _write(
        settings.platform_dir / "suite_jobs" / ("%s.json" % job_id),
        {
            "schema_version": 1,
            "job_id": job_id,
            "status": "completed",
            "judge": False,
            "suite_ref": {"id": suite_id, "version": 2, "title": "回归集"},
            "agent_ref": {"id": "agent-001", "name": "Demo Agent", "environment": "staging"},
            "attempts": attempts,
        },
    )


def _suite_ref(job_id="job-1", attempt_id="attempt-1-r1-bbb", retry_index=1):
    return {
        "id": "suite-1",
        "suite_id": "suite-1",
        "version": 2,
        "suite_version": 2,
        "job_id": job_id,
        "case_execution_id": "job-1-case-1",
        "attempt_id": attempt_id,
        "retry_index": retry_index,
    }


def test_run_context_full_linkage(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_job(settings)
    _make_run(settings.runs_dir, "run-a0", suite_ref=_suite_ref(attempt_id="attempt-1-aaa", retry_index=0))
    _make_run(settings.runs_dir, "run-a1", suite_ref=_suite_ref())

    body = client.get("/api/runs/run-a1/context").json()
    assert body["run"]["run_id"] == "run-a1"
    assert body["agent"]["id"] == "agent-001"
    assert body["agent"]["config_status"] == "registered"
    assert body["agent"]["environment"] == "staging"
    assert body["suite"]["suite_id"] == "suite-1"
    assert body["suite"]["job_id"] == "job-1"
    assert body["suite"]["job_status"] == "completed"
    assert body["attempt"]["attempt_id"] == "attempt-1-r1-bbb"
    assert body["attempt"]["retry_index"] == 1
    assert body["attempt"]["is_final_attempt"] is True
    assert body["case_execution"]["case_execution_id"] == "job-1-case-1"
    assert body["case_execution"]["attempt_count"] == 2
    related = body["related_runs"]
    assert [item["run_id"] for item in related] == ["run-a0"]
    assert related[0]["retry_index"] == 0
    assert body["warnings"] == []


def test_run_context_legacy_run_without_run_json(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    run_dir = settings.runs_dir / "run-legacy"
    run_dir.mkdir(parents=True)
    _write(run_dir / "summary.json", {"session_id": "s1"})
    body = client.get("/api/runs/run-legacy/context").json()
    # no fabricated suite/agent linkage; the legacy state is explicit
    assert body["run"]["legacy_inferred"] is True
    assert body["suite"]["job_id"] is None
    assert body["attempt"] is None
    assert body["case_execution"] is None
    assert body["related_runs"] == []
    assert any(w["code"] == "legacy_run" for w in body["warnings"])


def test_run_context_missing_linkage_not_failed(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-solo")
    body = client.get("/api/runs/run-solo/context").json()
    assert body["run"]["status"] == "completed"  # missing linkage is not an execution error
    assert body["suite"]["job_id"] is None
    assert body["warnings"] == []


def test_run_context_warns_on_missing_job(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-orphan", suite_ref=_suite_ref(job_id="job-gone"))
    body = client.get("/api/runs/run-orphan/context").json()
    assert body["suite"]["job_id"] == "job-gone"
    assert any(w["code"] == "suite_job_not_found" for w in body["warnings"])


def test_run_context_warns_on_attempt_run_mismatch(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_job(settings)
    # run.json claims attempt-1-aaa, but the job binds that attempt to run-a0
    _make_run(settings.runs_dir, "run-other", suite_ref=_suite_ref(attempt_id="attempt-1-aaa", retry_index=0))
    body = client.get("/api/runs/run-other/context").json()
    assert any(w["code"] == "attempt_run_mismatch" for w in body["warnings"])
    # run.json stays authoritative for the displayed linkage
    assert body["attempt"]["attempt_id"] == "attempt-1-aaa"


def test_run_context_deleted_agent_marked_missing_configuration(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_run(settings.runs_dir, "run-x")
    client.delete("/api/agents/agent-001")
    body = client.get("/api/runs/run-x/context").json()
    assert body["agent"]["config_status"] == "missing_configuration"
    assert body["agent"]["id"] == "agent-001"


def test_run_context_404_and_400(monkeypatch, tmp_path):
    client, _ = _client(monkeypatch, tmp_path)
    assert client.get("/api/runs/nope/context").status_code == 404
    assert client.get("/api/runs/..%2Fsecret/context").status_code in {400, 404, 422}


def test_suite_job_runs_groups_case_executions(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_job(settings)
    _make_run(settings.runs_dir, "run-a0", suite_ref=_suite_ref(attempt_id="attempt-1-aaa", retry_index=0))
    _make_run(settings.runs_dir, "run-a1", suite_ref=_suite_ref())

    body = client.get("/api/suite-jobs/job-1/runs").json()
    assert body["job_id"] == "job-1"
    assert body["agent"]["id"] == "agent-001"
    assert body["agent"]["environment"] == "staging"
    execution = body["case_executions"][0]
    assert execution["case_execution_id"] == "job-1-case-1"
    assert execution["attempt_count"] == 2
    assert execution["final_status"] == "completed"
    runs = {item["run_id"]: item for item in body["runs"]}
    assert runs["run-a1"]["retry_index"] == 1
    assert runs["run-a1"]["run"]["status"] == "completed"
    assert body["warnings"] == []


def test_suite_job_runs_legacy_job_without_agent_ref(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _write(
        settings.platform_dir / "suite_jobs" / "job-old.json",
        {
            "schema_version": 1,
            "job_id": "job-old",
            "status": "completed",
            "suite_ref": {"id": "suite-1", "version": 1},
            "attempts": [],
        },
    )
    body = client.get("/api/suite-jobs/job-old/runs").json()
    # legacy job: agent is unknown, never fabricated
    assert body["agent"]["id"] is None
    assert body["agent"]["config_status"] == "unknown"


def test_suite_job_attempt_history(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    _make_job(settings)
    _make_run(settings.runs_dir, "run-a0", suite_ref=_suite_ref(attempt_id="attempt-1-aaa", retry_index=0))
    _make_run(settings.runs_dir, "run-a1", suite_ref=_suite_ref())

    body = client.get("/api/suite-jobs/job-1/attempts/attempt-1-r1-bbb").json()
    assert body["attempt"]["is_final_attempt"] is True
    assert body["attempt"]["case_execution_id"] == "job-1-case-1"
    history = body["attempt_history"]
    assert [item["retry_index"] for item in history] == [0, 1]
    assert history[0]["is_final_attempt"] is False
    assert history[1]["is_final_attempt"] is True
    assert history[0]["run"]["run_id"] == "run-a0"

    # retry attempt resolves back to the same CaseExecution history
    first = client.get("/api/suite-jobs/job-1/attempts/attempt-1-aaa").json()
    assert first["attempt"]["is_final_attempt"] is False
    assert len(first["attempt_history"]) == 2

    assert client.get("/api/suite-jobs/job-1/attempts/nope").status_code == 404
    assert client.get("/api/suite-jobs/nope/attempts/attempt-1-aaa").status_code == 404


def test_case_execution_id_derivation_for_legacy_jobs(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _register_agent(client)
    # legacy attempt without case_execution_id: derived deterministically
    _make_job(
        settings,
        attempts=[
            {
                "attempt_id": "attempt-1-aaa",
                "order": 0,
                "case_id": "case-001",
                "retry_index": 0,
                "status": "completed",
                "run_id": "run-a0",
            }
        ],
    )
    _make_run(
        settings.runs_dir,
        "run-a0",
        suite_ref={"id": "suite-1", "version": 2, "job_id": "job-1", "attempt_id": "attempt-1-aaa"},
    )
    body = client.get("/api/suite-jobs/job-1/attempts/attempt-1-aaa").json()
    assert body["attempt"]["case_execution_id"] == suite_mod.case_execution_id_for("job-1", 0)
