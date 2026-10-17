"""Phase 0 runner: lifecycle states, error.json, evaluation.json, judge interplay."""

import json

from auditrail import run_contract
from auditrail.agents.tools import assess_run_quality, write_judge_report
from auditrail.client import AuditrailHTTPError
from auditrail.evaluation import load_evaluation
from auditrail.netguard import EndpointPolicy
from auditrail.runner import build_evaluation_envelope, create_platform_run, execute_run


def _agent(**overrides):
    agent = {
        "agent_id": "agent-1",
        "name": "Test Agent",
        "version": "git:abc123",
        "adapter": "http_session",
        "endpoint": "http://93.184.216.34:80",
        "capabilities": ["chat", "session"],
        "config_hash": "deadbeef",
    }
    agent.update(overrides)
    return agent


def _case(**overrides):
    case = {
        "case_id": "case-1",
        "version": 1,
        "title": "抓取会话",
        "adapter": "http_session",
        "input": {"user_id": "u1", "session_id": "s1"},
        "fixtures": [],
        "expectations": [{"id": "e1", "type": "final_answer_present", "required": True}],
        "timeout_seconds": 30,
        "adapter_config": {},
    }
    case.update(overrides)
    return case


def _policy(**kwargs):
    kwargs.setdefault("allow_private", True)
    kwargs.setdefault("allowed_ports", (80, 443, 8788))
    return EndpointPolicy(**kwargs)


def _session_response(messages=None, session_id="s1"):
    return {
        "session": {"session_id": session_id, "title": "t", "model": "m1"},
        "messages": messages
        or [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "完成"},
        ],
    }


class FakeClient:
    """Stand-in for AgentClient; records calls, never touches the network."""

    calls = []
    behavior = "ok"

    def __init__(self, base_url, user_id, token=None, timeout=60.0, url_guard=None):
        FakeClient.calls.append(("init", base_url, user_id, url_guard is not None))

    def create_session(self, title="auditrail smoke test", model=""):
        FakeClient.calls.append(("create_session", title))
        return {"session": {"session_id": "s-chat"}}

    def start_chat(self, session_id, message, model=None, max_turns=20):
        FakeClient.calls.append(("start_chat", session_id, message))
        return {"stream_id": "stream-1"}

    def stream_events(self, stream_id):
        if FakeClient.behavior == "stream_error":
            yield "error", {"message": "boom"}
            return
        yield "delta", {"content": "..."}
        yield "stream_end", {"ok": True}

    def fetch_session(self, session_id):
        if FakeClient.behavior == "connect_fail":
            raise AuditrailHTTPError(0, "cannot connect")
        if FakeClient.behavior == "http_500":
            raise AuditrailHTTPError(500, "server exploded")
        if FakeClient.behavior == "timeout":
            raise TimeoutError("timed out")
        return _session_response(session_id=session_id)


def _reset_fake():
    FakeClient.calls = []
    FakeClient.behavior = "ok"


def _new_run(tmp_path, agent=None, case=None):
    agent = agent or _agent()
    case = case or _case()
    run_dir = create_platform_run(tmp_path, "run-test-1", agent=agent, test_case=case)
    return run_dir, agent, case


def test_execute_run_completes_and_writes_contract_files(tmp_path, monkeypatch):
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    run_dir, agent, case = _new_run(tmp_path)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy())

    assert record["status"] == "completed"
    assert record["started_at"] and record["finished_at"]
    for name in ("session.json", "summary.json", "trajectory.jsonl", "audit.json", "evaluation.json", "run.json"):
        assert (run_dir / name).exists(), name
    # the manifest lists completed formal artifacts; run.json itself is written last
    for name in ("session.json", "summary.json", "trajectory.jsonl", "audit.json", "evaluation.json"):
        assert name in record["artifact_manifest"]
    assert "run.json" not in record["artifact_manifest"]
    # source_meta is still merged into summary.json (legacy-compatible behavior)
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["server_host"]
    # run.json carries only platform metadata, no session-derived fields
    assert "session_id" not in record
    envelope = load_evaluation(run_dir)
    assert envelope["status"] == "completed"
    assert envelope["expectation_results"][0]["status"] == "pass"
    assert envelope["final_quality"]["status"] == "success"
    assert FakeClient.calls[0][3] is True  # redirect guard installed


def test_execute_run_chat_flow_uses_sse(tmp_path, monkeypatch):
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    case = _case(input={"user_id": "u1", "message": "做一个 PPT"})
    run_dir, agent, case = _new_run(tmp_path, case=case)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy())

    assert record["status"] == "completed"
    assert ("create_session", "auditrail: 抓取会话") in FakeClient.calls
    assert ("start_chat", "s-chat", "做一个 PPT") in FakeClient.calls


