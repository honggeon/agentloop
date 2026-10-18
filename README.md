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
contract, so audit, judge and dashboard work unchanged. When the recording
carries provider-reported per-step `usage`, import writes a real
`llm_calls.json` (input/output/cache tokens and step latency); runs imported
before that support can be repaired in place with `render-run`, which recovers
the usage from the raw `dsh_events` stored in `session.json`.

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

### Multiple judge reports

One run keeps one report per engine side by side:

```text
runs/<run_id>/
  agent_judge.json|md, chart_data.json   # active report, unchanged contract
  judges/
    manifest.json
    deepagent/agent_judge.json|md, chart_data.json
    dsh/agent_judge.json|md, chart_data.json
```

The root files stay the active report, so old clients keep working. Switching the
active report only makes another engine's stored report current; it does not
re-run or re-normalize anything. Every stored report went through
`write_judge_report`, files are published atomically, and a per-run lock keeps
concurrent judges from interleaving. Runs judged before this layout still load:
without `judges/manifest.json`, the root report is exposed as the single active
`deepagent` report.

- `GET /api/runs/{run_id}/judges` — manifest: active engine and every stored report.
- `GET /api/runs/{run_id}/judges/{engine}` — one engine's stored report.
- `GET /api/runs/{run_id}/judges/compare` — per-engine status, completion,
  reliability, and dimension rows, plus the disagreements between engines.
- `POST /api/runs/{run_id}/judges/{engine}/active` — make a stored report active again.

Comparison never picks a winner: `unknown` ranks below a real judgment, so a
dimension one engine never assessed is reported as an open question instead of a
disagreement or a failure, and deterministic failures stay visible even when a
semantic judge passes the run.

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

### Local DSH ACP runtime

The ACP transport (ACP v1 over stdio: NDJSON framing plus JSON-RPC 2.0) powers
interactive sessions: one long-lived `dsh --profile acp` process owned by the
backend, with session creation/resume, prompts, streamed updates, one-shot
permission answers, cancellation, and close. It is disabled by default and needs
**both** switches: `DSH_RUNTIME_ENABLED=true` (the master switch, which also
enables Headless) **and** `DSH_ACP_ENABLED=true` (the ACP opt-in, whose default is
`false`). Either switch alone leaves ACP off — turning on the master switch does
not implicitly expose it, and the ACP switch cannot work without the master switch
and the server-owned binary/profile/workspace allowlist. `/api/dsh/options`
reports the effective state as `dsh.acp.enabled`. The profile is fixed to `acp`;
callers never pass argv or environment. Local execution stays single-worker: one
active prompt per session, and `DSH_MAX_CONCURRENCY` bounds the whole runtime.

| Route | Purpose |
|---|---|
| `GET /api/dsh/acp/options` | Live negotiated capabilities and the permission policy |
| `POST /api/dsh/acp/session/new` | Create a session in an allowed workspace |
| `POST /api/dsh/acp/session/resume` | Resume a persisted session (canonical workspace re-verified) |
| `GET /api/dsh/acp/sessions` | Sessions this backend currently owns |
| `POST /api/dsh/acp/prompt` (or `/session/{session_id}/prompt`) | One prompt turn, streamed as SSE |
| `POST /api/dsh/acp/cancel` (or `/operation/{operation_id}/cancel`) | Cancel one operation |
| `POST /api/dsh/acp/permission/respond` (or `/operation/{operation_id}/permission`) | Answer one permission request |
| `GET /api/dsh/acp/operation/{operation_id}` | Operation state, error, and imported `run_id` |
| `DELETE /api/dsh/acp/session/{session_id}` (or `/session/{session_id}/close`) | Close one session |

`POST /api/dsh/acp/prompt` takes `{"session_id", "message", "acceptance_criteria"}`
and returns `text/event-stream`. Validation and registry conflicts (`400` / `404`
/ `409` / `503`) are ordinary JSON errors before the stream starts; everything
that can only fail while streaming is an `event: error`. Events, in order:

```text
started              { operation_id, session_id }
runtime_event        { type, data [, phase, source_type, truncated] }
permission_requested { request_id, session_id, tool_call_id, options, operation_id }
completed            { session_id, stop_reason, text, cancelled }
run_imported         { run_id, session_id }
error                { code, detail [, stage, session_id] }
```

