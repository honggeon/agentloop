from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

#: Values of ``AUDITRAIL_LOAD_DOTENV`` that disable project ``.env`` reading.
_DOTENV_DISABLED = {"0", "false", "no", "off", ""}

#: The project ``.env`` consulted only when ``AUDITRAIL_LLM_PROXY_LOG`` is unset.
ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def create_proxy_app(upstream_base_url: str, log_path: Path, timeout: float = 300.0):
    from fastapi import FastAPI, Request
    from fastapi.responses import Response

    globals()["Request"] = Request
    globals()["Response"] = Response
    app = FastAPI(title="Auditrail LLM Metrics Proxy")
    upstream = upstream_base_url.rstrip("/")

    @app.api_route("/{path:path}", methods=["POST"])
    async def proxy(path: str, request: Request) -> Response:
        body = await request.body()
        body, request_body = _body_with_stream_usage(body)
        headers = _forward_headers(dict(request.headers))
        upstream_path = _upstream_path(path)
        started_at = _utc_now()
        started = time.perf_counter()
        status_code = 502
        response_headers: dict[str, str] = {"content-type": "application/json"}
        response_body = b""
        error: dict[str, str] | None = None
        try:
            status_code, response_headers, response_body = forward_request(
                upstream,
                upstream_path,
                request.method,
                headers,
                body,
                timeout,
            )
        except Exception as exc:
            error = {"type": type(exc).__name__, "message": str(exc)}
            response_body = json.dumps({"error": error}, ensure_ascii=False).encode("utf-8")
        latency = round(time.perf_counter() - started, 3)
        ended_at = _utc_now()
        response_json = _response_payload_for_metrics(response_headers, response_body)
        append_jsonl(
            log_path,
            build_llm_call_record(
                request_body=request_body,
                response_body=response_json,
                status_code=status_code,
                started_at=started_at,
                ended_at=ended_at,
                latency=latency,
                error=error,
                headers=dict(request.headers),
            ),
        )
        return Response(
            content=response_body,
            status_code=status_code,
            media_type=response_headers.get("content-type", "application/json").split(";")[0],
        )

    return app


def create_stdlib_proxy_server(
    upstream_base_url: str,
    log_path: Path,
    host: str,
    port: int,
    timeout: float = 300.0,
) -> ThreadingHTTPServer:
    upstream = upstream_base_url.rstrip("/")

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("content-length", "0")))
            body, request_body = _body_with_stream_usage(body)
            headers = _forward_headers(dict(self.headers.items()))
            started_at = _utc_now()
            started = time.perf_counter()
            status_code = 502
            response_headers: dict[str, str] = {"content-type": "application/json"}
            response_body = b""
            error: dict[str, str] | None = None
            try:
                status_code, response_headers, response_body = forward_request(
                    upstream,
                    _upstream_path(self.path),
                    "POST",
                    headers,
                    body,
                    timeout,
                )
            except Exception as exc:
                error = {"type": type(exc).__name__, "message": str(exc)}
                response_body = json.dumps({"error": error}, ensure_ascii=False).encode("utf-8")
            latency = round(time.perf_counter() - started, 3)
            append_jsonl(
                log_path,
                build_llm_call_record(
                    request_body=request_body,
                    response_body=_response_payload_for_metrics(response_headers, response_body),
                    status_code=status_code,
                    started_at=started_at,
                    ended_at=_utc_now(),
                    latency=latency,
                    error=error,
                    headers=dict(self.headers.items()),
                ),
            )
            content_type = response_headers.get("content-type", "application/json")
            self.send_response(status_code)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: Any) -> None:
            print("%s - %s" % (self.address_string(), format % args))

    return ThreadingHTTPServer((host, port), ProxyHandler)


def serve_stdlib_proxy(
    upstream_base_url: str,
    log_path: Path,
    host: str,
    port: int,
    timeout: float = 300.0,
) -> None:
    server = create_stdlib_proxy_server(upstream_base_url, log_path, host, port, timeout=timeout)
    print("Stdlib metrics proxy listening on http://%s:%s/v1" % (host, server.server_address[1]))
    try:
        server.serve_forever()
    finally:
        server.server_close()


def forward_request(
    upstream_base_url: str,
    path: str,
    method: str,
    headers: dict[str, str],
    body: bytes,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    url = upstream_base_url.rstrip("/") + path
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def usage_from_response(payload: dict[str, Any]) -> dict[str, int]:
    usage = payload.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    input_tokens = _int_value(usage, "prompt_tokens", "input_tokens", "inputTokens")
    output_tokens = _int_value(usage, "completion_tokens", "output_tokens", "outputTokens")
    total_tokens = _int_value(usage, "total_tokens", "totalTokens")
    if total_tokens == 0:
        total_tokens = input_tokens + output_tokens
    return {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
    }


def build_llm_call_record(
    *,
    request_body: dict[str, Any],
    response_body: dict[str, Any],
    status_code: int,
    started_at: str,
    ended_at: str,
    latency: float,
    error: dict[str, str] | None,
    headers: dict[str, str],
) -> dict[str, Any]:
    normalized_headers = {str(key).lower(): str(value) for key, value in headers.items()}
    response_id = response_body.get("id") if isinstance(response_body, dict) else None
    model = ""
    if isinstance(response_body, dict):
        model = str(response_body.get("model") or "")
    model = model or str(request_body.get("model") or "")
    status = "success" if status_code < 400 and error is None else "failed"
    if status_code >= 400 and error is None:
        error = {"type": "HTTPStatusError", "message": "HTTP %s" % status_code}
    return {
        "callId": str(response_id or uuid4()),
        "traceId": normalized_headers.get("x-session-id") or str(request_body.get("session_id") or ""),
        "provider": "proxy",
        "model": model,
        "startTime": started_at,
        "endTime": ended_at,
        "latency": latency,
        "tokenUsage": usage_from_response(response_body if isinstance(response_body, dict) else {}),
        "status": status,
        "error": error,
    }


def append_jsonl(path: Path, item: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False))
        handle.write("\n")


