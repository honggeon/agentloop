import urllib.error

import pytest

from agentloop.client import AgentClient, AgentLoopHTTPError


def test_http_error_message_falls_back_when_body_empty():
    err = AgentLoopHTTPError(502, "")
    assert "HTTP 502" in str(err)
    assert "empty response body" in str(err)


def test_transport_failure_wrapped_with_url_and_reason(monkeypatch):
    def boom(request, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    client = AgentClient("http://127.0.0.1:9", "user-1")
    with pytest.raises(AgentLoopHTTPError) as excinfo:
        client.fetch_session("s1")
    assert excinfo.value.status == 0
    assert "cannot connect" in str(excinfo.value)
    assert "Connection refused" in str(excinfo.value)