def test_execute_run_chat_requires_capability(tmp_path, monkeypatch):
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    agent = _agent(capabilities=["session"])
    case = _case(input={"user_id": "u1", "message": "hi"})
    run_dir, agent, case = _new_run(tmp_path, agent=agent, case=case)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy())

    assert record["status"] == "failed"
    assert "capability_not_supported" in record["status_reason"]
    error = run_contract.read_error(run_dir)
    assert error["error_type"] == "capability_not_supported"
    assert error["phase"] == "validate"


def test_execute_run_connect_failure_is_failed_with_error_json(tmp_path, monkeypatch):
    _reset_fake()
    FakeClient.behavior = "connect_fail"
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    run_dir, agent, case = _new_run(tmp_path)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy())

    assert record["status"] == "failed"
    assert "agent_unreachable" in record["status_reason"]
    error = run_contract.read_error(run_dir)
    assert error["error_type"] == "agent_unreachable"
    assert error["retryable"] is True
    # no fabricated report artifacts
    assert not (run_dir / "summary.json").exists()
    assert "error.json" in record["artifact_manifest"]


def test_execute_run_timeout_maps_to_timed_out(tmp_path, monkeypatch):
    _reset_fake()
    FakeClient.behavior = "timeout"
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    run_dir, agent, case = _new_run(tmp_path)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy())

    assert record["status"] == "timed_out"
    error = run_contract.read_error(run_dir)
    assert error["error_type"] == "agent_timeout"


def test_execute_run_http_error_keeps_external_status(tmp_path, monkeypatch):
    _reset_fake()
    FakeClient.behavior = "http_500"
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    run_dir, agent, case = _new_run(tmp_path)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy())

    assert record["status"] == "failed"
    error = run_contract.read_error(run_dir)
    assert error["error_type"] == "agent_http_error"
    assert error["external_status"] == 500


def test_execute_run_stream_error_is_failed(tmp_path, monkeypatch):
    _reset_fake()
    FakeClient.behavior = "stream_error"
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    case = _case(input={"user_id": "u1", "message": "hi"})
    run_dir, agent, case = _new_run(tmp_path, case=case)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy())

    assert record["status"] == "failed"
    error = run_contract.read_error(run_dir)
    assert error["error_type"] == "agent_stream_error"


def test_execute_run_rejects_restricted_endpoint_before_connecting(tmp_path, monkeypatch):
    _reset_fake()
    monkeypatch.setattr("auditrail.runner.AgentClient", FakeClient)
    agent = _agent(endpoint="http://169.254.169.254")
    run_dir, agent, case = _new_run(tmp_path, agent=agent)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy(allow_private=False))

    assert record["status"] == "failed"
    assert "endpoint_rejected" in record["status_reason"]
    assert FakeClient.calls == []  # never connected


def test_execute_run_runner_exception_is_failed_not_hidden(tmp_path, monkeypatch):
    _reset_fake()

    class BrokenClient(FakeClient):
        def fetch_session(self, session_id):
            raise RuntimeError("unexpected runner bug")

    monkeypatch.setattr("auditrail.runner.AgentClient", BrokenClient)
    run_dir, agent, case = _new_run(tmp_path)

    record = execute_run(run_dir, agent=agent, test_case=case, policy=_policy())

    assert record["status"] == "failed"
    error = run_contract.read_error(run_dir)
    assert error["error_type"] == "internal_error"
    assert error["phase"] == "runner"


def test_unsupported_adapter_fails_before_running(tmp_path):
    case = {
        "id": "LEGACY-1",
        "title": "legacy command case",
        "args": ["--version"],
        "expect": {"exit_code": 0},
    }
    run_dir = create_platform_run(tmp_path, "run-legacy", agent=_agent(), test_case={"case_id": "LEGACY-1", "version": 1})
    record = execute_run(run_dir, agent=_agent(), test_case=case, policy=_policy())
    assert record["status"] == "failed"
    assert "unsupported_adapter" in record["status_reason"]


# --- deterministic failure vs judge ------------------------------------------------


