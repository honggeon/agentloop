# Runtime Agent Judge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build runtime evaluation where a prompt and Agent Judge acceptance criteria are streamed through the remote agent and rendered as a live skill/tool workflow graph.

**Architecture:** Add a backend SSE route that starts remote chat, streams compact events, persists the final run, and emits graph projections. Add small deterministic projection and expectation modules so CLI/backend/judge share the same run files. Add a Vue live evaluation page that renders prompt input, graph, event log, final audit, and Agent Judge trigger.

**Tech Stack:** Python 3.11+, FastAPI SSE via `StreamingResponse`, urllib-based existing `AgentClient`, Vue 3, TypeScript, existing CSS/ECharts dependencies only.

## Global Constraints

- Project path: `/Users/chen/agentloop`.
- Runtime agent target is the existing remote HTTP/SSE API, not local agent execution.
- Natural-language acceptance criteria are judged by Agent Judge, not by deterministic audit.
- Deterministic audit remains factual preprocessing.
- No new database.
- No new frontend graph dependency in the first version.
- Existing run artifact names must remain compatible: `session.json`, `summary.json`, `trajectory.jsonl`, `audit.json`, `tool_audit.md`.

---

### Task 1: Runtime Graph Projection

**Files:**
- Create: `agentloop/runtime.py`
- Test: `tests/test_runtime.py`

**Interfaces:**
- Produces `build_runtime_graph(messages: Iterable[dict]) -> dict`.
- Produces `save_expectations(run_dir: Path, acceptance_criteria: str) -> None`.
- Produces `load_expectations(run_path: str | Path) -> dict`.

- [ ] **Step 1: Write failing tests** for graph projection and expectations.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_runtime.py -v` and confirm failure.
- [ ] **Step 3: Implement** `agentloop/runtime.py`.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_runtime.py -v` and confirm pass.

### Task 2: Backend Live Evaluation Route

**Files:**
- Create: `backend/app/routes/live.py`
- Modify: `backend/app/main.py`
- Test: `tests/test_backend_live.py`

**Interfaces:**
- Produces `POST /api/live/evaluate` as `text/event-stream`.
- Consumes request JSON: `base_url`, `user_id`, `message`, `acceptance_criteria`, optional `token`, `model`, `max_turns`, `timeout`.
- Emits SSE events: `run_started`, `agent_event`, `graph_update`, `audit_update`, `run_finished`, `error`.

- [ ] **Step 1: Write failing backend tests** with a fake `AgentClient`.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_backend_live.py -v` and confirm failure.
- [ ] **Step 3: Implement** route and register it in `main.py`.
- [ ] **Step 4: Run** `python3 -m pytest tests/test_backend_live.py -v` and confirm pass.

### Task 3: Agent Judge Criteria Integration

**Files:**
- Modify: `agentloop/cli.py`
- Modify: `backend/app/routes/judge.py`
- Test: `tests/test_cli_judge_run.py`

**Interfaces:**
- `judge-run` reads `expectations.json` when present.
- Backend judge endpoint uses the same criteria.

- [ ] **Step 1: Add tests** proving judge prompts include acceptance criteria.
- [ ] **Step 2: Run focused tests** and confirm failure.
- [ ] **Step 3: Update judge prompt construction** in CLI and backend.
- [ ] **Step 4: Run focused tests** and confirm pass.

### Task 4: Frontend Live Evaluation Page

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/router.ts`
- Modify: `frontend/src/App.vue`
- Create: `frontend/src/views/LiveEvalView.vue`
- Modify: `frontend/src/styles.css`
- Test: `tests/test_frontend_app_state.py`

**Interfaces:**
- Adds route `/live`.
- Form posts to `/api/live/evaluate`.
- Renders workflow graph, event log, deterministic audit summary, and final run link.

- [ ] **Step 1: Add frontend state test** checking source references include the live route/view.
- [ ] **Step 2: Run** `python3 -m pytest tests/test_frontend_app_state.py -v` and confirm failure.
- [ ] **Step 3: Implement** live view and navigation.
- [ ] **Step 4: Run frontend build** with `npm run build`.

### Task 5: Full Verification

**Files:**
- Test: `tests/test_runtime.py`
- Test: `tests/test_backend_live.py`
- Test: `tests/test_backend_routes.py`
- Test: `tests/test_cli_judge_run.py`
- Test: `tests/test_frontend_app_state.py`

- [ ] Run `python3 -m pytest`.
- [ ] Run `cd frontend && npm run build`.
- [ ] Start backend with `uv run uvicorn backend.app.main:app --host 0.0.0.0 --port 18080 --reload`.
- [ ] Start frontend with `cd frontend && npm run dev -- --host 0.0.0.0 --port 5173`.

