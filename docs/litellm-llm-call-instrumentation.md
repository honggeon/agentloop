# LiteLLM LLM Call Instrumentation

This project can display exact LLM token and latency metrics when the agent service exports `llm_calls`.

## Data Flow

```text
agent service LLM client
  -> auditrail metrics proxy
  -> local model /chat/completions
  -> response.usage
  -> proxy JSONL log
  -> auditrail import-llm-calls
  -> runs/<run_id>/llm_calls.json
  -> dashboard runtime metrics
```

When the agent service cannot be changed, put a small OpenAI-compatible proxy between the agent service and the local model endpoint. The proxy records one line per LLM request and forwards the original response back to the agent service.

## Agent Service Hook Point

The best long-term hook point is still the single model wrapper used by the agent service, not each agent node. The wrapper should record start/end time around the LiteLLM request and copy the `usage` object returned by the model API.

```python
from datetime import datetime, timezone
from time import perf_counter
from uuid import uuid4


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def call_litellm_with_metrics(client, *, session_id, model, messages, **kwargs):
    call_id = f"llm-{uuid4().hex}"
    started_at = utc_now()
    started = perf_counter()
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            **kwargs,
        )
        finished_at = utc_now()
        usage = getattr(response, "usage", None) or {}
        llm_call = {
            "callId": call_id,
            "traceId": session_id,
            "provider": "LiteLLM",
            "model": model,
            "startTime": started_at,
            "endTime": finished_at,
            "latency": round(perf_counter() - started, 3),
            "tokenUsage": {
                "inputTokens": int(getattr(usage, "prompt_tokens", 0) or usage.get("prompt_tokens", 0) or 0),
                "outputTokens": int(getattr(usage, "completion_tokens", 0) or usage.get("completion_tokens", 0) or 0),
                "totalTokens": int(getattr(usage, "total_tokens", 0) or usage.get("total_tokens", 0) or 0),
            },
            "status": "success",
            "error": None,
        }
        record_llm_call(session_id, llm_call)
        return response
    except Exception as exc:
        finished_at = utc_now()
        record_llm_call(
            session_id,
            {
                "callId": call_id,
                "traceId": session_id,
                "provider": "LiteLLM",
                "model": model,
                "startTime": started_at,
                "endTime": finished_at,
                "latency": round(perf_counter() - started, 3),
                "tokenUsage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0},
                "status": "failed",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            },
        )
        raise
```

`record_llm_call()` should persist the object into the session record. The session API can return either:

```json
{
  "session": {
    "session_id": "s1",
    "messages": [],
    "llm_calls": []
  }
}
```

or:

```json
{
  "session": {"session_id": "s1", "messages": []},
  "llm_calls": []
}
```

Auditrail accepts both `llm_calls` and `llmCalls`.

## No-Code Proxy Path

Use this path when `xuanjian-agent` code cannot be modified.

Start the proxy on the agent host:

```bash
uv run auditrail metrics-proxy \
  --upstream-base-url http://127.0.0.1:7500/v1 \
  --host 127.0.0.1 \
  --port 7600 \
  --log runs/proxy-llm-calls.jsonl
```

If the remote host does not have `uv`, install-free startup is also supported after copying this repository to `/home/newuser/agentloop`:

```bash
cd /home/newuser/agentloop
mkdir -p runs
nohup env PYTHONPATH=/home/newuser/agentloop \
  python3 -m auditrail.cli metrics-proxy \
  --upstream-base-url http://127.0.0.1:7500/v1 \
  --host 127.0.0.1 \
  --port 7600 \
  --log /home/newuser/agentloop/runs/proxy-llm-calls.jsonl \
  > /home/newuser/agentloop/runs/metrics-proxy.out 2>&1 &
echo $! > /home/newuser/agentloop/runs/metrics-proxy.pid
```

Stop the background proxy with:

```bash
kill "$(cat /home/newuser/agentloop/runs/metrics-proxy.pid)"
```

