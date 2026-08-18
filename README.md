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

## Evaluate DeepSeek Harness (DSH) Agents

agentloop can audit locally recorded DeepSeek Harness sessions. DSH writes every
agent session to `~/.dsh/sessions/<workspace>/<session-id>/session.jsonl.zstd`;
`agentloop/dsh_source.py` converts those event logs into the standard run
contract, so audit, judge and dashboard work unchanged.

```bash
# List recorded DSH sessions (newest first)
python3 -m agentloop.cli list-dsh-sessions

# Import one session as a run directory, then judge it
python3 -m agentloop.cli import-dsh-session --session-id session-<uuid>
python3 -m agentloop.cli judge-run --run runs/<run-dir>
```

Backend routes (used by the DSH plugin):

- `GET /api/dsh/sessions?limit=50` — list recorded DSH sessions.
- `POST /api/dsh/import` `{ "session_id": "session-<uuid>" }` — import + audit,
  returns `{ "run_id": ... }`. `DSH_HOME` env var overrides the default `~/.dsh`.

A DSH web plugin that embeds this flow into the DSH settings page (设置 →
Agent 评测) lives in `dsh-plugin/`; see `dsh-plugin/README.md`.

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

`agent_judge.json` includes two reliability-focused sections:

- `dimensions`: structured scores for intent recognition, task planning, tool selection, parameter generation, evidence citation, state tracking, final response, context management, execution closure, error recovery, verification, safety/permission, and efficiency. Missing evidence uses `unknown`, not fail.
- `judge_reliability`: deterministic checks over the judge output, including evidence coverage, unsupported claims, invalid evidence references, fact claims with no valid evidence, and `invalid_evidence` detail.

Deterministic checks validate evidence against `summary.json`, `audit.json`, `trajectory.jsonl`, and `session.json` when available. This round scores `evidence_citation` and `execution_closure`, caps `error_recovery` only when a failure occurred, and caps `tool_selection` only when a tool failed. If no failure occurred, `error_recovery` remains `unknown`. LLM judge conclusions remain useful for semantic assessment, but unsupported factual claims are surfaced in reliability metrics. Completion still follows `assess_run_quality` deliverable checks.

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
