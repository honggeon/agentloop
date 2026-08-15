# Agentloop MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a small black-box CLI that fetches xuanjian-agent sessions, exports normalized trajectories, and audits tool-call integrity.

**Architecture:** The CLI calls the deployed agent HTTP API with `X-User-Id`, writes run artifacts under `runs/`, and keeps analysis deterministic for the first version. It does not modify or instrument the agent service.

**Tech Stack:** Python 3.9 standard library, pytest for tests, urllib for HTTP/SSE.

## Global Constraints

- Project path: `/Users/chen/agentloop`.
- Project name: `agentloop`.
- No runtime dependency on LoongSuite instrumentation in MVP.
- Auth default: `X-User-Id`; optional JWT bearer token for gateway-backed authorization.
- Outputs: `session.json`, `summary.json`, `trajectory.jsonl`, `audit.json`, `tool_audit.md`.

---

### Task 1: Project Scaffold

**Files:**
- Create: `pyproject.toml`
- Create: `README.md`
- Create: `agentloop/__init__.py`

**Interfaces:**
- Produces package entry point `agentloop = agentloop.cli:main`.

- [x] Create package metadata and README with fetch/smoke usage.
- [x] Keep dependencies empty for runtime portability.

### Task 2: Agent API Client

**Files:**
- Create: `agentloop/client.py`

**Interfaces:**
- Produces `AgentClient.fetch_session(session_id, include_messages=True) -> dict`.
- Produces `AgentClient.create_session(title, model) -> dict`.
- Produces `AgentClient.start_chat(session_id, message, model=None, max_turns=20) -> dict`.
- Produces `AgentClient.stream_events(stream_id) -> Iterator[tuple[str, object]]`.

- [x] Implement urllib JSON requests with `X-User-Id` and optional bearer token.
- [x] Implement basic SSE parsing.

### Task 3: Trajectory Export

**Files:**
- Create: `agentloop/trajectory.py`
- Create: `tests/test_trajectory.py`

**Interfaces:**
- Produces `messages_from_session_response(response) -> list[dict]`.
- Produces `normalize_messages(messages) -> list[dict]`.
- Produces `session_summary(response) -> dict`.

- [x] Normalize role, content, reasoning, tool calls, and tool results into JSONL rows.
- [x] Test role classification and summary counts.

### Task 4: Deterministic Audit

**Files:**
- Create: `agentloop/audit.py`
- Create: `tests/test_audit.py`

**Interfaces:**
- Produces `audit_messages(messages) -> dict`.
- Produces `render_markdown_report(summary, audit) -> str`.

- [x] Detect missing tool results, orphan tool results, tool errors, dangerous tool use, and missing final answer.
- [x] Test missing-result and terminal-tool findings.

### Task 5: CLI Commands

**Files:**
- Create: `agentloop/cli.py`

**Interfaces:**
- Produces `fetch-session` and `smoke-chat` commands.

- [x] Wire client, trajectory export, audit report writing.
- [x] Print the run directory for each command.

### Task 6: Verification

**Files:**
- Test: `tests/test_trajectory.py`
- Test: `tests/test_audit.py`

- [x] Run `python3 -m pytest`.
- [x] Run live read-only fetch against `192.168.1.238:8788` with `X-User-Id`.
