from agentloop.audit import audit_messages, render_markdown_report


def test_audit_flags_missing_tool_result():
    audit = audit_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
            }
        ]
    )

    codes = {finding["code"] for finding in audit["findings"]}
    assert "missing_tool_result" in codes
    assert "missing_final_answer" in codes


def test_audit_matches_tool_result_and_flags_terminal():
    audit = audit_messages(
        [
            {
                "role": "assistant",
                "content": "running command",
                "tool_calls": [{"id": "call_1", "function": {"name": "terminal", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "tool_name": "terminal", "content": '{"exit_code": 0}'},
            {"role": "assistant", "content": "done"},
        ]
    )

    assert audit["missing_tool_results"] == []
    assert audit["orphan_tool_results"] == []
    codes = {finding["code"] for finding in audit["findings"]}
    assert "dangerous_tool_used" in codes
    assert "missing_final_answer" not in codes


def test_audit_includes_tool_details_and_phases():
    messages = [
        {
            "role": "assistant",
            "content": "load",
            "tool_calls": [{"id": "call_1", "function": {"name": "skill_view", "arguments": '{"name":"xj-pdf"}'}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "tool_name": "skill_view", "content": '{"success": true}'},
        {
            "role": "assistant",
            "content": "read",
            "tool_calls": [{"id": "call_2", "function": {"name": "read_file", "arguments": '{"path":"/tmp/a.pdf"}'}}],
        },
        {"role": "tool", "tool_call_id": "call_2", "tool_name": "read_file", "content": "paper text"},
        {"role": "assistant", "content": "final"},
    ]

    audit = audit_messages(messages)

    assert audit["tool_calls"][0]["tool"] == "skill_view"
    assert audit["tool_calls"][0]["status"] == "ok"
    assert audit["tool_calls"][1]["args_preview"] == '{"path":"/tmp/a.pdf"}'
    assert audit["phases"] == [
        {"phase": "load skills", "tool_count": 1, "tools": ["skill_view"]},
        {"phase": "collect context", "tool_count": 1, "tools": ["read_file"]},
    ]


def test_markdown_report_renders_phase_and_tool_table():
    audit = audit_messages(
        [
            {
                "role": "assistant",
                "content": "run",
                "tool_calls": [{"id": "call_1", "function": {"name": "terminal", "arguments": '{"command":"pwd"}'}}],
            },
            {"role": "tool", "tool_call_id": "call_1", "tool_name": "terminal", "content": '{"exit_code": 0}'},
            {"role": "assistant", "content": "done"},
        ]
    )

    report = render_markdown_report({"session_id": "s1", "model": "m1"}, audit)

    assert "## Phases" in report
    assert "## Tool Calls" in report
    assert "| # | tool | status | args | result |" in report
    assert "`terminal`" in report


def test_audit_flags_nested_delegate_timeout():
    audit = audit_messages(
        [
            {
                "role": "assistant",
                "content": "delegate",
                "tool_calls": [{"id": "call_1", "function": {"name": "delegate_task", "arguments": "{}"}}],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "tool_name": "delegate_task",
                "content": '{"results":[{"task_index":0,"status":"timeout","error":"Subagent timed out"}]}',
            },
            {"role": "assistant", "content": "done"},
        ]
    )

    assert audit["tool_calls"][0]["status"] == "error"
    codes = {finding["code"] for finding in audit["findings"]}
    assert "tool_error" in codes


def test_audit_prefers_structured_is_error_flag():
    # DSH tool results carry an explicit is_error flag; the content heuristic
    # must not override it in either direction.
    audit = audit_messages(
        [
            {
                "role": "assistant",
                "content": "x",
                "tool_calls": [
                    {"id": "c1", "function": {"name": "bash", "arguments": "{}"}},
                    {"id": "c2", "function": {"name": "write", "arguments": "{}"}},
                ],
            },
            # content mentions a traceback but the call succeeded (test output)
            {"role": "tool", "tool_call_id": "c1", "tool_name": "bash", "content": "[PASS] all green\nTraceback in fixture text", "is_error": False},
            # benign-looking content but the call failed
            {"role": "tool", "tool_call_id": "c2", "tool_name": "write", "content": "denied", "is_error": True},
            {"role": "assistant", "content": "done"},
        ]
    )
    by_tool = {call["tool"]: call["status"] for call in audit["tool_calls"]}
    assert by_tool == {"bash": "ok", "write": "error"}
    codes = {finding["code"] for finding in audit["findings"]}
    assert "tool_error" in codes
    errors = [f for f in audit["findings"] if f["code"] == "tool_error"]
    assert len(errors) == 1 and "write" in errors[0]["message"]


def test_audit_falls_back_to_heuristic_without_is_error():
    audit = audit_messages(
        [
            {
                "role": "assistant",
                "content": "x",
                "tool_calls": [{"id": "c1", "function": {"name": "bash", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "c1", "tool_name": "bash", "content": "Traceback (most recent call last): ..."},
            {"role": "assistant", "content": "done"},
        ]
    )
    assert audit["tool_calls"][0]["status"] == "error"