def import_proxy_log(log_path: Path, run_dir: Path) -> dict[str, Any]:
    window = _session_time_window(run_dir)
    calls = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if isinstance(item, dict) and _call_in_window(item, window):
            calls.append(item)
    payload = {"llm_calls": calls}
    target = run_dir / "llm_calls.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def import_configured_proxy_log(run_dir: Path, source: str | None = None) -> dict[str, Any] | None:
    configured = (source if source is not None else _configured_proxy_log_source()).strip()
    if not configured:
        return None
    if _looks_like_remote_path(configured):
        return _import_remote_proxy_log(configured, run_dir)
    path = Path(configured).expanduser()
    if not path.exists():
        return None
    return import_proxy_log(path, run_dir)


def _configured_proxy_log_source() -> str:
    value = os.getenv("AUDITRAIL_LLM_PROXY_LOG", "").strip()
    if value:
        return value
    # The project ``.env`` is a developer convenience, not a runtime contract.
    # ``AUDITRAIL_LOAD_DOTENV`` disables reading it: the test suite sets it so a
    # local ``.env`` (which may name a remote host) cannot make a test scp.
    if str(os.getenv("AUDITRAIL_LOAD_DOTENV", "true")).strip().lower() in _DOTENV_DISABLED:
        return ""
    env_path = ENV_FILE
    if not env_path.exists():
        return ""
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        if key.strip() == "AUDITRAIL_LLM_PROXY_LOG":
            return raw_value.strip().strip("'\"")
    return ""


def _import_remote_proxy_log(source: str, run_dir: Path) -> dict[str, Any] | None:
    with tempfile.NamedTemporaryFile(prefix="auditrail-proxy-", suffix=".jsonl") as handle:
        try:
            subprocess.run(["scp", "-q", source, handle.name], check=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            return None
        return import_proxy_log(Path(handle.name), run_dir)


def _looks_like_remote_path(value: str) -> bool:
    if value.startswith("/") or value.startswith(".") or value.startswith("~"):
        return False
    return ":" in value


def _session_time_window(run_dir: Path) -> tuple[datetime, datetime] | None:
    session_path = run_dir / "session.json"
    if not session_path.exists():
        return None
    try:
        payload = json.loads(session_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    session = payload.get("session") if isinstance(payload, dict) else None
    if not isinstance(session, dict):
        session = payload if isinstance(payload, dict) else {}
    start = _datetime_from_epoch_millis(session.get("created_at"))
    end = _datetime_from_epoch_millis(session.get("updated_at"))
    if start is None or end is None:
        return None
    return start - timedelta(seconds=5), end + timedelta(seconds=5)


def _call_in_window(item: dict[str, Any], window: tuple[datetime, datetime] | None) -> bool:
    if window is None:
        return True
    started = _datetime_from_iso(item.get("startTime"))
    if started is None:
        return False
    start, end = window
    return start <= started <= end


def _datetime_from_epoch_millis(value: Any) -> datetime | None:
    try:
        timestamp = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc)


def _datetime_from_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _forward_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {"authorization", "content-type", "accept"}
    return {key: value for key, value in headers.items() if key.lower() in allowed}


def _upstream_path(path: str) -> str:
    clean = "/" + path.lstrip("/")
    if clean == "/v1":
        return "/"
    if clean.startswith("/v1/"):
        return clean[3:]
    return clean


def _json_or_empty(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _body_with_stream_usage(raw: bytes) -> tuple[bytes, dict[str, Any]]:
    payload = _json_or_empty(raw)
    if not payload:
        return raw, {}
    if payload.get("stream") is True:
        stream_options = payload.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        if stream_options.get("include_usage") is not True:
            payload = dict(payload)
            payload["stream_options"] = {**stream_options, "include_usage": True}
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return raw, payload


def _response_payload_for_metrics(headers: dict[str, str], raw: bytes) -> dict[str, Any]:
    content_type = ""
    for key, value in headers.items():
        if key.lower() == "content-type":
            content_type = value.lower()
            break
    if "text/event-stream" in content_type:
        return _payload_from_sse(raw)
    payload = _json_or_empty(raw)
    if payload:
        return payload
    if raw.lstrip().startswith(b"data:"):
        return _payload_from_sse(raw)
    return {}


def _payload_from_sse(raw: bytes) -> dict[str, Any]:
    last_payload: dict[str, Any] = {}
    usage_payload: dict[str, Any] = {}
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        last_payload = payload
        if isinstance(payload.get("usage"), dict):
            usage_payload = payload
    if usage_payload:
        return usage_payload
    return last_payload


def _int_value(payload: dict[str, Any], *keys: str) -> int:
    """Return the first present, parseable alias value; 0 when none exists.

    Each alias key must actually be tried: returning inside the loop on the
    first key would make the fallback aliases dead code and zero-fill usages
    that only report a non-first alias.
    """
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
