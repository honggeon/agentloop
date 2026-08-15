# LLM Metrics Proxy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a no-source-change way to capture exact LiteLLM/vLLM chat token usage and latency by running an OpenAI-compatible proxy in front of the current model base URL.

**Architecture:** Add a small FastAPI proxy that forwards OpenAI-compatible requests to an upstream base URL and writes JSONL records for each LLM call. Add a CLI command to import those proxy logs into an existing run as `llm_calls.json`, which the dashboard already reads.

**Tech Stack:** Python 3.11+, FastAPI, Uvicorn, urllib/http.client standard library, pytest.

## Global Constraints

- Do not modify remote `xuanjian-agent` source code.
- Do not modify remote `.env` during this task.
- Proxy must preserve request path, headers required for auth, response body, status code, and content type.
- Proxy logs must not store API keys.
- `agentloop` must accept proxy logs and produce `runs/<run_id>/llm_calls.json`.

---

### Task 1: Proxy Record Utilities

**Files:**
- Create: `agentloop/llm_proxy.py`
- Test: `tests/test_llm_proxy.py`

**Interfaces:**
- Produces: `usage_from_response(payload: dict) -> dict[str, int]`.
- Produces: `build_llm_call_record(...) -> dict`.
- Produces: `append_jsonl(path: Path, item: dict) -> None`.

- [x] **Step 1: Write failing tests for usage normalization and API-key-safe logging.**
- [x] **Step 2: Run the focused tests and confirm they fail because the module does not exist.**
- [x] **Step 3: Implement minimal utility functions.**
- [x] **Step 4: Run focused tests and confirm they pass.**

### Task 2: OpenAI-Compatible Proxy App

**Files:**
- Modify: `agentloop/llm_proxy.py`
- Test: `tests/test_llm_proxy.py`

**Interfaces:**
- Produces: `create_proxy_app(upstream_base_url: str, log_path: Path) -> FastAPI`.
- Handles: `POST /v1/chat/completions` and other POST paths by forwarding to upstream.
- Logs: one JSONL record per request with token usage when response has `usage`.

- [x] **Step 1: Write failing FastAPI test with a monkeypatched upstream request function.**
- [x] **Step 2: Run focused test and confirm failure.**
- [x] **Step 3: Implement proxy app and upstream request function.**
- [x] **Step 4: Run focused tests and confirm pass.**

### Task 3: CLI Import and Run Command

**Files:**
- Modify: `agentloop/cli.py`
- Test: `tests/test_cli_judge_run.py`

**Interfaces:**
- Produces: `agentloop metrics-proxy --upstream-base-url http://127.0.0.1:7500/v1 --host 127.0.0.1 --port 7600 --log runs/proxy-llm-calls.jsonl`.
- Produces: `agentloop import-llm-calls --run runs/<run_id> --log runs/proxy-llm-calls.jsonl`.

- [x] **Step 1: Write failing test for `import-llm-calls` creating `llm_calls.json`.**
- [x] **Step 2: Run focused test and confirm failure.**
- [x] **Step 3: Implement CLI commands.**
- [x] **Step 4: Run focused tests and confirm pass.**

### Task 4: Documentation

**Files:**
- Modify: `docs/litellm-llm-call-instrumentation.md`

**Interfaces:**
- Documents exact proxy startup, curl validation, `.env` change to make later, rollback, and import command.

- [x] **Step 1: Update docs with concrete commands using current remote upstream `http://127.0.0.1:7500/v1`.**
- [x] **Step 2: Run full tests and frontend build.**
