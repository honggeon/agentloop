"""Phase 1 platform API: TestSuite CRUD and sequential SuiteJob execution."""

import json
import time

from fastapi.testclient import TestClient

from auditrail import run_contract
from auditrail import testsuites as suite_mod
from auditrail.client import AuditrailHTTPError
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path):
    return Settings(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("93.184.216.34",),
        agent_allowed_ports=(80, 443, 8788),
    )


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr("backend.app.routes.platform.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)
    return settings


def _agent_payload(**overrides):
    payload = {
        "agent_id": "xuanjian-local",
        "name": "Xuanjian Agent",
        "adapter": "http_session",
        "endpoint": "http://93.184.216.34",
        "capabilities": ["chat", "session"],
        "metadata": {},
    }
    payload.update(overrides)
    return payload


def _case_payload(case_id, session_id, **overrides):
    payload = {
        "case_id": case_id,
        "version": 1,
        "title": "用例 %s" % case_id,
        "adapter": "http_session",
        "input": {"user_id": "u1", "session_id": session_id},
        "expectations": [{"id": "e1", "type": "final_answer_present", "required": True}],
        "timeout_seconds": 60,
    }
    payload.update(overrides)
    return payload


def _suite_payload(**overrides):
    payload = {
        "suite_id": "suite-001",
        "title": "工具恢复回归集",
        "description": "批量回归",
        "case_ids": ["case-001", "case-002"],
        "enabled": True,
    }
    payload.update(overrides)
    return payload


class FakeClient:
    """Records fetch order; session_id "bad" fails to connect."""

    calls = []
    stop_stream = False

    def __init__(self, base_url, user_id, token=None, timeout=60.0, url_guard=None):
        pass

    def create_session(self, title="auditrail smoke test", model=""):
        return {"session": {"session_id": "s-chat"}}

    def start_chat(self, session_id, message, model=None, max_turns=20):
        return {"stream_id": "stream-1"}

    def stream_events(self, stream_id):
        # Keeps streaming until the test releases it, so cancellation has a
        # deterministic window to land inside the stream loop.
        while not FakeClient.stop_stream:
            yield "delta", {"content": "..."}
            time.sleep(0.01)
        yield "stream_end", {"ok": True}

    def fetch_session(self, session_id):
        FakeClient.calls.append(session_id)
        if session_id == "bad":
            raise AuditrailHTTPError(0, "cannot connect")
        return {
            "session": {"session_id": session_id, "title": "t", "model": "m1"},
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "完成"},
            ],
        }


def _reset_fake():
    FakeClient.calls = []
    FakeClient.stop_stream = False


