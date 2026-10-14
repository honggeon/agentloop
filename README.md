# auditrail

`auditrail` is a small black-box trajectory collection and audit tool for `xuanjian-agent`.

It does not require instrumentation in the agent service. The first version talks to the deployed HTTP API, exports normalized trajectory files, and runs deterministic checks over tool usage.

> 项目名是 `auditrail`（Python 包、CLI 模块、环境变量前缀）；本地 checkout 目录沿用历史的 `/Users/chen/agentloop`，下文的绝对路径都指这个目录。
> 运行时环境变量统一使用 `AUDITRAIL_` 前缀：`AUDITRAIL_RUNS_DIR`、`AUDITRAIL_LLM_PROXY_LOG`。

## Install

```bash
cd /Users/chen/agentloop
python3 -m pip install -e .
```

## Fetch an Existing Session

```bash
python3 -m auditrail.cli fetch-session \
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

auditrail can audit locally recorded DeepSeek Harness sessions. DSH writes every
agent session to `~/.dsh/sessions/<workspace>/<session-id>/session.jsonl.zstd`;
`auditrail/dsh_source.py` converts those event logs into the standard run
contract, so audit, judge and dashboard work unchanged.

```bash
# List recorded DSH sessions (newest first)
python3 -m auditrail.cli list-dsh-sessions

# Import one session as a run directory, then judge it
python3 -m auditrail.cli import-dsh-session --session-id session-<uuid>
python3 -m auditrail.cli judge-run --run runs/<run-dir>
```

Backend routes (used by the DSH plugin):

- `GET /api/dsh/sessions?limit=50` — list recorded DSH sessions. Sessions that
  belong to the configured judge profile/workspace are hidden from this list.
- `POST /api/dsh/import` `{ "session_id": "session-<uuid>" }` — import + audit,
  returns `{ "run_id": ... }`. `DSH_HOME` env var overrides the default `~/.dsh`.

A DSH web plugin that embeds this flow into the DSH settings page (设置 →
Agent 评测) lives in `dsh-plugin/`; see `dsh-plugin/README.md`.

### Judge engines

`POST /api/runs/{run_id}/judge` accepts an optional
`{ "engine": "deepagent" | "dsh" }` body and defaults to `deepagent`.
`GET /api/judge/engines` (mirrored in `GET /api/dsh/options` as `judge`) lists the
registered engines and whether each one is ready. Both engines return an
untrusted proposal; normalization, evidence validation, deterministic dimension
caps, and report writing always go through `write_judge_report`, so a semantic
judge can never override a deterministic failure. `agent_judge.json` records the
producing engine under the additive `judge` key.

Phase-one local Headless execution is disabled by default and has no public HTTP
route yet. Its server-owned policy is configured with `DSH_RUNTIME_ENABLED`,
`DSH_BINARY`, `DSH_ALLOWED_PROFILES`, `DSH_ALLOWED_WORKSPACE_ROOTS`,
`DSH_DEFAULT_TIMEOUT_SECONDS`, `DSH_MAX_TIMEOUT_SECONDS`, and
`DSH_MAX_CONCURRENCY`. Enabling it requires an absolute fixed binary path plus
non-empty profile and workspace-root allowlists; callers cannot supply raw argv,
`--patch`, or environment overrides. Phase-one local execution requires POSIX
process-group isolation so timeout and cancellation also terminate spawned tool
processes. See `.env.example` for safe defaults.

The DSH judge stays disabled until it is configured separately with
`DSH_JUDGE_PROFILE` (a dedicated profile name that must differ from every
evaluand profile) and `DSH_JUDGE_WORKSPACE` (a dedicated directory strictly
beneath an allowed workspace root). The judge turn receives bounded run evidence
in its prompt, gets no shell or report-writing tools, and its sessions are hidden
from evaluand discovery.

## Run a Deterministic Case Suite

`cases/` holds machine-readable acceptance-case suites. A case declares the argv
to run, an optional file fixture, optional environment overrides, and the
expected outcome; the runner turns every expectation into an explicit check and
reports a verdict.

```bash
# Run the DSH launcher suite, write cases/results/<suite>-<stamp>.{json,md}
python3 -m auditrail.cli run-cases --suite cases/dsh-cli.json

