"""Phase 1.1 batch reliability: idempotency, retry, concurrency caps, timeouts,
budgets and stale-job recovery for SuiteJobs."""

import json
import threading
import time

from fastapi.testclient import TestClient

from auditrail import run_contract
from auditrail import testsuites as suite_mod
from auditrail.client import AuditrailHTTPError
from auditrail.runner import create_platform_run
from backend.app import suite_executor
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path, **overrides):
    kwargs = dict(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("93.184.216.34",),
        agent_allowed_ports=(80, 443, 8788),
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr("backend.app.routes.platform.get_settings", lambda: settings)
    return settings


def _agent_payload(agent_id="xuanjian-local", **overrides):
    payload = {
        "agent_id": agent_id,
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


def _suite_payload(suite_id="suite-001", case_ids=("case-001", "case-002"), **overrides):
    payload = {
        "suite_id": suite_id,
        "title": "可靠性回归集 %s" % suite_id,
        "description": "批量可靠性",
        "case_ids": list(case_ids),
        "enabled": True,
    }
    payload.update(overrides)
    return payload


class FakeClient:
    """Deterministic fake AgentClient.

    - ``flaky`` session ids fail once with a retryable connect error, then succeed.
    - ``bad`` always fails retryably; ``bad400`` fails non-retryably (HTTP 400).
    - ``hang`` makes fetch_session block (finite) while ignoring cancellation,
      exercising the executor-side run watchdog.
    - ``fetch_delay`` slows every fetch; ``active``/``max_active`` gauge real
      concurrency for the limiter tests.
    """

    flaky = set()
    failed_once = set()
    hang_seconds = 0.0
    fetch_delay = 0.0
    active = 0
    max_active = 0
    stop_stream = False
    _gauge_lock = threading.Lock()

    def __init__(self, base_url, user_id, token=None, timeout=60.0, url_guard=None):
        pass

    def create_session(self, title="auditrail smoke test", model=""):
        return {"session": {"session_id": "s-chat"}}

    def start_chat(self, session_id, message, model=None, max_turns=20):
        return {"stream_id": "stream-1"}

    def stream_events(self, stream_id):
        while not FakeClient.stop_stream:
            yield "delta", {"content": "..."}
            time.sleep(0.01)
        yield "stream_end", {"ok": True}

    def fetch_session(self, session_id):
        with FakeClient._gauge_lock:
            FakeClient.active += 1
            FakeClient.max_active = max(FakeClient.max_active, FakeClient.active)
        try:
            if FakeClient.hang_seconds:
                time.sleep(FakeClient.hang_seconds)
            if session_id in FakeClient.flaky and session_id not in FakeClient.failed_once:
                FakeClient.failed_once.add(session_id)
                raise AuditrailHTTPError(0, "cannot connect")
            if session_id == "bad":
                raise AuditrailHTTPError(0, "cannot connect")
            if session_id == "bad400":
                raise AuditrailHTTPError(400, "bad request")
            if FakeClient.fetch_delay:
                time.sleep(FakeClient.fetch_delay)
            return {
                "session": {"session_id": session_id, "title": "t", "model": "m1"},
                "messages": [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "完成"},
                ],
                "llm_calls": [{"model": "m1", "totalTokens": 100}],
            }
        finally:
            with FakeClient._gauge_lock:
                FakeClient.active -= 1


def _reset_fake():
    FakeClient.flaky = set()
    FakeClient.failed_once = set()
    FakeClient.hang_seconds = 0.0
    FakeClient.fetch_delay = 0.0
    FakeClient.active = 0
    FakeClient.max_active = 0
    FakeClient.stop_stream = False


def _wait_job_terminal(client, job_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = client.get("/api/suite-jobs/%s" % job_id).json()["suite_job"]
        if payload["status"] in suite_mod.TERMINAL_JOB_STATUSES:
            return payload
        time.sleep(0.05)
    raise AssertionError("suite job %s did not reach a terminal state" % job_id)


def _setup(client, case_ids=("case-001", "case-002"), agent_id="xuanjian-local", **agent_overrides):
    client.post("/api/agents", json=_agent_payload(agent_id, **agent_overrides))
    for case_id in case_ids:
        session_id = {"case-flaky": "flaky-1", "case-bad": "bad", "case-bad400": "bad400"}.get(case_id, case_id)
        created = client.post("/api/test-cases", json=_case_payload(case_id, session_id))
        assert created.status_code == 201, created.text


# -------------------------------------------------------------- idempotency


def test_suite_job_idempotency_replays_and_conflicts(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client)
    client.post("/api/test-suites", json=_suite_payload())

    headers = {"Idempotency-Key": "batch-abc"}
    body = {"agent_id": "xuanjian-local", "max_retries": 1}
    first = client.post("/api/test-suites/suite-001/runs", json=body, headers=headers)
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    assert first.json()["idempotent_replay"] is False

    # same key + same request replays to the original job, never a second one
    replay = client.post("/api/test-suites/suite-001/runs", json=body, headers=headers)
    assert replay.json()["job_id"] == job_id
    assert replay.json()["idempotent_replay"] is True
    jobs = client.get("/api/suite-jobs").json()["suite_jobs"]
    assert len([item for item in jobs if item["job_id"] == job_id]) == 1

    job = _wait_job_terminal(client, job_id)
    assert job["status"] == "completed"
    # replay of a terminal job answers 200
    replay_done = client.post("/api/test-suites/suite-001/runs", json=body, headers=headers)
    assert replay_done.status_code == 200
    assert replay_done.json()["job_id"] == job_id

    # same key + different launch options conflicts
    conflict = client.post(
        "/api/test-suites/suite-001/runs",
        json={"agent_id": "xuanjian-local", "max_retries": 2},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_suite_job_rejects_invalid_launch_options(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    _setup(client)
    client.post("/api/test-suites", json=_suite_payload())

    for body in (
        {"agent_id": "xuanjian-local", "max_retries": 99},
        {"agent_id": "xuanjian-local", "timeout_seconds": -1},
        {"agent_id": "xuanjian-local", "budget": {"max_attempts": 0}},
    ):
        response = client.post("/api/test-suites/suite-001/runs", json=body)
        assert response.status_code == 400, body
        assert response.json()["detail"]["code"] == "invalid_job_options"


# -------------------------------------------------------------------- retry


def test_retryable_failure_creates_new_attempt_and_new_run(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    FakeClient.flaky = {"flaky-1"}
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client, case_ids=("case-flaky",))
    client.post("/api/test-suites", json=_suite_payload(case_ids=("case-flaky",)))

    accepted = client.post(
        "/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local", "max_retries": 1}
    )
    assert accepted.status_code == 202, accepted.text
    job = _wait_job_terminal(client, accepted.json()["job_id"])

    assert job["status"] == "completed"
    assert job["max_retries"] == 1
    attempts = job["attempts"]
    assert len(attempts) == 2
    assert [a["retry_index"] for a in attempts] == [0, 1]
    assert attempts[0]["status"] == "failed"
    assert "agent_unreachable" in attempts[0]["status_reason"]
    assert attempts[1]["status"] == "completed"
    # every retry is a new Run; the failed Run and its error.json survive
    assert attempts[0]["run_id"] != attempts[1]["run_id"]
    first_run = run_contract.read_run_record(settings.runs_dir / attempts[0]["run_id"])
    assert first_run["status"] == "failed"
    assert run_contract.read_error(settings.runs_dir / attempts[0]["run_id"])["retryable"] is True
    retry_run = run_contract.read_run_record(settings.runs_dir / attempts[1]["run_id"])
    assert retry_run["status"] == "completed"
    assert retry_run["suite_ref"]["attempt_id"] == attempts[1]["attempt_id"]
    # case-level stats follow the final attempt; runs/retries count all attempts
    stats = job["stats"]
    assert stats["total"] == 1 and stats["completed"] == 1 and stats["failed"] == 0
    assert stats["runs"] == 2 and stats["retries"] == 1


def test_non_retryable_failure_is_never_retried(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client, case_ids=("case-bad400",))
    client.post("/api/test-suites", json=_suite_payload(case_ids=("case-bad400",)))

    accepted = client.post(
        "/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local", "max_retries": 3}
    )
    job = _wait_job_terminal(client, accepted.json()["job_id"])
    assert job["status"] == "completed"
    assert len(job["attempts"]) == 1
    assert job["attempts"][0]["status"] == "failed"
    assert "agent_http_error" in job["attempts"][0]["status_reason"]
    assert job["stats"]["runs"] == 1 and job["stats"]["retries"] == 0


def test_retry_exhaustion_keeps_the_case_failed(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client, case_ids=("case-bad",))
    client.post("/api/test-suites", json=_suite_payload(case_ids=("case-bad",)))

    accepted = client.post(
        "/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local", "max_retries": 2}
    )
    job = _wait_job_terminal(client, accepted.json()["job_id"])
    assert len(job["attempts"]) == 3  # 1 initial + 2 retries, then give up
    assert all(a["status"] == "failed" for a in job["attempts"])
    stats = job["stats"]
    assert stats["failed"] == 1 and stats["runs"] == 3 and stats["retries"] == 2


# --------------------------------------------------------------- concurrency


def test_per_agent_concurrency_limit_serializes_jobs(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path, platform_max_concurrency=4))
    _reset_fake()
    FakeClient.fetch_delay = 0.1
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client)  # agent without max_concurrency -> default per-agent cap 1
    client.post("/api/test-suites", json=_suite_payload())
    client.post("/api/test-suites", json=_suite_payload(suite_id="suite-002"))

    first = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local"})
    second = client.post("/api/test-suites/suite-002/runs", json={"agent_id": "xuanjian-local"})
    assert first.status_code == 202 and second.status_code == 202
    job1 = _wait_job_terminal(client, first.json()["job_id"])
    job2 = _wait_job_terminal(client, second.json()["job_id"])
    assert job1["status"] == "completed" and job2["status"] == "completed"
    # the per-agent cap of 1 means the two jobs never overlapped on the agent
    assert FakeClient.max_active == 1
    assert job1["limits"] == {"global": 4, "agent": 1}


