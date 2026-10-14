# DSH Runtime and Judge Integration Implementation Plan

## Purpose

Implement the design in `docs/superpowers/specs/2026-10-13-dsh-runtime-judge-integration-design.md` without changing Agentloop's trust model: durable run artifacts and deterministic checks remain authoritative; DSH execution and semantic judgment are adapters around that contract.

This plan is staged. Each milestone must be independently releasable and tested. Proposed routes and settings must not be added to the root README until they work.

## Delivery Summary

| Milestone | Outcome | Priority |
|---|---|---|
| M0 | Freeze provider, event, and security contracts | P0 |
| M1 | Headless core adapter with deterministic tests | P0 |
| M2 | DSH chat SSE, cancellation, and durable import | P0 |
| M3 | Provider-aware Agent workbench | P0 |
| M4 | DSH judge and normalized report writing | P1 |
| M5 | Multi-judge storage and comparison | P1 |
| M6 | ACP interactive runtime | P2 |
| M7 | Hardening, documentation, and rollout | P0 |

Recommended releases:

1. M0-M3: usable DSH chat-to-audit loop.
2. M4-M5: DSH semantic judge and judge comparison.
3. M6-M7: complete ACP interaction and production hardening.

## M0: Contract and Configuration Foundation

### Tasks

1. Add backend settings for a fixed DSH binary, enabled providers, allowed profiles, allowed workspace roots, timeout, and concurrency.
2. Define core runtime event/config/result types.
3. Define normalized SSE mapping while retaining current remote-provider conventions where practical.
4. Decide provisional versus final imported run id behavior.
5. Define cancellation and one-active-turn-per-session invariants.
6. Finalize additive `source.json` metadata with compatibility tests.

### Likely files

```text
backend/app/config.py
agentloop/dsh_runtime.py
tests/test_backend_config.py
tests/test_dsh_runtime.py
```

### Acceptance

- Defaults do not enable arbitrary local execution.
- Workspace roots and profiles are validated centrally.
- Existing settings and tests continue to work.
- Core runtime types do not import FastAPI.

## M1: Headless Core Adapter

### Tasks

1. Implement safe argv construction for new and resumed sessions.
2. Spawn DSH with an argv array and no shell.
3. Parse stdout as NDJSON and stderr as bounded diagnostics.
4. Normalize `session`, `status`, `thinking`, `text`, `tool_call`, `tool_result`, `final`, and `error`.
5. Track session id, turn-end reason, final text, exit code, timeout, and cancellation.
6. Treat malformed output, missing terminal state, and non-zero exit explicitly.
7. Provide a fake-process boundary so tests never call a model.

### Likely files

```text
agentloop/dsh_runtime.py
agentloop/dsh_headless.py
tests/test_dsh_runtime.py
tests/test_dsh_headless.py
```

### Test matrix

- New and resumed session argv.
- Message beginning with `-` remains task content.
- All documented event variants.
- Tool arguments as objects and raw text.
- Truncation markers.
- Malformed JSON.
- `error` without `final`.
- `final` followed by exit code 1.
- Missing `final` with exit code 0.
- Timeout and cancellation.
- Bounded stderr and no secret echo in public errors.

### Acceptance

- Fixture streams deterministically produce normalized events.
- Tests require no credentials or network.
- Callers cannot inject launcher arguments.

## M2: Backend Chat, Cancellation, and Import

### Tasks

1. Add a DSH chat route using `StreamingResponse`.
2. Add an in-memory active-operation registry and bounded cleanup.
3. Validate profile, workspace, timeout, message, and session ownership.
4. Convert normalized events to existing graph/trace projections.
5. Implement operation cancellation.
6. After settlement, locate and import the durable DSH session through existing `dsh_source` logic.
7. Save acceptance criteria through the existing expectations helper.
8. Emit deterministic audit and final run metadata.
9. Preserve the session id in import-stage errors.

### Likely files

```text
backend/app/routes/dsh_chat.py
backend/app/main.py
backend/app/config.py
agentloop/dsh_source.py              # only if reusable lookup APIs are missing
agentloop/runtime.py                 # reuse/extend projections only
tests/test_backend_dsh_chat.py
tests/test_dsh_source.py
```

