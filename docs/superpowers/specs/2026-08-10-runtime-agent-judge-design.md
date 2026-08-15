# Runtime Agent Judge Design

## Goal

Agentloop will support runtime evaluation: a user enters a prompt and natural-language acceptance criteria, sends it to the remote agent service, watches skill/tool activity render as a live workflow graph, and receives deterministic plus Agent Judge results at the end.

## Scope

In scope:

- Start a new remote agent session from the dashboard.
- Stream the remote `/api/chat/stream` events through the Agentloop backend.
- Persist the completed run using the existing `session.json`, `summary.json`, `trajectory.jsonl`, `audit.json`, and `tool_audit.md` files.
- Project observed assistant/tool messages into graph nodes and edges.
- Accept natural-language criteria as Agent Judge input.
- Save those criteria in the run directory so `judge-run` and the backend judge endpoint can include them.

Out of scope:

- Instrumenting the remote agent internals.
- Replacing existing deterministic audit files.
- Batch live execution.
- A new database.
- Installing a graph library for the first version.

## Architecture

The backend remains the source of truth. It owns remote session creation, chat start, SSE consumption, final session fetch, run persistence, and event projection. The frontend consumes a single Agentloop SSE endpoint and renders graph state from normalized events.

The first graph renderer will use simple SVG/CSS nodes in Vue, not a new dependency. This keeps the feature install-free and easy to verify. The graph data contract stays library-neutral so Vue Flow or another graph component can replace it later.

## Runtime Events

Backend events use this shape:

```json
{
  "event": "graph_update",
  "payload": {
    "run_id": "20260810-120000-session-id",
    "graph": {
      "nodes": [
        {"id": "m0", "label": "User Prompt", "type": "user", "status": "ok", "detail": "..."}
      ],
      "edges": [
        {"from": "m0", "to": "m1"}
      ],
      "current_node": "m1"
    }
  }
}
```

Event names:

- `run_started`: contains `session_id`, `stream_id`, and temporary `run_id`.
- `agent_event`: forwards compact remote SSE event metadata for the log.
- `graph_update`: contains graph nodes and edges derived from the latest available messages/events.
- `audit_update`: contains deterministic audit output when final session data is available.
- `run_finished`: contains final `run_id` and persisted run metadata.
- `error`: contains a user-safe error string.

## Flow Projection

Flow projection is deterministic and based on normalized messages:

- User messages become `user` nodes.
- Assistant messages with `tool_calls` become one `assistant` node followed by one node per tool call.
- Tool messages become `tool_result` nodes linked from the matching tool call when possible.
- Assistant messages with final content and no tool calls become `final` nodes.
- Tool result status uses the existing audit error detector behavior.

Skill calls are represented as tool nodes where the tool name is `skill_view`. The label should prefer a parsed skill name from arguments when available.

## Expectations And Agent Judge

The runtime form has two expectation fields:

- `acceptance_criteria`: natural-language criteria for Agent Judge.
- `deterministic_checks`: optional structured checks for later expansion.

For this milestone, only `acceptance_criteria` is implemented. It is saved to `expectations.json`:

```json
{
  "acceptance_criteria": "- Must generate the requested file\n- Must not fabricate user confirmation"
}
```

Agent Judge prompts must explicitly load these criteria when present and judge against them. Deterministic audit remains factual preprocessing and does not interpret the natural-language criteria.

## Error Handling

Remote API failures are streamed as `error` and do not write a successful run. If the session was created but chat fails, the error payload includes the session id. If final session fetch fails after stream completion, the stream reports the failure and no run is added to the dashboard.

## Testing

Unit tests cover:

- Graph projection from messages with skill/tool calls and tool results.
- Saving and loading expectations.
- Backend live endpoint behavior using a fake `AgentClient`.
- Agent Judge prompt content includes acceptance criteria.

Manual verification covers:

- Start backend and frontend.
- Submit a prompt and criteria.
- Confirm graph nodes update while remote events arrive.
- Confirm completed run appears in the dashboard.
- Run Agent Judge and confirm the report references the criteria.