def test_global_concurrency_limit_caps_all_agents(tmp_path, monkeypatch):
    _patch_settings(
        monkeypatch,
        _settings(tmp_path, platform_max_concurrency=1, platform_agent_max_concurrency=4),
    )
    _reset_fake()
    FakeClient.fetch_delay = 0.1
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client, case_ids=("case-001",), agent_id="agent-a")
    _setup(client, case_ids=("case-002",), agent_id="agent-b")
    client.post("/api/test-suites", json=_suite_payload(case_ids=("case-001",)))
    client.post("/api/test-suites", json=_suite_payload(suite_id="suite-002", case_ids=("case-002",)))

    first = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "agent-a"})
    second = client.post("/api/test-suites/suite-002/runs", json={"agent_id": "agent-b"})
    job1 = _wait_job_terminal(client, first.json()["job_id"])
    job2 = _wait_job_terminal(client, second.json()["job_id"])
    assert job1["status"] == "completed" and job2["status"] == "completed"
    # global cap 1: even across different agents, runs never overlapped
    assert FakeClient.max_active == 1
    assert job1["limits"]["global"] == 1 and job1["limits"]["agent"] == 4


# ------------------------------------------------------------------ timeouts


def test_suite_total_timeout_cancels_in_flight_and_skips_pending(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    monkeypatch.setattr(suite_executor, "_TIMEOUT_GRACE_SECONDS", 0.2)
    client = TestClient(app)
    client.post("/api/agents", json=_agent_payload())
    # first case streams forever (chat mode), second would finish fast
    client.post(
        "/api/test-cases",
        json=_case_payload("case-001", "s1", input={"user_id": "u1", "message": "hi"}),
    )
    client.post("/api/test-cases", json=_case_payload("case-002", "s2"))
    client.post("/api/test-suites", json=_suite_payload())

    accepted = client.post(
        "/api/test-suites/suite-001/runs",
        json={"agent_id": "xuanjian-local", "timeout_seconds": 0.5},
    )
    assert accepted.status_code == 202, accepted.text
    job = _wait_job_terminal(client, accepted.json()["job_id"])
    FakeClient.stop_stream = True

    assert job["status"] == "failed"
    assert "suite_timeout" in job["status_reason"]
    assert job["timeout_seconds"] == 0.5
    attempts = job["attempts"]
    assert attempts[0]["status"] == "cancelled"
    assert attempts[1]["status"] == "skipped"
    assert attempts[1]["status_reason"] == "suite_timeout"
    # the in-flight Run was cancelled cooperatively, with a diagnosable record
    record = run_contract.read_run_record(settings.runs_dir / attempts[0]["run_id"])
    assert record["status"] == "cancelled"


def test_run_watchdog_marks_stuck_run_timed_out(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    FakeClient.hang_seconds = 2.5  # ignores cooperative cancel; watchdog must force it
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    monkeypatch.setattr(suite_executor, "_TIMEOUT_GRACE_SECONDS", 0.2)
    client = TestClient(app)
    client.post("/api/agents", json=_agent_payload())
    client.post("/api/test-cases", json=_case_payload("case-001", "s1", timeout_seconds=1))
    client.post("/api/test-suites", json=_suite_payload(case_ids=("case-001",)))

    accepted = client.post("/api/test-suites/suite-001/runs", json={"agent_id": "xuanjian-local"})
    job = _wait_job_terminal(client, accepted.json()["job_id"])

    assert job["status"] == "completed"  # one case, timed out: job still finishes
    attempt = job["attempts"][0]
    assert attempt["status"] == "timed_out"
    assert "run_timeout" in (attempt["status_reason"] or "")
    record = run_contract.read_run_record(settings.runs_dir / attempt["run_id"])
    assert record["status"] == "timed_out"
    # the late-finishing worker must not resurrect the forced terminal state
    time.sleep(1.6)
    record = run_contract.read_run_record(settings.runs_dir / attempt["run_id"])
    assert record["status"] == "timed_out"


# -------------------------------------------------------------------- budget


def test_budget_max_attempts_stops_scheduling(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client)
    client.post("/api/test-suites", json=_suite_payload())

    accepted = client.post(
        "/api/test-suites/suite-001/runs",
        json={"agent_id": "xuanjian-local", "budget": {"max_attempts": 1}},
    )
    job = _wait_job_terminal(client, accepted.json()["job_id"])
    assert job["status"] == "failed"
    assert "budget_exceeded" in job["status_reason"]
    attempts = job["attempts"]
    assert attempts[0]["status"] == "completed"
    assert attempts[1]["status"] == "skipped"
    assert attempts[1]["status_reason"] == "budget_exceeded"
    assert job["budget"] == {"max_attempts": 1, "max_total_tokens": None}
    assert job["budget_used"]["attempts"] == 1


def test_budget_max_total_tokens_stops_scheduling(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client)
    client.post("/api/test-suites", json=_suite_payload())

    accepted = client.post(
        "/api/test-suites/suite-001/runs",
        json={"agent_id": "xuanjian-local", "budget": {"max_total_tokens": 100}},
    )
    job = _wait_job_terminal(client, accepted.json()["job_id"])
    # each run reports 100 observed tokens, so the budget is exhausted after case 1
    assert job["status"] == "failed"
    assert "budget_exceeded" in job["status_reason"]
    assert job["attempts"][0]["status"] == "completed"
    assert job["attempts"][1]["status"] == "skipped"
    assert job["budget_used"]["total_tokens"] == 100


# ------------------------------------------------------------------ recovery


def test_recover_stale_job_reconciles_orphans_and_resumes(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    _setup(client)
    client.post("/api/test-suites", json=_suite_payload())

    # simulate a job orphaned by a dead process: attempt 1 was "running" with a
    # stale Run, attempt 2 never started
    orphan_run_id = "run-orphan-0001"
    run_dir = create_platform_run(
        settings.runs_dir,
        orphan_run_id,
        agent={"agent_id": "xuanjian-local", "version": "", "config_hash": "x"},
        test_case={"case_id": "case-001", "version": 1},
    )
    run_contract.update_run_status(run_dir, "running")

    suite = client.get("/api/test-suites/suite-001").json()["test_suite"]
    agent = client.get("/api/agents").json()["agents"][0]
    record = suite_mod.new_suite_job(
        "suitejob-orphan-1",
        suite=suite,
        agent=agent,
        judge=False,
        case_snapshots=[
            {"case_id": "case-001", "title": "t1", "version": 1},
            {"case_id": "case-002", "title": "t2", "version": 1},
        ],
        options={"max_retries": 1},
    )
    record["job_id"] = "suitejob-orphan-1"
    suite_mod.mark_job_running(record)
    suite_mod.advance_attempt(record, record["attempts"][0]["attempt_id"], "running", run_id=orphan_run_id)
    jobs_dir = settings.platform_dir / "suite_jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    suite_mod.write_suite_job(jobs_dir / "suitejob-orphan-1.json", record)

    # the stale job is flagged, not silently progressing
    stale_view = client.get("/api/suite-jobs/suitejob-orphan-1").json()["suite_job"]
    assert stale_view["stale"] is True

    resumed = client.post("/api/suite-jobs/recover")
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["resumed"] == ["suitejob-orphan-1"]

    job = _wait_job_terminal(client, "suitejob-orphan-1")
    assert job["status"] == "completed"
    assert job["recovered"] is True
    assert job["stale"] is False
    attempts = job["attempts"]
    # orphaned attempt: reconciled to failed/executor_interrupted, then retried
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["status_reason"] == "executor_interrupted"
    assert attempts[0]["run_id"] == orphan_run_id
    orphan_state = run_contract.read_run_record(run_dir)
    assert orphan_state["status"] == "failed"
    assert run_contract.read_error(run_dir)["error_type"] == "executor_interrupted"
    # the retry ran case-001 on a NEW run and completed
    retry = next(a for a in attempts if a["retry_index"] == 1)
    assert retry["status"] == "completed"
    assert retry["run_id"] != orphan_run_id
    # the never-started case ran normally
    assert attempts[1]["status"] == "completed"
    stats = job["stats"]
    assert stats["completed"] == 2 and stats["failed"] == 0
    assert stats["runs"] == 3 and stats["retries"] == 1


def test_recover_without_stale_jobs_is_a_noop(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    resumed = client.post("/api/suite-jobs/recover")
    assert resumed.status_code == 200
    assert resumed.json()["resumed"] == []
