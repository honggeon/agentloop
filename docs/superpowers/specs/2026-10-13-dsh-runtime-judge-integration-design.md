# DSH Runtime and Judge Integration Design

## Status

Proposed. This document describes the target design. Routes, settings, files, and classes marked as proposed are not available until implemented. Runtime code and tests remain the source of truth.

## Goal

Embed DeepSeek Harness (DSH) into Agentloop as an execution runtime and an optional semantic judge so a user can:

- chat with a local DSH agent from Agentloop Web;
- observe assistant messages, thinking, tool calls, tool results, and terminal state;
- continue, cancel, and later resume a DSH session;
- import the durable DSH recording into the standard `runs/<run_id>/` contract;
- run deterministic audit over the imported run;
- choose the existing DeepAgent judge or a dedicated DSH judge; and
- compare semantic reports without allowing either judge to override deterministic failures.

Agentloop remains the control plane, artifact owner, and deterministic authority. DSH supplies agent execution and, optionally, semantic analysis.

## Current State

Agentloop already provides:

- a live evaluation UI with prompt, acceptance criteria, uploads, SSE progress, graph, trace, clarification, approval, continuation, audit, and judge actions;
- `AgentClient`, which drives a `xuanjian-agent`-compatible HTTP service;
- DSH session discovery and import through `agentloop/dsh_source.py`;
- the standard run contract and deterministic tool audit;
- a DeepAgent judge endpoint that normalizes output with `resolve_judge_report` and persists it with `write_judge_report`; and
- a DSH-side plugin that embeds Agentloop evaluation controls into DSH Web.

The missing direction is Agentloop driving DSH as its local runtime. The existing DSH-side plugin is complementary, not a substitute for this integration.

DSH 0.2.0-rc.2 exposes two useful automation surfaces:

- Headless: one task per process, NDJSON events with `--json`, and persisted-session continuation with `--session-id`.
- ACP: a persistent stdio JSON-RPC process supporting session creation/resume, prompts, semantic updates, cancellation, permission requests, configuration, and close.

## Scope

### In scope

- A local DSH runtime provider in the Agentloop workbench.
- Headless execution as the minimum viable integration.
- Durable DSH session import after a turn settles.
- Existing deterministic audit and run rendering.
- A selectable DSH semantic judge.
- Backward-compatible storage for more than one judge report.
- A later ACP transport for full interactive behavior.
- Explicit process, workspace, permission, and evidence boundaries.

### Out of scope

- Embedding the complete DSH Web application in an iframe.
- Replacing deterministic audit with model judgment.
- Treating one DSH judge result as a product-level DSH release oracle.
- Allowing browser clients to provide arbitrary executables, shell commands, profile patches, or unrestricted workspaces.
- Reimplementing the judge schema, dimensions, evidence validator, quality assessment, or Markdown renderer.
- A database or distributed scheduler in the first release.
- Multi-tenant isolation in the first local-only release.

## Architecture Decision

Use a provider boundary behind Agentloop Web:

```text
Agentloop Web Workbench
        |
        +-- remote provider --> AgentClient --> xuanjian-agent HTTP
        |
        +-- dsh provider -----> DshRuntimeClient
                                      |
                         +------------+------------+
                         |                         |
                    Headless MVP               ACP target
                         |                         |
                         +----- DSH session -------+
                                      |
                                  dsh_source
                                      |
                              standard run contract
                                      |
                  deterministic audit + selected judge
                              |                 |
                         DeepAgent          DSH Judge
                              +--------+--------+
                                       |
                    resolve_judge_report / deterministic caps
                                       |
                              write_judge_report
```

The DSH Web shell is not embedded. It owns separate boot, routing, session, and presentation state. Agentloop renders its existing UI against normalized runtime events.

## Runtime Provider Design

### Core/backend boundary

Proposed core modules own DSH protocol behavior:

```text
agentloop/dsh_runtime.py       # shared config/event/result types
agentloop/dsh_headless.py      # argv construction and NDJSON parsing
agentloop/dsh_acp.py           # later ACP JSON-RPC client
```

The core package must not import `backend`. It normalizes DSH events but does not know about FastAPI or SSE.

The backend owns request validation, executable/profile allowlists, workspace authorization, process lifecycle, concurrency, SSE conversion, safe error mapping, and orchestration of durable import.

`AgentClient` remains the black-box HTTP client. DSH-specific behavior does not belong in it.

### Headless MVP

Invoke DSH without a shell:

```text
[dsh_binary, "--profile", "headless", "--json", message]
```

Continuation adds `--session-id <id>`. The browser never supplies the executable path or raw launcher arguments.

Headless projects these committed events:

