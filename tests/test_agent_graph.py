import importlib


def test_subagents_have_short_descriptions():
    module = importlib.import_module("agentloop.agents.subagents")
    subagents = module.build_subagents("openai:gpt-test")
    names = [item["name"] for item in subagents]
    assert names == ["trajectory_analyst", "tool_auditor", "root_cause_analyst", "report_writer"]
    assert all(len(item["description"]) < 120 for item in subagents)


def test_quality_assessment_tool_is_available_to_judging_subagents():
    module = importlib.import_module("agentloop.agents.subagents")
    subagents = {item["name"]: item for item in module.build_subagents("openai:gpt-test")}

    root_tools = {tool.__name__ for tool in subagents["root_cause_analyst"]["tools"]}
    writer_tools = {tool.__name__ for tool in subagents["report_writer"]["tools"]}

    assert "assess_run_quality" in root_tools
    assert "assess_run_quality" in writer_tools


def test_prompts_require_completion_deliverable_and_evidence_chain_checks():
    prompts = importlib.import_module("agentloop.agents.prompts")

    assert "assess_run_quality" in prompts.MAIN_SYSTEM_PROMPT
    assert "完成度" in prompts.REPORT_WRITER_PROMPT
    assert "交付物" in prompts.REPORT_WRITER_PROMPT
    assert "证据链" in prompts.REPORT_WRITER_PROMPT


def test_agent_module_imports_without_deepagent_installed():
    module = importlib.import_module("agentloop.agents.agent")
    assert hasattr(module, "get_agent")
    assert hasattr(module, "invoke_agent")