`completed` reports the ACP turn settlement, not success: `stopReason` folds
success and several failure modes into `end_turn`. Exactly one terminal event
follows it. `run_imported` appears when the authoritative DSH session recording
was imported as a run directory; an `error` with `stage="import"` means no run was
published, the operation state is `failed`, and the session id stays available for
recovery — a turn never looks successful without a run. `permission_requested`
arrives while `session/prompt` is still blocked and is answered with
`allow-once` or `reject-once`; cancel, a prompt timeout, or a client disconnect
answers any outstanding request as cancelled so DSH is never left waiting.
Imported runs carry `interaction_mode=auditrail-chat`, `dsh_profile=acp`,
`session_role=evaluand`, and `expectations.json` from the request's acceptance
criteria.

The runtime evaluation page (`/live`) offers three runtime providers: the remote
agent HTTP service, Local DSH (Headless), and **Local DSH ACP**. The ACP provider
is the interactive workbench for the long-lived process: it reads the negotiated
capabilities from `/api/dsh/acp/options`, creates/resumes/closes sessions, streams
one prompt turn per session, and surfaces each permission request as a two-button
`allow-once` / `reject-once` decision instead of an automatic approval. The panel
keeps the distinction the SSE contract makes: a settled turn (`completed`, with
its `stop_reason`) is never rendered as success, and only `run_imported` shows as
a published Auditrail run.

### Turn-owned ACP updates

A prompt turn and a long-lived projection layer need the same `session/update`
notifications. Draining one shared inbox from both sides split a turn between
whichever consumer won the race, so the settled outcome could come back
incomplete. `AcpTransport.open_session_updates(session_id)` instead gives the turn
its own bounded, ordered subscription: while it is open, that session's updates go
to the subscription and only to the subscription, frames already queued are
claimed, and `AcpClient.prompt(..., on_event=...)` reports each normalized event in
wire order. `outcome.updates` is therefore the complete turn, and a projection
layer cannot steal chunks from it.

A cancelled turn is never reported as success. `end_turn` covers several outcomes
in DSH's mapping, so the caller's cancel signal wins over the peer's result:
`outcome.cancelled` is true and `stop_reason` is `cancelled` whenever the turn was
cancelled locally, even if the peer folded it into a normal `end_turn` response.
Otherwise an aborted turn would be imported as a durable, evaluated run. A peer
that reports cancellation itself (JSON-RPC `-32800`) is mapped to the same
cancelled outcome rather than a failure.

### Operating the DSH integration

Enabling the runtime, startup/shutdown order, capacity and backpressure bounds,
the opt-in real-DSH smoke tests, and a symptom-to-cause troubleshooting table are
documented in [`docs/dsh-operations.md`](docs/dsh-operations.md). The security and
reliability audit behind the current hardening, with its residual risks, is in
[`docs/reports/2026-10-14-dsh-release-readiness.md`](docs/reports/2026-10-14-dsh-release-readiness.md);
the secure-default patch that makes ACP opt-in, with its acceptance evidence, is in
[`docs/reports/2026-10-14-dsh-release-candidate.md`](docs/reports/2026-10-14-dsh-release-candidate.md).

`AUDITRAIL_LOAD_DOTENV=0` disables reading the project `.env`. The test suite sets
it (forced) so a developer's local `.env` cannot steer a test run or make it reach
the network; production should leave it enabled.

The optional real-binary smoke tests are excluded from the default run and skip
unless explicitly enabled:

```bash
AUDITRAIL_DSH_INTEGRATION=1 DSH_BINARY="$(command -v dsh)" \
  python3 -m pytest tests/test_dsh_integration.py -q
```

They drive the real Headless, ACP, and cancellation paths. A DSH home is derived
under the pytest temporary directory by default, so the operator's `~/.dsh` is
never written to; the tests do consume model quota.


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
- `judges/deepagent/` plus `judges/manifest.json` when the report carries engine metadata

`agent_judge.json` includes two reliability-focused sections:

- `dimensions`: structured scores for intent recognition, task planning, tool selection, parameter generation, evidence citation, state tracking, final response, context management, execution closure, error recovery, verification, safety/permission, and efficiency. Missing evidence uses `unknown`, not fail.
- `judge_reliability`: deterministic checks over the judge output, including evidence coverage, unsupported claims, invalid evidence references, fact claims with no valid evidence, and `invalid_evidence` detail. Dimension-level integrity is reported via `unsupported_dimension_count`, `dimension_evidence_mismatch_count` and `dimensions_without_valid_evidence`.

Each dimension may additionally carry an `evidence_chain` (additive and optional; old reports without it stay readable):

