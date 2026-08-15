from agentloop.trajectory import llm_calls_from_session_response, messages_from_session_response, normalize_messages, session_summary


def test_normalize_messages_classifies_roles_and_tool_requests():
    messages = [
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_1", "tool_name": "read_file", "content": "ok"},
    ]

    rows = normalize_messages(messages)

    assert rows[0]["kind"] == "user_message"
    assert rows[1]["kind"] == "assistant_tool_request"
    assert rows[2]["kind"] == "tool_result"


def test_session_summary_counts_tools():
    response = {
        "session": {
            "session_id": "s1",
            "model": "xuanjian-lite",
            "messages": [
                {"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "terminal"}}]},
                {"role": "tool", "tool_call_id": "c1", "tool_name": "terminal", "content": "{}"},
            ],
        }
    }

    summary = session_summary(response)

    assert summary["session_id"] == "s1"
    assert summary["tool_call_count"] == 1
    assert summary["tool_result_count"] == 1
    assert summary["tool_names"] == ["terminal"]


def test_messages_from_session_response_accepts_top_level_messages():
    response = {
        "session": {"session_id": "s1"},
        "messages": [{"role": "assistant", "content": "done"}],
    }

    assert messages_from_session_response(response) == [{"role": "assistant", "content": "done"}]


def test_llm_calls_from_session_response_accepts_session_and_top_level_calls():
    response = {
        "session": {
            "session_id": "s1",
            "llm_calls": [
                {
                    "callId": "llm-1",
                    "model": "xuanjian-lite",
                    "tokenUsage": {"inputTokens": 10, "outputTokens": 4, "totalTokens": 14},
                }
            ],
        },
        "llmCalls": [
            {
                "call_id": "llm-2",
                "model": "xuanjian-pro",
                "token_usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28},
            }
        ],
    }

    calls = llm_calls_from_session_response(response)

    assert [call.get("callId") or call.get("call_id") for call in calls] == ["llm-1", "llm-2"]