# Only one case or one module; print without writing reports
python3 -m auditrail.cli run-cases --suite cases/dsh-cli.json --only DSH-CLI-002 --no-write

# Point the suite at another binary
python3 -m auditrail.cli run-cases --suite cases/dsh-cli.json --bin ./node_modules/.bin/dsh
```

Exit code is `0` when the suite passes and `1` otherwise, so it can gate CI.
Verdicts: `passed`, `failed`, `xfail` (a known defect, asserted as it *should*
behave — recorded under `defect` and not counted as a failure), `xpass` (a known
defect that now passes: the suite must be updated, so this fails the run), and
`error` (timeout or spawn failure — never absorbed by `xfail`, because a broken
environment must stay visible).

Expectations in `cases/dsh-cli.json` were captured from real runs, not read off
documentation. `xfail` cases there assert behaviour the launcher should have;
they fail on `0.1.2-rc.1` and name the defect. Fix the defect and drop the
`xfail` flag rather than deleting the case.

Supported case fields: `args` (argv, `{tmp}` expands to the fixture dir),
`files` (fixture files), `env` (environment overrides, e.g. an isolated
`DSH_HOME={tmp}/home` so profile-mutating cases never touch the operator's real
`~/.dsh`), `timeout_s`, `xfail`/`defect`, and `expect`.

Supported `expect` keys: `exit_code` (integer, `{"in": [...]}`, or `{"not": N}`),
`stdout_empty`/`stderr_empty`, `stdout_contains`/`stderr_contains` and their
`_not_` forms, `stdout_matches`/`stderr_matches` plus `_not_matches` for
multiline regexes, and `file_exists`/`file_not_exists` for workspace-relative
paths checked after the run (e.g. an auto-initialized profile directory). An
unknown key is a hard error: a silently ignored typo would turn an assertion
into a no-op that always passes.

## Run an Agent Behaviour Suite

`cases/dsh-agent.json` goes one step further: each case runs one headless agent
task and asserts against the **DSH session recording it produces**. After the
argv completes, the runner diffs the session recordings under the case's DSH
home against a pre-run snapshot, converts the newest new recording with
`dsh_source`, audits it with the deterministic tool audit, and checks the
case's `session` block:

```bash
python3 -m auditrail.cli run-cases --suite cases/dsh-agent.json
```

Supported `session` keys: `recorded`, `trajectory_contains` /
`trajectory_not_contains` / `trajectory_matches` / `trajectory_not_matches`
(over the flattened message text), `uses_tool` / `not_uses_tool`,
`tool_calls_min` / `tool_calls_max`, `no_missing_tool_results`,
`no_orphan_tool_results`, `no_tool_errors`, and `final_answer`.

This covers the expectations that only a real agent run can produce — sandbox
denials (`Operation not permitted` / `file access denied`), path-traversal
blocking, session persistence — with the same verdict/report machinery as the
argv suite. These cases need model credentials, burn a small amount of model
quota, and record into the operator's real `~/.dsh/sessions`, so they live in
their own suite file.

## Run a Smoke Chat

```bash
python3 -m auditrail.cli smoke-chat \
  --base-url http://192.168.1.238:8788 \
  --user-id auditrail-test \
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
python3 -m auditrail.cli judge-run --run runs/<run-dir>
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

The frontend dev server proxies `/api` to the backend (default `http://localhost:18080`,
overridable via `VITE_API_TARGET`), so requests are same-origin and any frontend port works
without CORS issues. Only set `VITE_API_BASE_URL` when the browser must talk to a remote
backend directly, and add that origin to `CORS_ORIGINS` in that case.

Open:

```text
http://localhost:5173
```

For LAN access, replace `localhost` with the host machine IP.

## Auth

For black-box testing, `X-User-Id` is enough to satisfy the current API login check. Use `--token` only when testing JWT-backed user gateway/model authorization.