def _wait_job_terminal(client, job_id, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get("/api/suite-jobs/%s" % job_id).json()["suite_job"]
        if payload["status"] in suite_mod.TERMINAL_JOB_STATUSES:
            return payload
        time.sleep(0.05)
    raise AssertionError("suite job %s did not reach a terminal state" % job_id)


def _setup_agent_and_cases(client, case_ids=("case-001", "case-002")):
    client.post("/api/agents", json=_agent_payload())
    for case_id in case_ids:
        session_id = {"case-001": "s1", "case-002": "s2", "case-bad": "bad"}.get(case_id, case_id)
        created = client.post("/api/test-cases", json=_case_payload(case_id, session_id))
        assert created.status_code == 201, created.text


# -------------------------------------------------------------------- CRUD


def test_test_suite_crud_roundtrip(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    _setup_agent_and_cases(client)

    created = client.post("/api/test-suites", json=_suite_payload())
    assert created.status_code == 201, created.text
    suite = created.json()["test_suite"]
    assert suite["suite_id"] == "suite-001"
    assert suite["version"] == 1
    assert suite["case_ids"] == ["case-001", "case-002"]
    assert suite["created_at"] and suite["updated_at"]

    listed = client.get("/api/test-suites").json()["test_suites"]
    assert [item["suite_id"] for item in listed] == ["suite-001"]

    detail = client.get("/api/test-suites/suite-001").json()
    assert [case["case_id"] for case in detail["test_cases"]] == ["case-001", "case-002"]
    assert detail["missing_case_ids"] == []

    updated = client.put(
        "/api/test-suites/suite-001",
        json={"title": "新回归集", "description": "d", "case_ids": ["case-002"], "enabled": False},
    )
    assert updated.status_code == 200, updated.text
    suite = updated.json()["test_suite"]
    assert suite["version"] == 2
    assert suite["title"] == "新回归集"
    assert suite["case_ids"] == ["case-002"]
    assert suite["enabled"] is False

    assert client.delete("/api/test-suites/suite-001").status_code == 200
    assert client.get("/api/test-suites/suite-001").status_code == 404
    assert client.delete("/api/test-suites/suite-001").status_code == 404


def test_test_suite_validation_errors(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    _setup_agent_and_cases(client)

    # duplicate suite_id conflicts instead of silently replacing
    assert client.post("/api/test-suites", json=_suite_payload()).status_code == 201
    duplicate = client.post("/api/test-suites", json=_suite_payload())
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "suite_exists"

    # unknown case references are rejected at save time
    bad = client.post("/api/test-suites", json=_suite_payload(suite_id="s2", case_ids=["case-001", "nope"]))
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "invalid_test_suite"

    # missing title / bad identifier
    assert client.post("/api/test-suites", json={"suite_id": "s3", "case_ids": []}).status_code == 400
    assert client.post("/api/test-suites", json=_suite_payload(suite_id="../x")).status_code == 400

    # update of an unknown suite is a 404
    missing = client.put("/api/test-suites/nope", json={"title": "t", "case_ids": []})
    assert missing.status_code == 404


# --------------------------------------------------------------- execution


def test_suite_job_executes_cases_in_order_with_independent_runs(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup_agent_and_cases(client)
    client.post("/api/test-suites", json=_suite_payload())

    accepted = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local"})
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    assert accepted.json()["status"] == "queued"
    assert accepted.json()["status_url"] == "/api/suite-jobs/%s" % job_id

    job = _wait_job_terminal(client, job_id)
    assert job["status"] == "completed"
    assert job["cancel_requested"] is False
    assert job["stats"] == {
        "total": 2, "queued": 0, "running": 0,
        "completed": 2, "failed": 0, "timed_out": 0, "cancelled": 0, "skipped": 0,
        "runs": 2, "retries": 0,
    }
    # cases ran strictly in suite order, each producing its own Run
    assert FakeClient.calls == ["s1", "s2"]
    attempts = job["attempts"]
    assert [a["case_id"] for a in attempts] == ["case-001", "case-002"]
    assert all(a["status"] == "completed" for a in attempts)
    assert attempts[0]["run_id"] != attempts[1]["run_id"]
    assert all(a["run_status_url"].endswith("/status") for a in attempts)
    assert all(a["evaluation_status"] == "completed" for a in attempts)
    assert all(a["final_quality_status"] == "success" for a in attempts)

    # every Run is an independent platform run linked back to the job
    for attempt in attempts:
        record = run_contract.read_run_record(settings.runs_dir / attempt["run_id"])
        assert record["status"] == "completed"
        assert record["suite_ref"]["id"] == "suite-001"
        assert record["suite_ref"]["job_id"] == job_id
        assert record["suite_ref"]["attempt_id"] == attempt["attempt_id"]
        assert "session.json" in record["artifact_manifest"]

    listed = client.get("/api/suite-jobs").json()["suite_jobs"]
    assert [item["job_id"] for item in listed] == [job_id]
    assert listed[0]["stats"]["completed"] == 2
    assert listed[0]["stale"] is False


def test_suite_job_continues_after_a_case_failure(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup_agent_and_cases(client, case_ids=("case-001", "case-bad", "case-002"))
    client.post("/api/test-suites", json=_suite_payload(case_ids=["case-001", "case-bad", "case-002"]))

    accepted = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local"})
    assert accepted.status_code == 202
    job = _wait_job_terminal(client, accepted.json()["job_id"])

    # a failed case does not block the suite; the job itself still completes
    assert job["status"] == "completed"
    assert FakeClient.calls == ["s1", "bad", "s2"]
    attempts = job["attempts"]
    assert [a["status"] for a in attempts] == ["completed", "failed", "completed"]
    assert "agent_unreachable" in attempts[1]["status_reason"]
    stats = job["stats"]
    assert stats["total"] == 3 and stats["completed"] == 2 and stats["failed"] == 1

    # the failed Run keeps its diagnosable error.json; others keep full evidence
    failed_record = run_contract.read_run_record(settings.runs_dir / attempts[1]["run_id"])
    assert failed_record["status"] == "failed"
    error = run_contract.read_error(settings.runs_dir / attempts[1]["run_id"])
    assert error["error_type"] == "agent_unreachable"
    ok_record = run_contract.read_run_record(settings.runs_dir / attempts[2]["run_id"])
    assert "summary.json" in ok_record["artifact_manifest"]


def test_suite_job_cancel_marks_running_cancelled_and_pending_skipped(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    client.post("/api/agents", json=_agent_payload())
    # first case streams forever (chat mode), second would fetch a session
    client.post("/api/test-cases", json=_case_payload("case-001", "s1", input={"user_id": "u1", "message": "hi"}))
    client.post("/api/test-cases", json=_case_payload("case-002", "s2"))
    client.post("/api/test-suites", json=_suite_payload())

    accepted = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local"})
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]

    # wait until the first case is genuinely running inside the stream loop
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get("/api/suite-jobs/%s" % job_id).json()["suite_job"]
        if job["attempts"][0]["status"] == "running":
            break
        time.sleep(0.05)
    else:
        raise AssertionError("first attempt never started")

    cancelled = client.post("/api/suite-jobs/%s/cancel" % job_id)
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["suite_job"]["cancel_requested"] is True

    job = _wait_job_terminal(client, job_id)
    FakeClient.stop_stream = True
    assert job["status"] == "cancelled"
    attempts = job["attempts"]
    assert attempts[0]["status"] == "cancelled"
    assert attempts[1]["status"] == "skipped"
    assert attempts[1]["run_id"] is None
    stats = job["stats"]
    assert stats["cancelled"] == 1 and stats["skipped"] == 1 and stats["completed"] == 0

    # the in-flight Run was cooperatively cancelled with a diagnosable record
    record = run_contract.read_run_record(settings.runs_dir / attempts[0]["run_id"])
    assert record["status"] == "cancelled"
    error = run_contract.read_error(settings.runs_dir / attempts[0]["run_id"])
    assert error["error_type"] == "cancelled"

    # a terminal job cannot be cancelled again
    again = client.post("/api/suite-jobs/%s/cancel" % job_id)
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "suite_job_not_cancellable"


def test_suite_job_rejects_unknown_refs_and_disabled_suite(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    _setup_agent_and_cases(client)
    client.post("/api/test-suites", json=_suite_payload())

    assert client.post("/api/test-suites/nope/runs", json={"agent_id": "xuanjian-local"}).status_code == 404
    assert client.get("/api/suite-jobs/nope").status_code == 404
    assert client.post("/api/suite-jobs/nope/cancel").status_code == 404

    missing_agent = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "nope"})
    assert missing_agent.status_code == 404
    assert missing_agent.json()["detail"]["code"] == "agent_not_found"

    no_agent = client.post("/api/test-suites/suite-001/runs", json={})
    assert no_agent.status_code == 400

    client.put("/api/test-suites/suite-001", json={"title": "t", "case_ids": ["case-001"], "enabled": False})
    disabled = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local"})
    assert disabled.status_code == 400
    assert disabled.json()["detail"]["code"] == "suite_disabled"


def test_suite_job_missing_case_fails_attempt_without_blocking(tmp_path, monkeypatch):
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    _setup_agent_and_cases(client)
    client.post("/api/test-suites", json=_suite_payload())
    # remove the case from the store directly after the suite snapshot validated it
    store_path = tmp_path / "platform" / "test_cases.json"
    data = json.loads(store_path.read_text(encoding="utf-8"))
    data["items"] = [item for item in data["items"] if item.get("case_id") != "case-001"]
    store_path.write_text(json.dumps(data), encoding="utf-8")

    accepted = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local"})
    assert accepted.status_code == 202
    job = _wait_job_terminal(client, accepted.json()["job_id"])
    assert job["status"] == "completed"
    attempts = job["attempts"]
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["status_reason"] == "test_case_not_found"
    assert attempts[0]["run_id"] is None
    assert attempts[1]["status"] == "completed"