| DSH event | Meaning |
|---|---|
| `session` | Session identity and effective working directory |
| `status/turn_start` | Turn admitted |
| `status/step_start` | Model/tool step started |
| `thinking` | Committed reasoning block |
| `text` | Committed assistant text block |
| `tool_call` | Tool name, call id, and bounded input |
| `tool_result` | Call id, completion state, and bounded result |
| `status/step_end` | Step settled, optionally with usage |
| `status/turn_end` | Turn settled with a reason |
| `final` | Lossless final answer for this invocation |
| `error` | Runner-level failure before a final event |

The exit code and `turn_end.reason` are authoritative signals. The presence of `final` alone does not prove success.

### Real-time projection versus durable evidence

The Headless stream is bounded and omits unmodeled session events. It is suitable for progress, not final evidence:

```text
NDJSON projection --> live SSE and provisional graph
Durable DSH log  --> dsh_source --> normalized run --> audit/judge
```

Agentloop must never construct the final run solely from projected NDJSON.

### ACP target

ACP replaces per-message processes when complete interaction is required. The target adapter covers:

- `initialize`;
- `session/new` and `session/resume`;
- `session/prompt` and ordered `session/update`;
- `session/request_permission` and one-shot responses;
- `session/cancel`;
- model/reasoning configuration;
- `session/close`; and
- disconnect and restart handling.

Headless remains useful for CI and one-shot tasks.

## Proposed HTTP and SSE Contract

These routes are proposals and must not be documented as stable user commands until implemented.

### Start or continue a turn

```http
POST /api/dsh/chat
Content-Type: application/json
```

```json
{
  "message": "Run the relevant tests and explain failures",
  "workspace": "/allowed/project",
  "profile": "headless",
  "session_id": null,
  "acceptance_criteria": "Relevant tests pass and evidence is cited",
  "timeout": 600
}
```

The response is `text/event-stream`.

### Cancel an operation

```http
POST /api/dsh/chat/{operation_id}/cancel
```

`operation_id` identifies one active process or prompt. It is not a DSH session id.

### SSE events

| Event | Required data |
|---|---|
| `run_started` | `operation_id`, `session_id`, provisional `run_id`, provider |
| `agent_event` | Compact normalized runtime event |
| `graph_update` | Current projected graph |
| `trace_update` | Current projected trace |
| `permission_requested` | ACP phase: request id and choices |
| `audit_update` | Deterministic audit after durable import |
| `run_finished` | Final Agentloop `run_id` and summary |
| `error` | Safe detail, stage, and known identifiers |

New fields are additive. Existing remote-provider consumers remain valid.

## Session and Process Lifecycle

For the local single-instance release, the backend keeps an in-memory registry:

```text
operation_id -> process/prompt handle, session_id, workspace, start time, state
```

Required invariants:

- One active prompt owns a DSH session at a time.
- A busy session rejects another turn rather than silently queuing it.
- Cancellation targets an operation, not all profile processes.
- Timeout performs bounded cancellation/termination and reports final state.
- Backend shutdown drains or terminates owned children.
- Completed operations leave the registry.
- A crashed process cannot produce a successful UI state.

This is not a distributed scheduler. Local DSH execution must remain single-worker until ownership is externalized.

## Durable Run Import

After settlement, use the reported session id with existing DSH discovery/import code. The run continues to contain:

```text
session.json
summary.json
trajectory.jsonl
audit.json
tool_audit.md
source.json
expectations.json
```

Proposed additive source metadata:

```json
{
  "source_type": "dsh",
  "interaction_mode": "agentloop-chat",
  "dsh_session_id": "session-...",
  "dsh_profile": "headless",
  "dsh_version": "0.2.0-rc.2",
  "session_role": "evaluand"
}
```

Exact fields must be finalized in code and compatibility tests. Existing runs remain readable.

If execution completes but import fails, report an import-stage error and preserve the session id for later recovery. Do not claim a successful Agentloop run.

## Workbench UX

The current live evaluation page becomes provider-aware rather than duplicating DSH Web.

```text
Runtime provider: Remote Agent | Local DSH
```

Remote mode retains its current HTTP fields and interactions. Local DSH mode displays only supported inputs: allowed workspace/profile, model when supported, acceptance criteria, message, new/continue session, stop, and imported-run actions.

Remote-agent-specific PPT continuation heuristics must not run for generic DSH chat.

Suggested layout:

- session/history column;
- main conversation stream;
- trace/tool panel; and
- audit/judge drawer.

Thinking content is collapsed by default because it can be large and sensitive.

## Judge Architecture

### Engine boundary

The judge endpoint selects `deepagent` or `dsh`. Both return an untrusted proposal. The shared path remains:

