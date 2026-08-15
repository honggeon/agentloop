# agentloop

`agentloop` is a small black-box trajectory collection and audit tool for `xuanjian-agent`.

It does not require instrumentation in the agent service. The first version talks to the deployed HTTP API, exports normalized trajectory files, and runs deterministic checks over tool usage.

## Install

```bash
cd /Users/chen/agentloop
python3 -m pip install -e .
```

## Fetch an Existing Session

```bash
python3 -m agentloop.cli fetch-session \
  --base-url http://192.168.1.238:8788 \
  --user-id 54775da4-0190-4d67-90e4-32d4a0334571 \
  --session-id 5ab7b3b0-1659-4c30-9735-1d54da1548fd
```

Outputs:

- `runs/<run_id>/session.json`
- `runs/<run_id>/summary.json`
- `runs/<run_id>/trajectory.jsonl`
- `runs/<run_id>/audit.json`
- `runs/<run_id>/tool_audit.md`

## Run a Smoke Chat

```bash
python3 -m agentloop.cli smoke-chat \
  --base-url http://192.168.1.238:8788 \
  --user-id agentloop-test \
  --message "请列出当前工作目录文件"
```

## DeepAgent Analysis

Install optional dependencies:

```bash
python3 -m pip install -e '.[agent]'
```

Configure a model provider:

```bash
export MODEL_PROVIDER=openai
export MODEL_NAME=gpt-4.1
export OPENAI_API_KEY=...
```

Judge an exported run:

```bash
python3 -m agentloop.cli judge-run --run runs/<run-dir>
```

The command writes:

- `agent_judge.json`
- `agent_judge.md`
- `chart_data.json`

The existing deterministic commands do not require DeepAgent dependencies or model credentials.

## Web Dashboard

Create local config:

```bash
cp .env.example .env
```

Start backend:

```bash
uv run uvicorn backend.app.main:app --host 0.0.0.0 --port 18080 --reload
```

Start frontend:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

Open:

```text
http://localhost:5173
```

For LAN access, replace `localhost` with the host machine IP.

## Auth

For black-box testing, `X-User-Id` is enough to satisfy the current API login check. Use `--token` only when testing JWT-backed user gateway/model authorization.