Point the agent's OpenAI-compatible base URL at the proxy:

```env
OPENAI_BASE_URL=http://127.0.0.1:7600/v1
```

Keep the original local model endpoint as the proxy upstream:

```text
proxy http://127.0.0.1:7600/v1 -> model http://127.0.0.1:7500/v1
```

After a test run has been exported into `runs/<run_id>`, import the captured calls:

```bash
uv run auditrail import-llm-calls \
  --run runs/<run_id> \
  --log runs/proxy-llm-calls.jsonl
```

For automatic import during `fetch-session`, `smoke-chat`, and dashboard live evaluation, configure the dashboard/backend process with:

```env
AUDITRAIL_LLM_PROXY_LOG=newuser@192.168.0.64:/home/newuser/agentloop/runs/proxy-llm-calls.jsonl
```

Local file paths are also supported:

```env
AUDITRAIL_LLM_PROXY_LOG=/home/newuser/agentloop/runs/proxy-llm-calls.jsonl
```

When this variable is set, Auditrail imports only the proxy calls whose `startTime` falls inside the run's `session.created_at` / `session.updated_at` window, with a small clock-skew allowance. This prevents older test calls from being mixed into a new run.

This writes:

```text
runs/<run_id>/llm_calls.json
```

Then refresh the dashboard run page. The runtime metrics chart will use exact token counts from the model response `usage` field and proxy-measured latency.

## Validation

Before changing the remote `.env`, validate the proxy with one direct model call:

```bash
curl -s http://127.0.0.1:7600/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <same-key-as-agent>' \
  -d '{
    "model": "xuanjian-lite",
    "messages": [{"role": "user", "content": "ping"}],
    "stream": false
  }'
```

Expected checks:

- The response body matches the normal local model API shape.
- The response contains `usage.prompt_tokens`, `usage.completion_tokens`, and `usage.total_tokens`.
- `runs/proxy-llm-calls.jsonl` receives one JSON line.
- The JSON line does not contain the API key.

For `stream: true` requests, the proxy automatically adds:

```json
{"stream_options": {"include_usage": true}}
```

It then parses the returned SSE `data: {...}` chunks and records the final chunk that contains `usage`. This requires an upstream OpenAI-compatible server that supports stream usage reporting. The current remote vLLM endpoint has been validated to return final usage when `stream_options.include_usage=true`.

## Remote Configuration Impact

The inspected remote config currently points local text model calls at:

```env
OPENAI_BASE_URL=http://127.0.0.1:7500/v1
```

Changing it to:

```env
OPENAI_BASE_URL=http://127.0.0.1:7600/v1
```

does not change the model, API key, request payload, or application code. It only inserts the metrics proxy on the request path.

Expected impact:

- One extra local HTTP hop, usually a small latency increase.
- If the proxy process is down, text model calls through `OPENAI_BASE_URL` fail.
- Token data becomes per-call and exportable into Auditrail.
- CloudManager usage still only reflects traffic that actually goes through CloudManager or LiteLLM; local `xuanjian-agent` calls are captured by the proxy log instead.

Rollback is:

```env
OPENAI_BASE_URL=http://127.0.0.1:7500/v1
```

and restart the agent service if it only reads `.env` at process startup.

## CloudManager / LiteLLM Interfaces

The CloudManager test manual shows these endpoints:

- `POST /api/auth/token` to obtain the CloudManager token.
- `GET /api/litellm/me/access` to obtain model `base_url` and `api_key`.
- `POST <base_url>/chat/completions` to call `xuanjian-lite` or `xuanjian-pro`.
- `GET /api/litellm/me/usage` to inspect account-level credit usage.

For exact per-call token metrics, use the `usage` field from `/chat/completions`. For latency, use agent service start/end timestamps. For local deployment cost, either show token metrics without monetary cost or calculate internal cost from your own model/GPU pricing table.