```text
raw proposal
  -> resolve_judge_report
  -> evidence validation
  -> deterministic dimension caps
  -> assess_run_quality
  -> write_judge_report
```

No engine may write or certify final report files directly.

### DSH judge profile

Use a fixed, dedicated `agentloop-judge` profile distinct from the evaluated profile. Preferred capabilities:

- read-only evidence access;
- bounded trajectory search/pagination;
- optional analysis subagents;
- no shell or file mutation;
- no external side-effect tools; and
- no report-writing tool.

The preferred formal implementation exposes a read-only Agentloop MCP service with tools such as:

- `get_run_summary`;
- `get_run_expectations`;
- `get_run_audit`;
- `get_trajectory_page`;
- `search_trajectory`; and
- `assess_run_quality`.

HTTP run ids still pass `resolve_run_path` before evidence access.

### Trust boundary

A DSH judge may share defects, tools, prompts, or models with the evaluated runtime. Its output is supplementary semantic evidence, not a release oracle.

Mitigations:

- distinct judge profile and workspace;
- preferably a different model/provider;
- record runtime version, model, and profile;
- retain DeepAgent as an independent comparison path;
- keep deterministic failures authoritative; and
- use controlled scenario suites for product correctness.

### Prevent recursive evaluation

Judge sessions use `session_role: judge`, a dedicated profile, and are hidden from default evaluand discovery. A judge session cannot target itself.

## Multiple Judge Reports

Root files remain the backward-compatible active report:

```text
agent_judge.json
agent_judge.md
chart_data.json
```

Proposed archive:

```text
judges/
  manifest.json
  deepagent/
    agent_judge.json
    agent_judge.md
    chart_data.json
  dsh/
    agent_judge.json
    agent_judge.md
    chart_data.json
```

`write_judge_report` remains the only normalization/rendering path. Implementation may extend its output target or archive normalized outputs, but must not create another schema or renderer.

## Security

### Executable and arguments

- Binary path comes from backend configuration.
- Profiles/presets use an allowlist.
- HTTP cannot pass `--patch`, arbitrary environment, or raw argv.
- Processes run without a shell.
- Session ids are opaque and never become unchecked paths.

### Workspace

Resolve configured workspace roots and requested workspace paths on the backend. The request must resolve beneath an allowed root. Reject root paths, traversal, and symlink escapes. Resume must use the persisted session's canonical workspace.

### Resource limits

- Maximum concurrent operations.
- Per-turn timeout.
- Maximum event and diagnostic sizes.
- Bounded cancellation/termination.
- Bounded judge evidence.

### Privacy

Runs can contain source, personal data, secrets, arguments, and reasoning. Bound and redact model-bound evidence. The UI distinguishes transcript display from consent to include optional thinking or attachment content in judgment.

## Error Handling

| Stage | Behavior |
|---|---|
| validation | SSE `error` before process start |
| spawn | report configured binary/profile failure |
| stream parsing | report malformed frame and stop owned process |
| execution | report exit code and turn-end reason |
| cancellation | report cancelled state, not success |
| import | preserve session id and report recoverable error |
| judge unavailable | HTTP 503 |
| unexpected judge failure | HTTP 500 |
| invalid judge proposal | reject without weakening deterministic data |

Do not return raw secrets or uncontrolled stderr to the browser.

## Compatibility

- Remote live evaluation stays available.
- Existing runs remain readable.
- Source/judge metadata additions are backward-compatible.
- Root judge files retain their meaning.
- Deterministic logic has no model dependency.
- Headless remains after ACP is added.
- Stable commands/settings enter the root README only after implementation.

## Verification Strategy

Default tests use fake process streams and temporary runs, never real model calls. Required coverage includes:

- safe argv construction;
- all NDJSON event types;
- malformed output, missing final, non-zero exit, timeout, and cancellation;
- one operation per session;
- workspace/profile rejection;
- SSE ordering and stage errors;
- durable import;
- metadata compatibility;
- malformed DSH judge output and invalid evidence;
- deterministic caps after judgment;
- multi-judge compatibility; and
- frontend provider/state behavior.

Real DSH/model tests are opt-in integration tests, not the CI oracle.

## Rollout

1. Ship Headless chat behind explicit Local DSH selection.
2. Validate continuation, cancellation, import, and audit.
3. Add DSH Judge behind separate configuration.
4. Add judge comparison and archive.
5. Complete an ACP client/lifecycle spike.
6. Promote ACP to the interactive default while retaining Headless.

## Open Decisions

- Python ACP dependency versus a small owned bridge process.
- FastAPI process ownership versus a dedicated local runtime service before multi-worker deployment.
- Exact additive source metadata names.
- How an archived judge report becomes the active root report.
- Initial redaction policy and consent-required evidence classes.
