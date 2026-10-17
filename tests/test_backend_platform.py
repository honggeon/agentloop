"""Phase 0 platform API: agents, test-cases, async runs, idempotency, status, compat."""

import json
import time

from fastapi.testclient import TestClient

from auditrail import run_contract
from auditrail.client import AuditrailHTTPError
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path):
    return Settings(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("192.168.1.238", "93.184.216.34"),
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
        "version": "git:abc123",
        "adapter": "http_session",
        "endpoint": "http://93.184.216.34",
        "capabilities": ["chat", "session"],
        "metadata": {"environment": "test"},
    }
    payload.update(overrides)
    return payload


def _case_payload(**overrides):
    payload = {
        "case_id": "tool-recovery-001",
        "version": 1,
        "title": "工具失败后恢复",
        "adapter": "http_session",
        "input": {"user_id": "u1", "session_id": "s1"},
        "expectations": [{"id": "e1", "type": "final_answer_present", "required": True}],
        "timeout_seconds": 60,
    }
    payload.update(overrides)
    return payload


class FakeClient:
    behavior = "ok"

    def __init__(self, base_url, user_id, token=None, timeout=60.0, url_guard=None):
        pass

    def fetch_session(self, session_id):
        if FakeClient.behavior == "connect_fail":
            raise AuditrailHTTPError(0, "cannot connect")
        return {
            "session": {"session_id": session_id, "title": "t", "model": "m1"},
            "messages": [
                {"role": "user", "content": "你好"},
                {"role": "assistant", "content": "完成"},
            ],
        }


