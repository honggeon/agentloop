import importlib

from auditrail.agents.prompts import MAIN_SYSTEM_PROMPT, REPORT_WRITER_PROMPT


def test_report_writer_prompt_requires_dimensions_and_reliability():
    assert "dimensions" in REPORT_WRITER_PROMPT
    assert "judge_reliability" in REPORT_WRITER_PROMPT
    assert "intent_recognition" in REPORT_WRITER_PROMPT
    assert "evidence_citation" in REPORT_WRITER_PROMPT
    assert "没有证据" in REPORT_WRITER_PROMPT
    assert "unknown" in REPORT_WRITER_PROMPT
    assert "dimensions" in MAIN_SYSTEM_PROMPT


def test_subagents_have_short_descriptions():
    module = importlib.import_module("auditrail.agents.subagents")
    subagents = module.build_subagents("openai:gpt-test")
    names = [item["name"] for item in subagents]
    assert names == ["trajectory_analyst", "tool_auditor", "root_cause_analyst", "report_writer"]
    assert all(len(item["description"]) < 120 for item in subagents)


def test_quality_assessment_tool_is_available_to_judging_subagents():
    module = importlib.import_module("auditrail.agents.subagents")
    subagents = {item["name"]: item for item in module.build_subagents("openai:gpt-test")}

    root_tools = {tool.__name__ for tool in subagents["root_cause_analyst"]["tools"]}
    writer_tools = {tool.__name__ for tool in subagents["report_writer"]["tools"]}

    assert "assess_run_quality" in root_tools
    assert "assess_run_quality" in writer_tools


def test_prompts_require_completion_deliverable_and_evidence_chain_checks():
    prompts = importlib.import_module("auditrail.agents.prompts")

    assert "assess_run_quality" in prompts.MAIN_SYSTEM_PROMPT
    assert "完成度" in prompts.REPORT_WRITER_PROMPT
    assert "交付物" in prompts.REPORT_WRITER_PROMPT
    assert "证据链" in prompts.REPORT_WRITER_PROMPT


def test_agent_module_imports_without_deepagent_installed():
    module = importlib.import_module("auditrail.agents.agent")
    assert hasattr(module, "get_agent")
    assert hasattr(module, "invoke_agent")


def test_get_agent_builds_fresh_graph_per_event_loop(monkeypatch):
    """Regression: a cached graph bound to a closed asyncio.run loop made every
    judge after the first fail with "Event loop is closed". get_agent must
    build a fresh graph per call so each event loop gets its own."""
    import asyncio

    from auditrail.agents import agent as agent_module

    builds = []

    def fake_create_deep_agent(model, **kwargs):
        graph = object()
        builds.append(graph)
        return graph

    monkeypatch.setattr(
        agent_module,
        "_load_deepagent_runtime",
        lambda: (fake_create_deep_agent, lambda spec: object(), lambda: object()),
    )
    monkeypatch.setattr(agent_module, "_validate_model_spec", lambda init, spec: None)

    first = asyncio.run(agent_module.get_agent())
    second = asyncio.run(agent_module.get_agent())

    assert first is not second
    assert len(builds) == 2


def test_sanitize_no_proxy_unbrackets_ipv6_entries(monkeypatch):
    """Regression: shells exporting NO_PROXY=...,[::1] made the OpenAI SDK's
    httpx2 client fail model init with "InvalidURL: Invalid port: ':1]'".
    Bracketed IPv6 entries must be rewritten to the handled unbracketed form."""
    import os

    from auditrail.agents import agent as agent_module

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1,[::1]")
    monkeypatch.setenv("no_proxy", "example.com, [fe80::1]")

    agent_module._sanitize_no_proxy_env()

    assert os.environ["NO_PROXY"] == "127.0.0.1,localhost,::1,::1"
    assert os.environ["no_proxy"] == "example.com,fe80::1"


def test_sanitize_no_proxy_leaves_values_without_brackets_untouched(monkeypatch):
    import os

    from auditrail.agents import agent as agent_module

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    monkeypatch.delenv("no_proxy", raising=False)

    agent_module._sanitize_no_proxy_env()

    assert os.environ["NO_PROXY"] == "127.0.0.1,localhost,::1"
    assert "no_proxy" not in os.environ
