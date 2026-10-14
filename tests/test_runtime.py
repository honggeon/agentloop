import json

from auditrail.runtime import build_runtime_graph, build_runtime_trace, load_expectations, save_expectations


def test_build_runtime_graph_projects_skill_tool_and_final_nodes():
    messages = [
        {"role": "user", "content": "做一个 PPT"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "skill_view",
                        "arguments": json.dumps({"name": "ppt-master", "file_path": "SKILL.md"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "tool_name": "skill_view", "content": "{\"success\": true}"},
        {"role": "assistant", "content": "已完成"},
    ]

    graph = build_runtime_graph(messages)

    assert [node["type"] for node in graph["nodes"]] == ["user", "assistant", "skill", "tool_result", "final"]
    assert graph["nodes"][2]["label"] == "skill_view: ppt-master"
    assert graph["nodes"][3]["status"] == "ok"
    assert graph["nodes"][4]["label"] == "Final Answer"
    assert graph["current_node"] == graph["nodes"][-1]["id"]
    assert {"from": graph["nodes"][2]["id"], "to": graph["nodes"][3]["id"]} in graph["edges"]


def test_build_runtime_graph_reads_flat_tool_call_schema():
    graph = build_runtime_graph(
        [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "name": "skill_view",
                        "arguments": json.dumps({"name": "ppt-master"}),
                    },
                    {
                        "id": "call-2",
                        "name": "write_file",
                        "arguments": json.dumps({"path": "a.md"}),
                    },
                ],
            }
        ]
    )

    labels = [node["label"] for node in graph["nodes"] if node["type"] in {"skill", "tool"}]
    assert labels == ["skill_view: ppt-master", "write_file"]


def test_build_runtime_graph_marks_error_tool_results():
    graph = build_runtime_graph(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "pip install"}}],
            },
            {"role": "tool", "tool_call_id": "call-1", "tool_name": "terminal", "content": "Traceback: failed"},
        ]
    )

    result_nodes = [node for node in graph["nodes"] if node["type"] == "tool_result"]
    assert result_nodes[0]["status"] == "error"
    tool_nodes = [node for node in graph["nodes"] if node["type"] == "tool"]
    assert tool_nodes[0]["status"] == "error"


def test_build_runtime_graph_closes_successful_tool_request():
    graph = build_runtime_graph(
        [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "pdf2md"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "tool_name": "terminal",
                "content": json.dumps({"output": "{\"success\": true}", "exit_code": 0, "error": None}),
            },
        ]
    )

    tool_nodes = [node for node in graph["nodes"] if node["type"] == "tool"]
    result_nodes = [node for node in graph["nodes"] if node["type"] == "tool_result"]
    assert tool_nodes[0]["status"] == "ok"
    assert result_nodes[0]["status"] == "ok"


def test_build_runtime_trace_models_agent_tool_and_final_spans():
    messages = [
        {"role": "user", "content": "请调用 ppt-master 生成 PPT"},
        {
            "role": "assistant",
            "content": "I will plan then call the skill.",
            "model": "gpt-5",
            "usage": {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            "tool_calls": [
                {
                    "id": "call-1",
                    "function": {
                        "name": "skill_view",
                        "arguments": json.dumps({"name": "ppt-master", "topic": "AI材料设计", "pages": 10}),
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "tool_name": "skill_view",
            "content": json.dumps({"file": "xxx.pptx"}),
        },
        {"role": "assistant", "content": "完成，文件已生成。"},
    ]

    trace = build_runtime_trace(
        messages,
        trace_id="trace-1",
        session_id="s1",
        agent_name="ppt-master runtime evaluation",
        agent_version="m1",
    )

    assert trace["traceId"] == "trace-1"
    assert trace["sessionId"] == "s1"
    assert trace["agentName"] == "ppt-master runtime evaluation"
    assert trace["agentVersion"] == "m1"
    assert trace["status"] == "success"
    assert trace["totalTokens"] == 160
    assert trace["metrics"] == {"steps": 5, "llmCalls": 1, "toolCalls": 0, "skillCalls": 1}

    spans = trace["spans"]
    assert [span["type"] for span in spans] == ["Agent", "LLM", "Skill", "Skill", "Agent"]
    assert spans[0]["name"] == "User Request"
    assert spans[1]["attributes"]["model"] == "gpt-5"
    assert spans[1]["tokenUsage"] == {"input": 120, "output": 40, "total": 160}
    assert spans[2]["name"] == "skill_view: ppt-master"
    assert spans[2]["input"] == {"name": "ppt-master", "topic": "AI材料设计", "pages": 10}
    assert spans[3]["parentSpanId"] == spans[2]["spanId"]
    assert spans[3]["output"] == {"file": "xxx.pptx"}
    assert spans[4]["name"] == "Final Response"


def test_build_runtime_trace_marks_tool_errors_failed():
    messages = [
        {"role": "user", "content": "search"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-2", "function": {"name": "web_search", "arguments": {"query": "x"}}}],
        },
        {"role": "tool", "tool_call_id": "call-2", "tool_name": "web_search", "content": "ModuleNotFoundError: missing"},
    ]

    trace = build_runtime_trace(messages, trace_id="trace-2", session_id="s2")

    assert trace["status"] == "failed"
    assert trace["metrics"]["toolCalls"] == 1
    result_span = trace["spans"][-1]
    assert result_span["type"] == "Tool"
    assert result_span["status"] == "failed"
    assert result_span["error"]["type"] == "ModuleNotFoundError"
    assert "ModuleNotFoundError" in result_span["error"]["message"]


def test_build_runtime_trace_treats_recovered_tool_error_as_completed_trace():
    messages = [
        {"role": "user", "content": "run"},
        {
            "role": "assistant",
            "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "bad args"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "tool_name": "terminal",
            "content": json.dumps({"output": "usage error", "exit_code": 2, "error": None}),
        },
        {"role": "assistant", "content": "Recovered and completed."},
    ]

    trace = build_runtime_trace(messages, trace_id="trace-3", session_id="s3")

    assert trace["status"] == "success"
    assert trace["spans"][2]["status"] == "failed"
    assert trace["spans"][-1]["name"] == "Final Response"


def test_build_runtime_trace_reports_running_when_tool_result_missing():
    trace = build_runtime_trace(
        [
            {"role": "user", "content": "run"},
            {
                "role": "assistant",
                "tool_calls": [{"id": "call-1", "function": {"name": "terminal", "arguments": "long command"}}],
            },
        ],
        trace_id="trace-4",
        session_id="s4",
    )

    assert trace["status"] == "running"
    assert trace["spans"][-1]["status"] == "running"


def test_save_and_load_expectations(tmp_path):
    save_expectations(tmp_path, "必须生成 pptx\n不能伪造用户确认")

    assert json.loads((tmp_path / "expectations.json").read_text(encoding="utf-8")) == {
        "acceptance_criteria": "必须生成 pptx\n不能伪造用户确认"
    }
    assert load_expectations(tmp_path)["acceptance_criteria"] == "必须生成 pptx\n不能伪造用户确认"
    assert load_expectations(tmp_path / "missing") == {}