- `checks`: atomic criteria the verdict rests on — `{id, criterion, result (pass|partial|fail|unknown), conclusion, type (fact|inference), evidence, counter_evidence}`. `conclusion` is a short auditable statement, never hidden chain-of-thought.
- `score_trace`: how the score moved — `judge_score`/`judge_status` (the judge's raw submission), `deterministic_score` (the deterministic candidate), `final_score`, and `rules` (`{id, action: set|cap|keep, before, candidate, after, reason, evidence}`). A `keep` rule records that a deterministic rule ran and deliberately left the judge score unchanged.
- `gaps`: what evidence is still missing.

The dimension's top-level `evidence` remains the compatibility view: the original references plus the chain's supporting references (check evidence and rule evidence), deduplicated. Counter evidence stays inside the chain. Deterministic checks validate evidence against `summary.json`, `audit.json`, `trajectory.jsonl`, `session.json`, `expectations.json`, and `evaluation.json` when available. Quote validation is mechanical verbatim matching only: a valid quote proves the reference exists in the artifact, never that the conclusion follows semantically.

Scoring authority: deterministic checks outrank the LLM judge. This round scores `evidence_citation` and `execution_closure`, caps `error_recovery` only when a failure occurred, and caps `tool_selection` only when a tool failed; every set/cap/keep is recorded in `score_trace`. If no failure occurred, `error_recovery` remains `unknown`. Shell tool results marked with a non-zero `[exit code: N]` (or a structured `exit_code`/`is_error` field) are audited as failures; a zero exit code or bare error keywords in otherwise successful output are not. A judge dimension scored pass/partial/fail without any mechanically valid evidence is normalized to `score=null, status=unknown, source=judge` with the gap recorded — `unknown` means "assessed but insufficient evidence", never a disguised fail. Completion still follows `assess_run_quality` deliverable checks, and quality findings go through the same evidence validation as judge findings.

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

## Agent Test Platform (Phase 0)

The platform slice manages HTTP/SSE Agents and unified TestCases, and runs a
single asynchronous case through the API/Web console (sidebar「测试平台」).

- `GET/POST /api/agents` — register an Agent (`adapter=http_session`, endpoint,
  capabilities). Endpoints pass SSRF validation: http/https only, no URL
  credentials, allowlisted ports, and no loopback/private/link-local/metadata
  addresses unless explicitly allowed (`AGENT_ALLOWED_HOSTS`,
  `AGENT_ALLOWED_PORTS`, `AGENT_ALLOW_PRIVATE_ENDPOINTS`).
- `GET/POST /api/test-cases`, `GET /api/test-cases/{case_id}` — unified
  TestCases; legacy `cases/*.json` suites are exposed read-only as
  `adapter=command` (executed by `run-cases`, not by the platform runner).
- `POST /api/runs` — `202 Accepted` with `{run_id, status, status_url}`;
  executes in the background (`queued -> running -> completed|failed|timed_out`).
  `Idempotency-Key` replays the same request to the original run and conflicts
  (`409`) on a different body.
- `GET /api/runs/{run_id}/status` — the `run.json` safe subset; stale
  queued/running records are flagged instead of pretending to progress.

Runs write the Run Contract v1 `run.json` (platform state only) plus the
existing artifacts (`session.json`, `summary.json`, `trajectory.jsonl`,
`audit.json`, ...) and a versioned `evaluation.json` envelope; pre-collection
failures also write `error.json`. All artifact writes are atomic
(temp file + fsync + replace). A blocking deterministic failure caps the judge
`completion_score` (missing artifact 40, blocking check/required expectation 60,
minimum wins) and forces judge `overall_status=failed` — an LLM judge cannot
override it; dimensions without evidence stay `unknown` with a null score.

## Agent Test Platform (Phase 1): Test Suites and Batch Runs

Phase 1 adds TestSuite CRUD and sequential batch execution on the same
platform page (sidebar「测试平台」), following the model
`SuiteJob -> CaseAttempt -> Run`:

- `GET/POST /api/test-suites`, `GET/PUT/DELETE /api/test-suites/{suite_id}` —
  a TestSuite is `{suite_id, version, title, description, case_ids, enabled,
  created_at, updated_at}`; `case_ids` defines the execution order, updates
  bump `version`, and unknown case references are rejected at save time.
- `POST /api/test-suites/{suite_id}/runs` — `202 Accepted` with
  `{job_id, status, status_url}`; launches one SuiteJob that runs each case
  in order against one Agent, producing an independent Run per case (the
  run's `run.json.suite_ref` links back to the job and attempt).
- `GET /api/suite-jobs`, `GET /api/suite-jobs/{job_id}` — job status
  (`queued/running/completed/failed/cancelled`), per-case attempt status
  (Run lifecycle, including `skipped`), Run links, deterministic evaluation
  per finished case, and stats counted by final case status.
- `POST /api/suite-jobs/{job_id}/cancel` — cooperative cancellation: the
  in-flight case finishes as `cancelled`, cases that have not started are
  marked `skipped`, and completed Runs keep all artifacts.

A failed case never blocks the rest of the suite. Stats keep `skipped` and
`unknown` quality separate from passes — the pass-rate denominator only
includes completed cases. SuiteJob records live in
`platform/suite_jobs/<job_id>.json` and are written atomically like all other
platform files.

## Agent Test Platform (Phase 1.1): Batch Reliability and Scheduling

Phase 1.1 hardens batch execution to `SuiteJob -> CaseExecution -> Attempt N
-> Run N` — every retry is a new attempt bound to a new Run, and old Runs keep
their evidence untouched:

- **Idempotent launch** — `POST /api/test-suites/{suite_id}/runs` accepts an
  `Idempotency-Key` header; the same key with the same request replays to the
  original job (`idempotent_replay: true`), a different request conflicts
  (`409 idempotency_conflict`).
- **Retry** — `max_retries` (0–5) retries failed/timed-out cases whose error
  is retryable (agent timeout/unreachable/5xx/stream errors, executor
  interruptions); validation and definition errors are never retried.
- **Concurrency caps** — static ceilings, no autoscaling: a global cap
  (`AUDITRAIL_PLATFORM_MAX_CONCURRENCY`, default 4) plus a per-Agent cap
  (Agent config `max_concurrency`, falling back to
  `AUDITRAIL_PLATFORM_AGENT_MAX_CONCURRENCY`, default 1). The caps in effect
  at launch are snapshotted into the job's `limits`.
- **Timeouts** — each case Run is watchdogged against its `timeout_seconds`
  (cooperative cancel first, forced `timed_out` after a grace period); the
  job-level `timeout_seconds` is a suite-wide deadline that cancels the
  in-flight case, skips pending cases and fails the job with `suite_timeout`.
- **Budgets** — `budget.max_attempts` (execution budget, retries included)
  and `budget.max_total_tokens` (cost budget over observed LLM tokens) stop
  scheduling new work; pending cases are `skipped` and the job fails with
  `budget_exceeded`. Usage is reported in `budget_used`.
- **Recovery** — on backend startup (or via `POST /api/suite-jobs/recover`)
  queued/running jobs whose executor is gone are resumed: orphaned in-flight
  attempts reconcile to their Run's terminal state or to
  `failed`/`executor_interrupted` (retryable), remaining cases continue, and
  resumed jobs are marked `recovered`. Jobs that nobody is executing are
  still flagged `stale` until recovered.

SuiteJob stats count cases by their *final* attempt (`total/completed/failed/
timed_out/cancelled/skipped`) and additionally report `runs` (attempts that
produced a Run) and `retries`.

## Agent Test Platform (Phase 2): Metadata, Linkage, Costs and Run Index

Phase 2 adds data governance and scale-out observability on top of the same
run artifacts, without changing the audit/judge/run lifecycle semantics:

- **Agent metadata** — Agent configs carry `description`, `environment`
  (`local/development/staging/production/unknown` or a preserved custom
  value, `null` when undeclared — never defaulted to production), `tags`,
  `enabled`, `created_at` (written only at creation; legacy configs keep
  `null`) and `updated_at`. `metadata` rejects credentials and private
  prompts, and endpoints with embedded credentials are rejected. New APIs:
  `PUT /api/agents/{agent_id}` and `DELETE /api/agents/{agent_id}`; deleting
  an Agent never deletes its historical runs — they keep their `run.json`
  snapshot and show as `missing_configuration`/derived.
- **Run linkage** — new runs snapshot `agent_ref` (id/name/environment),
  `test_case_ref` (id/case_id/version) and, for batch runs, a `suite_ref`
  with `suite_id/suite_version/job_id/case_execution_id/attempt_id/
  retry_index`. New APIs: `GET /api/runs/{run_id}/context`,
  `GET /api/suite-jobs/{job_id}/runs`,
  `GET /api/suite-jobs/{job_id}/attempts/{attempt_id}`. `run.json` is
  authoritative; disagreements surface as `warnings`, never as run failures.
- **Cost records** — a unified record model with an explicit
  `observed|imported|estimated|missing` source (`mixed` at aggregation).
  Observed costs come from run artifacts (`llm_calls.json` cost fields) and
  are never overwritten; external logs are imported via
  `POST /api/costs/import` (idempotent on `source_system + source_id`,
  `dry_run` supported, per-line errors bounded and redacted, import paths
  confined to the runs/platform directories); estimates exist only when a
  price is configured in `platform/price_table.json` and carry the
  `price_version` they were computed with. Reads: `GET /api/runs/{id}/costs`,
  `GET /api/agents/{id}/costs`, `GET /api/costs/imports/{import_id}`. Missing
  cost is `null`, never `0`; token counts are never treated as dollars.
- **Run index** — `platform/run_index.json` + `run_index_meta.json` cache
  per-run summary fields (no prompts, no trajectory content) for fast
  listing/attribution; the Agent overview and lists read the index first and
  fall back to a safe directory scan (`index.used=false` in the response)
  when it is unavailable or corrupt. Admin endpoints
  `GET /api/admin/run-index/status` and `POST /api/admin/run-index/rebuild`
  are local-only management capabilities (no production auth; gated by
  `AUDITRAIL_LOCAL_ADMIN`, default on, responses state `authority: "local"`).

## Agent Test Platform (Phase 3): External Evidence, Prices and Operations

Phase 3 adapts external evaluation/observability systems and adds platform
operations, under one rule: **external results are additional evidence only** —
they never overwrite `audit.json`, deterministic checks, judge
`overall_status` or score caps, and they never modify formal run artifacts
(all external evidence lives under `platform/external/`).

- **Unified external evidence** — `ExternalEvaluation` (score with explicit
  `score_min`/`score_max`, never reinterpreted as a 0–100 completion score)
  and `ExternalTrace` records carry `source_system`, `source_id`,
  `imported_at`, association method and confidence
  (`exact/strong/weak/unlinked`). Imports are idempotent on
  `source_system + source_id`; conflicting re-imports keep the original record
  and append a warning. Traces that cannot be linked are kept as
  `unlinked_external_trace`, never dropped.
- **Langfuse adapter** (`auditrail/integrations/langfuse.py`, no SDK required)
  — imports traces/scores from safe JSON/JSONL exports, or from an explicitly
  enabled HTTP endpoint guarded by the SSRF policy (`netguard`: scheme/host/
  port checks, DNS re-validation, redirect guards); API tokens come from a
  named environment variable and are never logged or stored. Association
  priority: platform `run_id` metadata > `session_id` > trace-level field >
  operator source-id mapping > agent + time window (always `weak`) >
  `unlinked`. A Langfuse trace id is stored as `source_id`, never presented
  as a platform run id.
- **promptfoo adapter** (`auditrail/integrations/promptfoo.py`) — static
  asset import only: tests/vars/provider declarations/basic assertions are
  mapped to **draft** TestCases (`platform/external/promptfoo_drafts.json`);
  `command`/plugin/shell/Python/JavaScript/template content is rejected or
  preserved as `unmapped` assertions (`safe_to_execute: false`); nothing is
  ever executed and the promptfoo CLI is never called. YAML uses
  `yaml.safe_load` when PyYAML is installed and fails with a clear error
  otherwise (JSON always works). `GET /api/integrations/promptfoo/export/{case_id}`
  renders a safe static promptfoo JSON config: no secrets, tokens,
  Authorization headers or full private prompts.
- **Import APIs** — `POST /api/integrations/{source}/import` (`dry_run`,
  `Idempotency-Key`: same key + same fingerprint replays, different
  fingerprint is 409; import paths confined to the runs/platform roots),
  `POST /api/integrations/{source}/validate`,
  `GET /api/integrations/imports[/{import_id}]`,
  `GET /api/runs/{run_id}/external-evaluations`,
  `GET /api/runs/{run_id}/external-traces`,
  `GET /api/agents/{agent_id}/external-evaluations`.
- **Versioned price tables** (`auditrail/prices.py`) — price versions are
  `draft -> active -> retired` with exactly one active version; activating
  rewrites the derived `platform/price_table.json`, historical `estimated`
  records keep their original `price_version`, referenced/active versions
  cannot be deleted, and only `USD` is accepted (missing prices stay `null`,
  never invented). APIs: `GET /api/prices`,
  `POST /api/prices/versions`, `GET /api/prices/versions/{version}`,
  `POST /api/prices/versions/{version}/activate`,
  `DELETE /api/prices/versions/{version}`, `POST /api/prices/validate`.
  Mutations are local-admin gated (`AUDITRAIL_LOCAL_ADMIN`).
- **Run-index watcher** — an opt-in, debounced **polling** refresher (no OS
  watcher dependency; `mode` is always reported as `polling`). Env:
  `AUDITRAIL_RUN_INDEX_WATCHER` (default `false`),
  `AUDITRAIL_RUN_INDEX_DEBOUNCE_MS` (500), `AUDITRAIL_RUN_INDEX_MAX_BATCH`
  (1000), `AUDITRAIL_RUN_INDEX_POLL_INTERVAL_MS` (2000). It only watches the
  runs root, ignores `.tmp`/hidden/platform directories, collapses bursts
  into one refresh per debounce window, degrades (never crashes the API) on
  errors, and is stopped on shutdown. Manual rebuild and fallback scan remain
  available; watcher state is reported separately in
  `GET /api/costs/admin/run-index/status` under `watcher`, never as a
  data-quality signal.
- **SuiteJob detail page** — the frontend route `/suite-jobs/:jobId`
  (name `suite-job-detail`) shows the job header, summary (unknown/failed/
  cancelled/skipped counted separately), CaseExecutions, the attempt
  timeline (final vs. historical attempts), budget/limits usage and a
  clearly separated external-evidence section, backed by
  `GET /api/suite-jobs/{job_id}/detail`. The legacy `/platform?job=:jobId`
  entry keeps working and links to the new page.

## Agent Test Platform (Phase 4): Production Hardening and Governance

Phase 4 upgrades external imports to resumable asynchronous jobs and adds a
minimal, honest local permission boundary. Still no database, no queue, no
SSO — everything stays file-based under `platform/`.

- **ImportJob** (`auditrail/integrations/import_jobs.py` +
  `backend/app/import_worker.py`) — every external import is a persisted job
  at `platform/external/imports/<import_id>.json` (atomic writes) with the
  state machine
  `queued → validating → running → completed|partial|failed|cancelled`,
  plus `paused` and `stale`. Checkpoints (`cursor`/`page`/`byte_offset`/
  `index`) are written every `AUDITRAIL_IMPORT_CHECKPOINT_RECORDS` (default
  100) records and after every HTTP page, so retry/resume continues instead
  of restarting; dedupe on `source_system + source_id` makes resumed writes
  idempotent. Cancellation is cooperative and persisted; already-imported
  evidence is never rolled back. Workers hold a lease
  (`worker_id`/`heartbeat_at`/`lease_expires_at`): an expired lease marks the
  job `stale` (never `completed`), and an old worker can never overwrite a
  newer worker's state. Errors are classified retryable (HTTP 408/429/5xx,
  timeouts — bounded auto-retry with capped backoff, max
  `AUDITRAIL_IMPORT_MAX_RETRIES`, default 5) vs. non-retryable (schema,
  401/403, SSRF, path errors). `POST /api/integrations/{source}/import`
  keeps the synchronous contract for small/dry-run imports (HTTP 200 +
  legacy report fields); large-file/HTTP imports return HTTP 202 with
  `import_id` and run on the in-process worker. New endpoints:
  `GET /api/integrations/imports/{id}/status`,
  `POST /api/integrations/imports/{id}/cancel|retry|resume`
  (`GET /api/integrations/imports` is now paginated: `limit`/`offset`,
  `has_more`, server cap 200).
- **Langfuse compatibility layer** (`adapter_compat` in reports) — page and
  cursor pagination (`meta.totalPages`/`total_pages`, `meta.cursor`), `data`/
  `traces`/`scores` field variants, partial-error responses keep available
  data with a warning, a broken generation/span never drops its trace, and
  `metadata.session_id` linking variants. Tokens still come only from the
  named env var and never appear in logs/errors/artifacts.
- **promptfoo draft promotion** — drafts carry a lifecycle
  (`draft → approved|rejected|archived`) and are never executed. New
  endpoints: `GET /api/integrations/promptfoo/drafts[/{id}]`,
  `POST .../{id}/approve|reject|archive`. Approve re-validates schema and
  safety, requires derivable http_session input (or an explicit
  `input_override`), creates one stable formal TestCase with a `source_ref`
  (`source_system/source_id/approved_by/approved_at`), is idempotent, and is
  refused for drafts declaring executable content — the platform has no safe
  execution capability for them. Unmapped assertions stay unmapped
  (`required: false`, never converted to executable assertions).
- **Local permission boundary** (`backend/app/auth.py`) — roles
  `viewer`/`operator`/`admin` from server-owned env config:
  `AUDITRAIL_AUTH_MODE=local|disabled` (default `local`),
  `AUDITRAIL_AUTH_ROLE` (default `admin`), `AUDITRAIL_AUTH_ACTOR`.
  `disabled` is a local-development mode only: everything is allowed and
  every response/UI surface says so. `GET /api/auth/context` reports the
  mode, role and effective permissions. This is not production
  authentication; there is no SSO, no tokens, no multi-user identity.
- **Audit log** (`backend/app/audit_service.py`) — append-only JSONL at
  `platform/audit/audit_events.jsonl`; records agent create/update/delete,
  price version lifecycle, import create/cancel/resume, promptfoo draft
  review and index rebuild/pause/resume with actor/role/action/resource/
  result/timestamp. Secrets are redacted; a failed write is an explicit 500
  (`audit_unavailable`), never silent. Read via paginated, filterable
  `GET /api/audit-events`. There is no update/delete path.
- **Run-index operations** — `GET /api/admin/run-index/health` (index +
  watcher + degraded flag), `POST /api/admin/run-index/pause|resume`
  (persisted in the meta file; manual rebuild stays available while paused;
  startup never triggers an implicit full rebuild). Every refresh records
  its reason (`manual|scheduled|file_change|recovery|import_complete`) in
  the meta history; watcher failures surface as `degraded` with
  `failure_count` and a redacted `last_error`. Large runs directories are
  scanned in batches (`scan_batch`, default 500) without holding locks.
- **Pagination governance** — `GET /api/agents/{id}/runs` gains `has_more`
  and a sort whitelist (`effective_ts|started_at|finished_at|status|run_id`,
  unknown sorts are a 400); `GET /api/agents/{id}/external-evaluations` and
  `GET /api/suite-jobs/{id}/runs` are paginated server-side
  (`limit`/`offset`/`total`/`has_more`, capped limits).
- **Frontend** — new pages: `/integrations/imports` (导入任务),
  `/integrations/imports/:importId` (导入详情),
  `/integrations/promptfoo/drafts` (promptfoo 草稿审核),
  `/admin/audit-events` (审计日志). Buttons are permission-gated from
  `GET /api/auth/context`; `disabled` auth mode shows a local-development
  banner.

## Auth

For black-box testing, `X-User-Id` is enough to satisfy the current API login check. Use `--token` only when testing JWT-backed user gateway/model authorization.

## Agent Test Platform (Phase 5): Production Deployment, Auth and Reliable Imports

Phase 5 turns the platform slice into a deployable architecture. The
architecture decision record is `docs/phase5-production-architecture.md`;
operational docs: `docs/deployment.md`, `docs/operations.md`,
`docs/backup-restore.md`, `docs/security.md`.

- **Deployment modes** — `AUDITRAIL_DEPLOYMENT_MODE=local|single_process|production`,
  `AUDITRAIL_STORAGE_MODE=file|database|dual`,
  `AUDITRAIL_QUEUE_MODE=inline|local_worker|redis`,
  `AUDITRAIL_AUTH_MODE=disabled|local|oidc|jwt`. Impossible combinations
  (e.g. `production` + `disabled` auth, `production` + file storage,
  `local_worker`/`redis` queue without database storage) fail startup.
- **Database** — ImportJob state, the user registry, OIDC login state and
  the worker registry can live in SQLite (embedded default, real
  cross-process transactions on one host) or PostgreSQL (optional
  `psycopg` dependency, `AUDITRAIL_DATABASE_URL=postgresql://...`). The
  database holds metadata and indexes only; `runs/<run_id>/` files remain
  the authoritative run-evidence contract, and trajectories/prompts never
  enter the database. Agents/suites/cost-records/external-evidence runtime
  reads stay file-based this phase — they migrate into the database for
  unified query and backup (schema and tooling are in place for a later
  switch).
- **Migrations** — explicit and versioned: `auditrail migrate-db`
  (idempotent, never implicit at API startup), status via
  `--status` or `GET /api/admin/migrations`. File→database data migration:
  `auditrail migrate-platform` (dry-run by default, idempotent, bad records
  never block), `verify-platform-migration`, `rollback-platform-migration`
  (deletes only rows marked with the migration id; files and runs are never
  touched).
- **Cross-process ImportJobs** — in database modes, jobs are claimed with
  transactions and fenced by `lease_version`: one job has exactly one lease
  owner, stale workers cannot overwrite state, heartbeats are fenced, and a
  crashed worker's job becomes `stale` (never `completed`) until explicitly
  resumed. New controls: `pause` (boundary stop) / `resume` / `retry` /
  `cancel` on both `/api/integrations/imports/{id}` and
  `/api/costs/imports/{id}`. Retryable failures requeue with
  `next_retry_at` backoff instead of sleeping while holding the lease.
  Standalone worker: `python -m backend.app.worker_main`; `queue_mode=redis`
  adds a Redis delivery hint (the database stays the source of truth; a
  Redis outage yields `503 queue_unavailable` + `queue_degraded`, never a
  fake enqueue).
- **Cost imports** — `POST /api/costs/import` now runs through the same
  ImportJob state machine (checkpointed, pausable, cancellable, async for
  large files) while preserving observed-priority, `source_id` idempotency
  and price-versioned estimation. Legacy cost reports stay readable.
- **Auth** — `oidc`/`jwt` modes verify Bearer tokens server-side (issuer,
  audience, exp/nbf, signature; JWKS cache + key rotation + bounded stale
  window). Browser SSO: `GET /api/auth/oidc/login` → provider →
  `/api/auth/oidc/callback` → SPA `/login/callback#token=...`. Tokens never
  enter logs/audit/metrics. Roles `viewer < operator < admin < owner` with
  a centralized permission map; user registry (JIT-provisioned, database
  mode) managed via `GET/PUT /api/admin/users` (owner). Single-tenant:
  `tenant_id` is server-owned; client-supplied tenant fields are dropped.
- **Health & metrics** — `GET /api/health` (liveness), `/api/health/live`,
  `/api/health/ready` (200 ready/degraded, 503 not_ready; checks database,
  queue, storage, runs root, audit log, workers), `GET /api/metrics`
  (Prometheus text; no high-cardinality labels, no secrets). Admin pages:
  `/admin/health`, `/admin/workers`, `/admin/migrations`, `/admin/backups`.
- **Backup/restore** — `auditrail backup-platform [--include-database]`,
  `verify-platform-backup`, `restore-platform` (dry-run default, idempotent,
  never overwrites without `--force`). Backups exclude secrets and `runs/`
  evidence (see `docs/backup-restore.md` for RPO/RTO honesty).
- **Docker** — `Dockerfile` + `docker-compose.yml` for local/staging
  (postgres + redis + migrate + api + worker). Compose is not a HA
  production setup.

### Phase 5 environment variables

```bash
# AUDITRAIL_DEPLOYMENT_MODE=local
# AUDITRAIL_STORAGE_MODE=file
# AUDITRAIL_QUEUE_MODE=inline
# AUDITRAIL_DATABASE_URL=sqlite:////abs/path/platform/auditrail.db  # or postgresql://...?sslmode=require
# AUDITRAIL_REDIS_URL=rediss://...
# AUDITRAIL_AUTH_MODE=local          # local|disabled|oidc|jwt
# AUDITRAIL_AUTH_REQUIRED=true
# AUDITRAIL_OIDC_ISSUER_URL=https://idp.example.com
# AUDITRAIL_OIDC_CLIENT_ID=auditrail
# AUDITRAIL_OIDC_CLIENT_SECRET=      # env only, never logged
# AUDITRAIL_OIDC_AUDIENCE=auditrail
# AUDITRAIL_OIDC_SCOPES=openid,profile,email
# AUDITRAIL_OIDC_JWKS_CACHE_SECONDS=3600
# AUDITRAIL_OIDC_JWKS_MAX_STALE_SECONDS=86400
# AUDITRAIL_OIDC_CLOCK_SKEW_SECONDS=60
# AUDITRAIL_OIDC_ROLES_CLAIM=roles
# AUDITRAIL_JWT_SECRET=              # HS256 for auth_mode=jwt
# AUDITRAIL_TENANT_ID=default        # single-tenant; server-owned
# AUDITRAIL_WORKER_POLL_INTERVAL_MS=1000
# AUDITRAIL_WORKER_HEARTBEAT_SECONDS=15
# AUDITRAIL_BACKUP_DIR=              # default <platform>/backups
```
