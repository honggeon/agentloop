import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

from auditrail.llm_proxy import (
    _configured_proxy_log_source,
    append_jsonl,
    build_llm_call_record,
    create_proxy_app,
    create_stdlib_proxy_server,
    import_configured_proxy_log,
    usage_from_response,
)


def test_usage_from_response_normalizes_openai_usage():
    usage = usage_from_response(
        {
            "usage": {
                "prompt_tokens": 120,
                "completion_tokens": 30,
                "total_tokens": 150,
            }
        }
    )

    assert usage == {"inputTokens": 120, "outputTokens": 30, "totalTokens": 150}


def test_usage_from_response_tries_alias_keys():
    # Regression: _int_value returned on the first alias unconditionally, so
    # usages reported only under a later alias were zero-filled.
    usage = usage_from_response(
        {
            "usage": {
                "input_tokens": 88,
                "outputTokens": 12,
                "totalTokens": 100,
            }
        }
    )

    assert usage == {"inputTokens": 88, "outputTokens": 12, "totalTokens": 100}


def test_usage_from_response_alias_fallback_after_unparseable():
    usage = usage_from_response(
        {
            "usage": {
                "prompt_tokens": "not-a-number",
                "input_tokens": 42,
                "completion_tokens": 7,
                "total_tokens": 49,
            }
        }
    )

    assert usage == {"inputTokens": 42, "outputTokens": 7, "totalTokens": 49}