The route must reuse `agentloop.cli._write_run_outputs` and current DSH conversion functions. It must not duplicate audit or rendering logic.

### Backend scenarios

- Validation error is an SSE `error` without spawning.
- Disallowed profile/workspace is rejected.
- Session id is captured before subsequent events.
- Graph and trace update from tool events.
- Busy session rejects a second turn.
- Cancellation targets one operation.
- Successful settlement imports a run and emits `run_finished`.
- Import failure emits stage and session id without claiming success.
- Shutdown cleans owned fake processes.

### Acceptance

- A fixture DSH stream plus durable session creates the standard run files.
- The run opens through existing APIs and deterministic audit.
- Existing `/api/live/*` remote flows remain unchanged.

## M3: Agent Workbench UI

### Tasks

1. Add provider types and DSH SSE parsing to `frontend/src/api.ts`.
2. Add a runtime-provider selector to the current live view.
3. Hide remote-only inputs in Local DSH mode.
4. Present only backend-approved workspace/profile choices.
5. Render thinking, assistant text, tool call/result, status, and error events.
6. Add Stop, New Session, and Continue Session actions.
7. Link the imported run to existing audit and judge views.
8. Disable PPT-specific heuristics for generic DSH runs.
9. Collapse thinking by default and distinguish cancelled, failed, imported, and judged states.

### Likely files

```text
frontend/src/api.ts
frontend/src/views/LiveEvalView.vue
frontend/src/styles.css
frontend/src/router.ts               # only if route/name changes
tests/test_frontend_app_state.py
```

Do not build a second chat page unless the current view cannot be kept maintainable. Extract reusable components only when justified by the implementation.

### Acceptance

- User can start, observe, stop, and continue a DSH session.
- Remote base URL/user id are not requested in DSH mode.
- A successfully imported run is reachable from the workbench.
- Failure/cancellation are not displayed as completion.
- Remote live evaluation remains available.
- `npm run build` passes.

## First Release Gate

Release M0-M3 only when:

- unit/backend tests pass without a model;
- frontend type-check/build passes;
- workspace traversal and profile injection tests pass;
- an opt-in smoke test completes a DSH turn and imports its recording;
- cancellation leaves no owned process running;
- the run contains `session.json`, `summary.json`, `trajectory.jsonl`, `audit.json`, `tool_audit.md`, `source.json`, and `expectations.json`; and
- existing remote live tests remain green.

## M4: DSH Judge Engine

### Tasks

1. Introduce a core judge-engine protocol.
2. Wrap the existing DeepAgent invocation without changing behavior.
3. Implement a DSH judge runner using a fixed dedicated profile.
4. Build bounded judge input using existing prompt/evidence helpers.
5. Require a strict JSON proposal and fail explicitly when it cannot be parsed.
6. Route proposals through `resolve_judge_report` and `write_judge_report`.
7. Record judge runtime, profile, version, model, and engine metadata.
8. Mark DSH judge sessions as judge-role and filter them from default evaluand discovery.

### Likely files

```text
agentloop/judges/base.py
agentloop/judges/deepagent_engine.py
agentloop/judges/dsh_engine.py
agentloop/runtime.py
agentloop/agents/tools.py
backend/app/routes/judge.py
tests/test_dsh_judge_engine.py
tests/test_backend_routes.py
tests/test_agent_judge.py
```

The package layout may be simplified if it creates unnecessary indirection, but DSH execution must not be implemented directly in the route.

### Test matrix

- Valid DSH JSON proposal.
- Malformed/fenced output behavior is explicit and tested.
- Missing dimensions become `unknown`, not fabricated failure.
- Invalid evidence references are handled by current evidence rules.
- Deterministic completion and quality caps remain effective.
- Unavailable runtime maps to 503.
- Unexpected failure maps to 500.
- Judge session is hidden from normal evaluand discovery.

### Acceptance

- `dsh` judgment does not require the DeepAgent optional dependency.
- `deepagent` preserves current behavior.
- DSH cannot directly write final reports.
- Deterministic conclusions remain authoritative.

## M5: Multi-Judge Storage and Comparison

### Tasks

