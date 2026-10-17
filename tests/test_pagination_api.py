"""Phase 4 pagination governance: unified limit/offset, server caps, sort
whitelists, has_more, no unbounded responses."""

import json

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path, **overrides):
    base = dict(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _client(monkeypatch, settings):
    for module in (
        "backend.app.routes.platform",
        "backend.app.routes.integrations",
        "backend.app.routes.admin",
    ):
        monkeypatch.setattr("%s.get_settings" % module, lambda: settings)
    return TestClient(app)


def _write_run(settings, run_id, *, agent_id="agent-1", status="completed", started="2026-01-05T10:00:00Z"):
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_id,
                "status": status,
                "status_reason": None,
                "started_at": started,
                "finished_at": started,
                "agent_ref": {"id": agent_id},
                "test_case_ref": None,
                "suite_ref": None,
                "artifact_manifest": [],
            }
        ),
        encoding="utf-8",
    )


def _make_agent(client):
    response = client.post(
        "/api/agents",
        json={
            "agent_id": "agent-1",
            "name": "Agent One",
            "endpoint": "http://agent.internal:80",
            "capabilities": ["session"],
        },
    )
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# agent runs
# ---------------------------------------------------------------------------


def test_agent_runs_pagination_and_sort_whitelist(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _make_agent(client)
    for index in range(5):
        _write_run(settings, "run-%d" % index, started="2026-01-0%dT10:00:00Z" % (index + 1))

    page1 = client.get("/api/agents/agent-1/runs?range=all&limit=2&offset=0")
    assert page1.status_code == 200, page1.text
    body = page1.json()
    assert body["total"] == 5
    assert body["has_more"] is True
    assert len(body["items"]) == 2
    page3 = client.get("/api/agents/agent-1/runs?range=all&limit=2&offset=4")
    assert page3.json()["has_more"] is False
    assert len(page3.json()["items"]) == 1

    # server-side limit cap
    capped = client.get("/api/agents/agent-1/runs?range=all&limit=100000")
    assert capped.json()["limit"] == 200

    # sort whitelist: valid field ok, unknown field 400
    sorted_runs = client.get("/api/agents/agent-1/runs?range=all&sort=run_id")
    assert sorted_runs.status_code == 200
    ids = [item["run_id"] for item in sorted_runs.json()["items"]]
    assert ids == sorted(ids)
    bad = client.get("/api/agents/agent-1/runs?range=all&sort=;DROP")
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# agent external evaluations
# ---------------------------------------------------------------------------


def test_agent_external_evaluations_paginated(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _make_agent(client)
    # seed external evaluations directly in the store
    from auditrail.integrations import external_models as ext

    records = [
        ext.normalize_external_evaluation(
            {"source_id": "s-%02d" % index, "agent_id": "agent-1", "metric_name": "quality", "value": 0.5},
            source_system="langfuse",
        )
        for index in range(5)
    ]
    ext.save_external_evaluations(settings.platform_dir, records)

    page = client.get("/api/agents/agent-1/external-evaluations?limit=2&offset=0")
    assert page.status_code == 200
    body = page.json()
    assert len(body["external_evaluations"]) == 2
    assert body["pagination"]["total"] == 5
    assert body["pagination"]["has_more"] is True
    # summary stays a full aggregate, independent of the page size
    assert body["external_summary"]["evaluation_count"] == 5


# ---------------------------------------------------------------------------
# suite job runs
# ---------------------------------------------------------------------------


def test_suite_job_runs_paginated(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _make_agent(client)
    from backend.app import platform_store

    attempts = [
        {
            "attempt_id": "att-%d" % index,
            "order": index,
            "case_id": "case-%d" % index,
            "case_title": "Case %d" % index,
            "retry_index": 0,
            "status": "completed",
            "run_id": None,
        }
        for index in range(5)
    ]
    platform_store.save_suite_job(
        {
            "job_id": "job-1",
            "status": "completed",
            "suite_ref": {"id": "suite-1", "version": 1},
            "agent_ref": {"id": "agent-1"},
            "attempts": attempts,
            "created_at": "2026-01-05T10:00:00Z",
        },
        settings,
    )
    page = client.get("/api/suite-jobs/job-1/runs?limit=2&offset=0")
    assert page.status_code == 200, page.text
    body = page.json()
    assert len(body["runs"]) == 2
    assert body["runs_pagination"]["total"] == 5
    assert body["runs_pagination"]["has_more"] is True
    tail = client.get("/api/suite-jobs/job-1/runs?limit=2&offset=4")
    assert tail.json()["runs_pagination"]["has_more"] is False
    # invalid pagination is a structured 400
    bad = client.get("/api/suite-jobs/job-1/runs?limit=abc")
    assert bad.status_code in (400, 422)