def test_build_llm_call_record_omits_api_key_and_keeps_usage():
    record = build_llm_call_record(
        request_body={"model": "xuanjian-lite", "messages": [{"role": "user", "content": "hi"}]},
        response_body={"id": "chatcmpl-1", "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
        status_code=200,
        started_at="2026-08-14T01:00:00Z",
        ended_at="2026-08-14T01:00:01Z",
        latency=1.0,
        error=None,
        headers={"authorization": "Bearer secret", "x-session-id": "s1"},
    )

    assert record["callId"] == "chatcmpl-1"
    assert record["traceId"] == "s1"
    assert record["model"] == "xuanjian-lite"
    assert record["tokenUsage"]["totalTokens"] == 7
    assert "secret" not in json.dumps(record)


def test_build_llm_call_record_marks_http_error_detail():
    record = build_llm_call_record(
        request_body={"model": "xuanjian-lite"},
        response_body={"error": "Unauthorized"},
        status_code=401,
        started_at="2026-08-14T01:00:00Z",
        ended_at="2026-08-14T01:00:01Z",
        latency=1.0,
        error=None,
        headers={},
    )

    assert record["status"] == "failed"
    assert record["error"] == {"type": "HTTPStatusError", "message": "HTTP 401"}


def test_append_jsonl_writes_one_record_per_line(tmp_path):
    path = tmp_path / "calls.jsonl"

    append_jsonl(path, {"callId": "a"})
    append_jsonl(path, {"callId": "b"})

    assert path.read_text(encoding="utf-8").splitlines() == ['{"callId": "a"}', '{"callId": "b"}']


def test_import_configured_proxy_log_uses_explicit_source(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    log = tmp_path / "calls.jsonl"
    log.write_text(json.dumps({"callId": "llm-1"}) + "\n", encoding="utf-8")

    payload = import_configured_proxy_log(run, source=str(log))

    assert payload is not None
    assert payload["llm_calls"][0]["callId"] == "llm-1"
    assert (run / "llm_calls.json").exists()


def test_dotenv_guard_disables_the_project_env_fallback(monkeypatch):
    """The repo ``.env`` must not steer an isolated run.

    A developer's ``.env`` can point ``AUDITRAIL_LLM_PROXY_LOG`` at a remote host,
    so reading it must respect the same guard the application uses.
    """
    monkeypatch.delenv("AUDITRAIL_LLM_PROXY_LOG", raising=False)
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "0")
    assert _configured_proxy_log_source() == ""


def test_env_fallback_still_reads_the_file_when_enabled(monkeypatch, tmp_path):
    """The documented convenience keeps working with the guard lifted."""
    monkeypatch.delenv("AUDITRAIL_LLM_PROXY_LOG", raising=False)
    monkeypatch.setenv("AUDITRAIL_LOAD_DOTENV", "1")
    env_file = tmp_path / ".env"
    env_file.write_text("AUDITRAIL_LLM_PROXY_LOG=/tmp/local-calls.jsonl\n", encoding="utf-8")
    monkeypatch.setattr("auditrail.llm_proxy.ENV_FILE", env_file)
    assert _configured_proxy_log_source() == "/tmp/local-calls.jsonl"


def test_proxy_forwards_chat_completion_and_logs_usage(tmp_path, monkeypatch):
    log_path = tmp_path / "calls.jsonl"

    def fake_forward(upstream_base_url, path, method, headers, body, timeout):
        assert upstream_base_url == "http://127.0.0.1:7500/v1"
        assert path == "/chat/completions"
        assert method == "POST"
        assert json.loads(body.decode("utf-8"))["model"] == "xuanjian-lite"
        assert headers["authorization"] == "Bearer sk-test"
        return 200, {"content-type": "application/json"}, json.dumps(
            {
                "id": "chatcmpl-1",
                "model": "xuanjian-lite",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            }
        ).encode("utf-8")

    monkeypatch.setattr("auditrail.llm_proxy.forward_request", fake_forward)
    client = TestClient(create_proxy_app("http://127.0.0.1:7500/v1", log_path))

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test", "X-Session-Id": "s1"},
        json={"model": "xuanjian-lite", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert response.json()["usage"]["total_tokens"] == 13
    logged = json.loads(log_path.read_text(encoding="utf-8"))
    assert logged["traceId"] == "s1"
    assert logged["tokenUsage"]["totalTokens"] == 13


def test_proxy_adds_stream_usage_option_and_logs_sse_usage(tmp_path, monkeypatch):
    log_path = tmp_path / "calls.jsonl"

    def fake_forward(upstream_base_url, path, method, headers, body, timeout):
        payload = json.loads(body.decode("utf-8"))
        assert payload["stream"] is True
        assert payload["stream_options"]["include_usage"] is True
        response = (
            'data: {"id":"chatcmpl-stream","model":"xuanjian-lite",'
            '"choices":[{"delta":{"content":"ok"}}]}\n\n'
            'data: {"id":"chatcmpl-stream","model":"xuanjian-lite","choices":[],'
            '"usage":{"prompt_tokens":21,"completion_tokens":5,"total_tokens":26}}\n\n'
            "data: [DONE]\n\n"
        )
        return 200, {"content-type": "text/event-stream"}, response.encode("utf-8")

    monkeypatch.setattr("auditrail.llm_proxy.forward_request", fake_forward)
    client = TestClient(create_proxy_app("http://127.0.0.1:7500/v1", log_path))

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer sk-test"},
        json={"model": "xuanjian-lite", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    )

    assert response.status_code == 200
    assert "data: [DONE]" in response.text
    logged = json.loads(log_path.read_text(encoding="utf-8"))
    assert logged["callId"] == "chatcmpl-stream"
    assert logged["status"] == "success"
    assert logged["tokenUsage"] == {"inputTokens": 21, "outputTokens": 5, "totalTokens": 26}


def test_stdlib_proxy_forwards_chat_completion_and_logs_usage(tmp_path):
    log_path = tmp_path / "calls.jsonl"

    class UpstreamHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            assert self.path == "/v1/chat/completions"
            assert self.headers.get("authorization") == "Bearer sk-test"
            assert json.loads(body.decode("utf-8"))["model"] == "xuanjian-lite"
            payload = {
                "id": "chatcmpl-stdlib",
                "model": "xuanjian-lite",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10},
            }
            response = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, format, *args):
            return

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
    proxy = create_stdlib_proxy_server(
        "http://127.0.0.1:%s/v1" % upstream.server_address[1],
        log_path,
        "127.0.0.1",
        0,
    )
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    proxy_thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    upstream_thread.start()
    proxy_thread.start()
    try:
        request = urllib.request.Request(
            "http://127.0.0.1:%s/v1/chat/completions" % proxy.server_address[1],
            data=json.dumps(
                {"model": "xuanjian-lite", "messages": [{"role": "user", "content": "hi"}]}
            ).encode("utf-8"),
            headers={"content-type": "application/json", "authorization": "Bearer sk-test"},
            method="POST",
        )

        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))

        assert payload["usage"]["total_tokens"] == 10
        logged = json.loads(log_path.read_text(encoding="utf-8"))
        assert logged["callId"] == "chatcmpl-stdlib"
        assert logged["status"] == "success"
        assert logged["tokenUsage"] == {"inputTokens": 4, "outputTokens": 6, "totalTokens": 10}
    finally:
        proxy.shutdown()
        upstream.shutdown()
        proxy.server_close()
        upstream.server_close()