1. Extend report writing to support an engine-specific destination without duplicating normalization or rendering.
2. Add `judges/manifest.json` and engine-specific directories.
3. Keep root report files as the active backward-compatible report.
4. Add APIs to list and, if needed, activate reports.
5. Add UI comparison for dimensions, findings, completion, and reliability.
6. Display disagreements without automatically choosing either LLM judge.

### Likely files

```text
agentloop/agents/tools.py
agentloop/agents/schemas.py           # compatible metadata only
backend/app/routes/judge.py
backend/app/routes/runs.py
frontend/src/api.ts
frontend/src/views/RunView.vue
tests/test_agent_judge.py
tests/test_backend_routes.py
tests/test_frontend_app_state.py
```

### Acceptance

- Both reports survive subsequent judge runs.
- Old clients reading root `agent_judge.json` still work.
- Every report passed through `write_judge_report`.
- Comparison distinguishes `unknown` from `fail`.
- Deterministic failures remain visible even when semantic judges pass them.

## M6: ACP Interactive Runtime

### Required spike

1. Verify ACP methods and update shapes against the installed DSH version.
2. Choose a maintained Python client or a small owned bridge process.
3. Prove initialize, new, prompt, update, permission, cancel, close, list, and resume against a controlled profile.
4. Document process ownership and reconnect behavior.

Do not add a dependency before this spike demonstrates lifecycle and packaging fit.

### Implementation tasks

1. Implement JSON-RPC request/notification correlation.
2. Start and monitor the configured ACP process.
3. Maintain per-session prompt exclusivity.
4. Map updates to the normalized events shared with Headless.
5. Surface permission requests and one-shot responses.
6. Support image/resource prompts only when advertised.
7. Implement cancel, close, disconnect, and persisted resume.
8. Keep Headless for one-shot and CI use.

### Likely files

```text
agentloop/dsh_acp.py
backend/app/routes/dsh_chat.py
backend/app/runtime_manager.py
frontend/src/api.ts
frontend/src/views/LiveEvalView.vue
tests/test_dsh_acp.py
tests/test_backend_dsh_chat.py
```

### Acceptance

- Multiple sessions are isolated.
- One prompt is in flight per session.
- Permission is allowed/rejected once.
- Cancellation does not enqueue a late turn.
- Closing one session does not close another.
- Disconnect produces an explicit error, never false success.
- Resume verifies the canonical workspace.

## M7: Hardening and Rollout

### Tasks

1. Add concurrency, time, and output limits.
2. Add structured operation/judge telemetry without sensitive payloads.
3. Implement judge-input redaction and evidence bounds.
4. Add opt-in real DSH integration tests.
5. Update the root README only with implemented routes and settings.
6. Document local-only deployment and single-worker ownership.
7. Run the full relevant regression suite and frontend build.

### Verification commands

Use narrow tests during development, then the full relevant suite:

```bash
uv run pytest -q tests/test_dsh_runtime.py tests/test_backend_dsh_chat.py
uv run pytest -q
cd frontend && npm run build
```

Real DSH/model smoke tests remain opt-in.

## Security Review Checklist

- Binary path is server-owned.
- Profiles/presets are allowlisted.
- HTTP cannot pass `--patch` or raw argv.
- `shell=False` is covered by tests.
- Workspaces are canonicalized beneath allowed roots.
- Session ids never become unchecked paths.
- Judge run ids pass `resolve_run_path`.
- Judge profile has no mutation or side-effect tools.
- Prompts, reasoning, secrets, and raw stderr are not logged by default.
- Timeout/cancellation affects only owned work.
- Browser disconnect behavior is defined and tested.
- Multi-worker deployment is rejected or documented unsupported until ownership is externalized.

## Final Definition of Done

- Agentloop drives DSH from its Web workbench.
- Live events are visible, while final evaluation uses the durable session log.
- New and resumed sessions work only in authorized workspaces.
- Cancellation and failure states are accurate.
- Completed sessions become standard auditable runs.
- DeepAgent and DSH share one normalization/report-writing path.
- Multiple judge reports are preserved without breaking old readers.
- Deterministic checks remain authoritative.
- Judge sessions are isolated from evaluand discovery.
- Relevant pytest suites and frontend build pass.
- Stable configuration and commands are documented only after implementation.
