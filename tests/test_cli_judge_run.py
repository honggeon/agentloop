import json

from auditrail import cli
from tests.conftest import full_judge_dimensions


def _write_minimal_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps({"message_count": 1, "tool_call_count": 0, "tool_calls": [], "findings": []}),
        encoding="utf-8",
    )
    (run / "trajectory.jsonl").write_text(
        json.dumps({"index": 0, "role": "user", "content": "hi"}),
        encoding="utf-8",
    )
    return run


def test_judge_run_uses_agent_and_writes_report(tmp_path, monkeypatch):
    run = _write_minimal_run(tmp_path)

    async def fake_invoke(messages, config=None):
        assert "run_path" in messages[0]["content"]
        return {
            "overall_status": "success",
            "completion_score": 95,
            "confidence": "high",
            "summary": "ok",
            "findings": [],
            "dimensions": full_judge_dimensions(),
        }

    monkeypatch.setattr("auditrail.agents.agent.invoke_agent", fake_invoke)
    code = cli.main(["judge-run", "--run", str(run)])
    assert code == 0
    assert json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))["overall_status"] == "success"


def test_judge_run_repairs_incomplete_proposal_once(tmp_path, monkeypatch):
    # 首次 proposal 缺维度时，引擎在同一个 run 证据上做一次有界修复：
    # 第二次调用必须点名缺失维度，修复成功后报告完整写出。
    run = _write_minimal_run(tmp_path)
    calls = []

    async def fake_invoke(messages, config=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"overall_status": "success", "completion_score": 90, "summary": "缺维度"}
        return {
            "overall_status": "success",
            "completion_score": 90,
            "confidence": "high",
            "summary": "ok",
            "dimensions": full_judge_dimensions(),
        }

    monkeypatch.setattr("auditrail.agents.agent.invoke_agent", fake_invoke)
    code = cli.main(["judge-run", "--run", str(run)])

    assert code == 0
    assert len(calls) == 2
    assert "intent_recognition" in calls[1][0]["content"]
    assert "efficiency" in calls[1][0]["content"]
    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    assert report["judge_incomplete"] is False


def test_judge_run_fails_cleanly_when_proposal_stays_incomplete(tmp_path, monkeypatch, capsys):
    # 两次都不完整：明确失败（非零退出、stderr 说明缺失维度），不宣称成功，
    # 也不写出任何报告文件。
    run = _write_minimal_run(tmp_path)
    calls = []

    async def fake_invoke(messages, config=None):
        calls.append(messages)
        return {"overall_status": "success", "completion_score": 90, "summary": "缺维度"}

    monkeypatch.setattr("auditrail.agents.agent.invoke_agent", fake_invoke)
    code = cli.main(["judge-run", "--run", str(run)])

    assert code == 2
    assert len(calls) == 2
    assert not (run / "agent_judge.json").exists()
    assert not (run / "chart_data.json").exists()
    captured = capsys.readouterr()
    assert "intent_recognition" in captured.err
    assert "Wrote" not in captured.out


def test_write_run_outputs_persists_llm_calls(tmp_path):
    run = tmp_path / "run"
    session_response = {
        "session": {
            "session_id": "s1",
            "messages": [{"role": "assistant", "content": "done"}],
            "llm_calls": [
                {
                    "callId": "llm-1",
                    "model": "xuanjian-pro",
                    "provider": "LiteLLM",
                    "latency": 1.25,
                    "tokenUsage": {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150},
                }
            ],
        }
    }

    cli._write_run_outputs(run, session_response)

    payload = json.loads((run / "llm_calls.json").read_text(encoding="utf-8"))
    assert payload["llm_calls"][0]["callId"] == "llm-1"
    assert payload["llm_calls"][0]["tokenUsage"]["totalTokens"] == 150