def _wait_terminal(runs_dir, run_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = run_contract.read_run_record(runs_dir / run_id)
        if record and record["status"] in run_contract.TERMINAL_STATUSES:
            return record
        time.sleep(0.02)
    raise AssertionError("run %s did not reach a terminal state" % run_id)


def test_create_and_list_agents(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    created = client.post("/api/agents", json=_agent_payload())
    assert created.status_code == 201, created.text
    agent = created.json()["agent"]
    assert agent["agent_id"] == "xuanjian-local"
    assert agent["config_hash"]
    listed = client.get("/api/agents").json()["agents"]
    assert [item["agent_id"] for item in listed] == ["xuanjian-local"]


def test_create_agent_rejects_ssrf_endpoints(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    for endpoint in ("http://169.254.169.254", "http://127.0.0.1:8788", "http://10.1.2.3"):
        response = client.post("/api/agents", json=_agent_payload(endpoint=endpoint))
        assert response.status_code == 400, endpoint
        assert response.json()["detail"]["code"] == "endpoint_rejected"


def test_create_agent_rejects_credentials_in_metadata(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    response = client.post("/api/agents", json=_agent_payload(metadata={"key": "sk-secretvalue"}))
    assert response.status_code == 400
    assert "invalid_agent" == response.json()["detail"]["code"]


def test_test_case_crud_and_legacy_listing(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    created = client.post("/api/test-cases", json=_case_payload())
    assert created.status_code == 201, created.text
    fetched = client.get("/api/test-cases/tool-recovery-001")
    assert fetched.status_code == 200
    assert fetched.json()["test_case"]["adapter"] == "http_session"
    assert client.get("/api/test-cases/nope").status_code == 404
    listed = client.get("/api/test-cases").json()
    ids = [case["case_id"] for case in listed["test_cases"]]
    assert "tool-recovery-001" in ids
    # legacy cases/*.json suites are exposed through the command adapter mapping
    legacy = [case for case in listed["test_cases"] if case.get("legacy")]
    assert legacy and all(case["adapter"] == "command" for case in legacy)


def test_create_test_case_rejects_invalid_payload(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    response = client.post("/api/test-cases", json={"case_id": "x", "title": "t", "adapter": "http_session", "input": {}})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_test_case"


def test_post_run_executes_to_completion(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    FakeClient.behavior = "ok"
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    client.post("/api/agents", json=_agent_payload())
    client.post("/api/test-cases", json=_case_payload())

    accepted = client.post("/api/runs", json={"agent_id": "xuanjian-local", "test_case_id": "tool-recovery-001"})
    assert accepted.status_code == 202, accepted.text
    body = accepted.json()
    assert body["status"] == "queued"
    assert body["status_url"] == "/api/runs/%s/status" % body["run_id"]

    record = _wait_terminal(settings.runs_dir, body["run_id"])
    assert record["status"] == "completed"

    status = client.get(body["status_url"]).json()
    assert status["status"] == "completed"
    assert status["stale"] is False
    assert status["agent_ref"]["id"] == "xuanjian-local"
    assert status["test_case_ref"]["id"] == "tool-recovery-001"
    assert status["evaluation_status"] == "completed"
    assert "session.json" in status["artifact_manifest"]

    detail = client.get("/api/runs/%s" % body["run_id"]).json()
    assert detail["run"]["status"] == "completed"
    assert detail["evaluation"]["final_quality"]["status"] == "success"
    assert detail["summary"]["session_id"] == "s1"

    listed = client.get("/api/runs").json()["runs"]
    row = next(item for item in listed if item["name"] == body["run_id"])
    assert row["run_status"] == "completed"


def test_post_run_agent_failure_is_diagnosable(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    FakeClient.behavior = "connect_fail"
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    client.post("/api/agents", json=_agent_payload())
    client.post("/api/test-cases", json=_case_payload())

    accepted = client.post("/api/runs", json={"agent_id": "xuanjian-local", "test_case_id": "tool-recovery-001"})
    assert accepted.status_code == 202
    record = _wait_terminal(settings.runs_dir, accepted.json()["run_id"])
    assert record["status"] == "failed"
    assert "agent_unreachable" in record["status_reason"]

    status = client.get(accepted.json()["status_url"]).json()
    assert status["error"]["error_type"] == "agent_unreachable"
    assert status["error"]["phase"] == "collect"


def test_post_run_idempotency_key_replays_and_conflicts(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    FakeClient.behavior = "ok"
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    client = TestClient(app)
    client.post("/api/agents", json=_agent_payload())
    client.post("/api/test-cases", json=_case_payload())

    headers = {"Idempotency-Key": "abc-123"}
    first = client.post(
        "/api/runs",
        json={"agent_id": "xuanjian-local", "test_case_id": "tool-recovery-001", "judge": False},
        headers=headers,
    )
    assert first.status_code == 202
    run_id = first.json()["run_id"]
    _wait_terminal(settings.runs_dir, run_id)

    # same key + same request returns the original run, never a second one
    replay = client.post(
        "/api/runs",
        json={"agent_id": "xuanjian-local", "test_case_id": "tool-recovery-001", "judge": False},
        headers=headers,
    )
    assert replay.json()["run_id"] == run_id
    assert replay.status_code == 200  # original run already terminal
    runs = client.get("/api/runs").json()["runs"]
    assert len([item for item in runs if item["name"] == run_id]) == 1

    # same key + different request conflicts
    conflict = client.post(
        "/api/runs",
        json={"agent_id": "xuanjian-local", "test_case_id": "tool-recovery-001", "judge": True},
        headers=headers,
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_post_run_rejects_unknown_refs_and_unsupported_adapter(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    assert client.post("/api/runs", json={"agent_id": "nope", "test_case_id": "x"}).status_code == 404
    client.post("/api/agents", json=_agent_payload())
    assert client.post("/api/runs", json={"agent_id": "xuanjian-local", "test_case_id": "nope"}).status_code == 404
    # legacy command-adapter case is readable but not runnable via the Phase 0 runner
    cases = client.get("/api/test-cases").json()["test_cases"]
    legacy = next(case for case in cases if case.get("legacy"))
    rejected = client.post("/api/runs", json={"agent_id": "xuanjian-local", "test_case_id": legacy["case_id"]})
    assert rejected.status_code == 400
    assert rejected.json()["detail"]["code"] == "unsupported_adapter"


def test_post_run_rejects_restricted_agent_endpoint(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings = Settings(
        runs_dir=settings.runs_dir,
        platform_dir=settings.platform_dir,
        agent_allowed_hosts=(),
        agent_allowed_ports=(80, 443),
    )
    _patch_settings(monkeypatch, settings)
    client = TestClient(app)
    # store an agent with a restricted endpoint directly (bypassing route validation)
    from backend.app import platform_store

    platform_store.create_agent(_agent_payload(endpoint="http://192.168.1.238:8788"), settings)
    client.post("/api/test-cases", json=_case_payload())
    response = client.post("/api/runs", json={"agent_id": "xuanjian-local", "test_case_id": "tool-recovery-001"})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "endpoint_rejected"


def test_stale_queued_run_is_flagged(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    run_dir = settings.runs_dir / "run-stale-1"
    run_dir.mkdir(parents=True)
    run_contract.write_run_record(run_dir, run_contract.new_run_record("run-stale-1"))
    client = TestClient(app)
    status = client.get("/api/runs/run-stale-1/status").json()
    assert status["status"] == "queued"
    assert status["stale"] is True  # no executor owns it: not pretending to progress


def test_old_run_without_run_json_still_works(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    run_dir = settings.runs_dir / "20260101-000000-legacy"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
    (run_dir / "audit.json").write_text(json.dumps({"tool_calls": [], "findings": [], "phases": []}), encoding="utf-8")
    client = TestClient(app)

    listed = client.get("/api/runs").json()["runs"]
    row = next(item for item in listed if item["name"] == "20260101-000000-legacy")
    assert row["run_status"] == "completed"

    status = client.get("/api/runs/20260101-000000-legacy/status").json()
    assert status["status"] == "completed"
    assert status["legacy_inferred"] is True
    assert status["agent_ref"] is None

    detail = client.get("/api/runs/20260101-000000-legacy").json()
    assert detail["summary"]["session_id"] == "s1"
    assert detail["run"]["legacy_inferred"] is True
    assert detail["evaluation"] is None
    assert not (run_dir / "run.json").exists()  # reads never rewrite legacy runs


def test_status_rejects_path_traversal(tmp_path, monkeypatch):
    _patch_settings(monkeypatch, _settings(tmp_path))
    client = TestClient(app)
    assert client.get("/api/runs/..%2F..%2Fetc/status").status_code in {400, 404, 422}
    assert client.get("/api/runs/../status").status_code in {400, 404, 422}


def test_run_with_judge_success_and_failure(tmp_path, monkeypatch):
    settings = _patch_settings(monkeypatch, _settings(tmp_path))
    FakeClient.behavior = "ok"
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)

    from auditrail.agents.tools import write_judge_report

    def good_judge(run_dir):
        write_judge_report(
            str(run_dir),
            {"overall_status": "success", "completion_score": 90, "confidence": "high", "summary": "好"},
        )

    def bad_judge(run_dir):
        raise RuntimeError("model unavailable")

    client = TestClient(app)
    client.post("/api/agents", json=_agent_payload())
    client.post("/api/test-cases", json=_case_payload())

    monkeypatch.setattr("backend.app.routes.platform._default_judge_runner", good_judge)
    accepted = client.post(
        "/api/runs", json={"agent_id": "xuanjian-local", "test_case_id": "tool-recovery-001", "judge": True}
    )
    record = _wait_terminal(settings.runs_dir, accepted.json()["run_id"])
    assert record["status"] == "completed"
    run_dir = settings.runs_dir / accepted.json()["run_id"]
    assert (run_dir / "agent_judge.json").exists()
    envelope = json.loads((run_dir / "evaluation.json").read_text(encoding="utf-8"))
    assert envelope["judge"]["status"] == "completed"

    # judge failure leaves the run completed and the reason diagnosable
    monkeypatch.setattr("backend.app.routes.platform._default_judge_runner", bad_judge)
    accepted2 = client.post(
        "/api/runs", json={"agent_id": "xuanjian-local", "test_case_id": "tool-recovery-001", "judge": True}
    )
    record2 = _wait_terminal(settings.runs_dir, accepted2.json()["run_id"])
    assert record2["status"] == "completed"
    run_dir2 = settings.runs_dir / accepted2.json()["run_id"]
    assert not (run_dir2 / "agent_judge.json").exists()
    envelope2 = json.loads((run_dir2 / "evaluation.json").read_text(encoding="utf-8"))
    assert envelope2["judge"]["status"] == "failed"
    assert "model unavailable" not in envelope2["judge"]["error"] or envelope2["judge"]["error"]