def _write_run_with_tool_error(tmp_path):
    run_dir = tmp_path / "runs" / "run-det-fail"
    run_dir.mkdir(parents=True)
    summary = {"session_id": "s1", "title": "回答即可", "message_count": 3}
    messages = [
        {"role": "user", "content": "读文件"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "tool_name": "read_file", "content": "Error: file not found"},
        {"role": "assistant", "content": "文件不存在"},
    ]
    audit = {
        "message_count": 4,
        "tool_call_count": 1,
        "tool_calls": [{"index": 1, "tool_call_id": "c1", "tool": "read_file", "status": "error"}],
        "findings": [{"severity": "error", "code": "tool_error", "message": "Tool result reports an error"}],
        "missing_tool_results": [],
        "orphan_tool_results": [],
        "final_answer_chars": 5,
        "phases": [],
    }
    (run_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    (run_dir / "audit.json").write_text(json.dumps(audit), encoding="utf-8")
    (run_dir / "trajectory.jsonl").write_text(
        "\n".join(json.dumps({"index": i, "role": m["role"], "content": m.get("content", "")}) for i, m in enumerate(messages)),
        encoding="utf-8",
    )
    return run_dir


def test_deterministic_failure_caps_judge_pass(tmp_path):
    """A blocking deterministic failure must survive a fully-passing judge."""
    run_dir = _write_run_with_tool_error(tmp_path)
    case = _case()
    envelope = build_evaluation_envelope(run_dir, case)
    from auditrail.evaluation import write_evaluation

    write_evaluation(run_dir, envelope)

    quality = assess_run_quality(str(run_dir))
    assert quality["completion"]["status"] == "failed"
    assert quality["completion"]["score_cap"] == 60  # blocking tool error -> 60 cap

    proposal = {
        "overall_status": "success",
        "completion_score": 95,
        "confidence": "high",
        "summary": "一切顺利",
        "findings": [],
    }
    paths = write_judge_report(str(run_dir), proposal)
    report = json.loads(open(paths["json_path"], encoding="utf-8").read())
    assert report["overall_status"] == "failed"  # judge cannot override determinism
    assert report["completion_score"] == 60
    # a no-evidence dimension stays unknown with a null score, never fabricated
    dimension = report["dimensions"]["intent_recognition"]
    assert dimension["status"] == "unknown"
    assert dimension["score"] is None
    assert dimension["source"] == "missing"


def test_multiple_caps_take_the_minimum(tmp_path):
    run_dir = _write_run_with_tool_error(tmp_path)
    case = _case(
        expectations=[
            {"id": "e1", "type": "artifact_exists", "path": "report.md", "required": True},
        ]
    )
    envelope = build_evaluation_envelope(run_dir, case)
    from auditrail.evaluation import write_evaluation

    write_evaluation(run_dir, envelope)

    quality = assess_run_quality(str(run_dir))
    assert quality["completion"]["score_cap"] == 40  # min(60 tool_error, 60 expectation, 40 artifact)

    proposal = {"overall_status": "success", "completion_score": 90, "confidence": "high", "summary": "ok"}
    paths = write_judge_report(str(run_dir), proposal)
    report = json.loads(open(paths["json_path"], encoding="utf-8").read())
    assert report["completion_score"] == 40
    assert report["overall_status"] == "failed"


def test_assess_run_quality_legacy_run_without_envelope_unchanged(tmp_path):
    run_dir = _write_run_with_tool_error(tmp_path)
    quality = assess_run_quality(str(run_dir))
    assert quality["evaluation"]["status"] == "missing"
    assert quality["completion"]["score_cap"] == 100  # legacy inference path only


def test_invalid_envelope_never_gates(tmp_path):
    run_dir = _write_run_with_tool_error(tmp_path)
    (run_dir / "evaluation.json").write_text("{broken json", encoding="utf-8")
    quality = assess_run_quality(str(run_dir))
    assert quality["evaluation"]["status"] == "invalid"
    assert quality["completion"]["score_cap"] == 100  # invalid envelope is not used

    proposal = {"overall_status": "success", "completion_score": 90, "confidence": "high", "summary": "ok"}
    paths = write_judge_report(str(run_dir), proposal)
    report = json.loads(open(paths["json_path"], encoding="utf-8").read())
    assert report["overall_status"] == "success"
    assert report["completion_score"] == 90


def test_render_and_judge_preserve_run_json(tmp_path):
    from auditrail.cli import _write_run_outputs

    run_dir = _write_run_with_tool_error(tmp_path)
    run_contract.write_run_record(run_dir, run_contract.new_run_record(run_dir.name))
    # re-render session-derived artifacts
    _write_run_outputs(run_dir, _session_response())
    assert (run_dir / "run.json").exists()
    # re-judge
    proposal = {"overall_status": "partial", "completion_score": 50, "confidence": "low", "summary": "x"}
    write_judge_report(str(run_dir), proposal)
    record = run_contract.read_run_record(run_dir)
    assert record["run_id"] == run_dir.name