def test_write_run_outputs_imports_configured_proxy_log(tmp_path, monkeypatch):
    run = tmp_path / "run"
    log = tmp_path / "proxy.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"callId": "before", "startTime": "2026-08-14T06:53:38.524742Z"}),
                json.dumps(
                    {
                        "callId": "inside",
                        "startTime": "2026-08-14T06:57:28.939702Z",
                        "tokenUsage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13},
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDITRAIL_LLM_PROXY_LOG", str(log))
    session_response = {
        "session": {
            "session_id": "s1",
            "created_at": 1786690648611,
            "updated_at": 1786690651668,
            "messages": [{"role": "assistant", "content": "done"}],
        }
    }

    cli._write_run_outputs(run, session_response)

    payload = json.loads((run / "llm_calls.json").read_text(encoding="utf-8"))
    assert [item["callId"] for item in payload["llm_calls"]] == ["inside"]


def test_import_llm_calls_command_writes_run_payload(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    log = tmp_path / "proxy.jsonl"
    log.write_text(
        json.dumps({"callId": "llm-1", "tokenUsage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13}})
        + "\n",
        encoding="utf-8",
    )

    code = cli.main(["import-llm-calls", "--run", str(run), "--log", str(log)])

    assert code == 0
    payload = json.loads((run / "llm_calls.json").read_text(encoding="utf-8"))
    assert payload["llm_calls"][0]["callId"] == "llm-1"


def test_import_llm_calls_filters_by_session_time_window(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "session.json").write_text(
        json.dumps({"session": {"created_at": 1786690648611, "updated_at": 1786690651668}}),
        encoding="utf-8",
    )
    log = tmp_path / "proxy.jsonl"
    log.write_text(
        "\n".join(
            [
                json.dumps({"callId": "before", "startTime": "2026-08-14T06:53:38.524742Z"}),
                json.dumps(
                    {
                        "callId": "inside-1",
                        "startTime": "2026-08-14T06:57:28.939702Z",
                        "tokenUsage": {"totalTokens": 13979},
                    }
                ),
                json.dumps(
                    {
                        "callId": "inside-2",
                        "startTime": "2026-08-14T06:57:30.034695Z",
                        "tokenUsage": {"totalTokens": 284},
                    }
                ),
                json.dumps({"callId": "after", "startTime": "2026-08-14T07:05:00.000000Z"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    code = cli.main(["import-llm-calls", "--run", str(run), "--log", str(log)])

    assert code == 0
    payload = json.loads((run / "llm_calls.json").read_text(encoding="utf-8"))
    assert [item["callId"] for item in payload["llm_calls"]] == ["inside-1", "inside-2"]


def test_judge_run_includes_acceptance_criteria(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps({"message_count": 1, "tool_call_count": 0, "tool_calls": [], "findings": []}),
        encoding="utf-8",
    )
    (run / "trajectory.jsonl").write_text(json.dumps({"index": 0, "role": "user", "content": "hi"}), encoding="utf-8")
    (run / "expectations.json").write_text(
        json.dumps({"acceptance_criteria": "必须生成 pptx\n不能伪造用户确认"}, ensure_ascii=False),
        encoding="utf-8",
    )

    async def fake_invoke(messages, config=None):
        content = messages[0]["content"]
        assert "Agent Judge acceptance criteria" in content
        assert "必须生成 pptx" in content
        assert "不能伪造用户确认" in content
        return {
            "overall_status": "success",
            "completion_score": 90,
            "confidence": "high",
            "summary": "ok",
            "dimensions": full_judge_dimensions(),
        }

    monkeypatch.setattr("auditrail.agents.agent.invoke_agent", fake_invoke)
    assert cli.main(["judge-run", "--run", str(run)]) == 0


def test_judge_run_model_profile_is_forwarded(tmp_path, monkeypatch):
    # --model-profile 透传给裁判调用，并作为 provenance 写进报告的 judge 字段。
    run = _write_minimal_run(tmp_path)
    seen = []
    config_path = tmp_path / "judge_models.yaml"
    config_path.write_text(
        "profiles:\n"
        "  kimi-for-coding:\n"
        "    provider: openai\n"
        "    model: kimi-for-coding\n"
        "    api_key_env: OPENAI_API_KEY\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "1")
    monkeypatch.setenv("AUDITRAIL_JUDGE_MODEL_CONFIG", str(config_path))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    async def fake_invoke(messages, config=None, model_profile=None):
        seen.append(model_profile)
        return {
            "overall_status": "success",
            "completion_score": 95,
            "confidence": "high",
            "summary": "ok",
            "dimensions": full_judge_dimensions(),
        }

    monkeypatch.setattr("auditrail.agents.agent.invoke_agent", fake_invoke)
    code = cli.main(["judge-run", "--run", str(run), "--model-profile", "kimi-for-coding"])
    assert code == 0
    assert seen == ["kimi-for-coding"]
    provenance = json.loads((run / "agent_judge.json").read_text(encoding="utf-8")).get("judge") or {}
    assert provenance.get("model_profile") == "kimi-for-coding"
    assert provenance.get("model") == "kimi-for-coding"
    assert provenance.get("engine") == "deepagent"


def test_judge_run_without_model_profile_keeps_legacy_signature(tmp_path, monkeypatch):
    # 未指定 profile 时不得传 model_profile kwarg：旧的 (messages, config)
    # 签名调用方（包括测试替身和 invoke_agent 默认值）完全不受影响。
    run = _write_minimal_run(tmp_path)

    async def fake_invoke(messages, config=None):
        return {
            "overall_status": "success",
            "completion_score": 95,
            "confidence": "high",
            "summary": "ok",
            "dimensions": full_judge_dimensions(),
        }

    monkeypatch.setattr("auditrail.agents.agent.invoke_agent", fake_invoke)
    assert cli.main(["judge-run", "--run", str(run)]) == 0


def test_load_dotenv_respects_guard(monkeypatch, tmp_path):
    # conftest sets AUDITRAIL_LOAD_DOTENV=0 for the whole suite; the guard must
    # keep working when invoked through the CLI entry as well.
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "0")
    monkeypatch.delenv("MODEL_PROVIDER", raising=False)
    cli._load_dotenv()
    assert "MODEL_PROVIDER" not in __import__("os").environ


def test_load_dotenv_does_not_override_real_env(monkeypatch):
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "1")
    monkeypatch.setenv("MODEL_PROVIDER", "from-real-env")
    cli._load_dotenv()
    # python-dotenv never overrides variables that already exist
    assert __import__("os").environ["MODEL_PROVIDER"] == "from-real-env"
