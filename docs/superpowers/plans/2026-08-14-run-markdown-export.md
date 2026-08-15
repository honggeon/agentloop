# Run Markdown Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Markdown report export for individual evaluation runs.

**Architecture:** The backend renders a deterministic Markdown report from existing run artifacts and serves it as `text/markdown`. The frontend adds a run-detail download button that fetches the endpoint as a blob.

**Tech Stack:** FastAPI, Python standard library, Vue 3, TypeScript, pytest.

## Global Constraints

- Export format is Markdown.
- Scope is a single run detail page.
- Missing `agent_judge.json` must not block export.
- No new runtime dependencies.

---

### Task 1: Backend Markdown Export Endpoint

**Files:**
- Modify: `backend/app/routes/runs.py`
- Test: `tests/test_backend_routes.py`

**Interfaces:**
- Produces: `GET /api/runs/{run_id}/export.md` returning `text/markdown; charset=utf-8`.
- Produces: `_render_run_markdown_report(run_id: str, path: Path) -> str`.

- [ ] **Step 1: Write the failing test**

Add a test in `tests/test_backend_routes.py`:

```python
def test_export_run_markdown_report_includes_runtime_and_judge(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    (run / "summary.json").write_text(
        json.dumps({"session_id": "s1", "model": "gpt-5", "message_count": 2, "assistant_count": 1, "tool_call_count": 1}),
        encoding="utf-8",
    )
    (run / "audit.json").write_text(
        json.dumps(
            {
                "tool_call_count": 1,
                "tool_calls": [{"tool": "search", "status": "ok"}],
                "findings": [{"severity": "warning", "code": "missing_evidence", "message": "missing proof"}],
                "phases": [],
            }
        ),
        encoding="utf-8",
    )
    (run / "agent_judge.json").write_text(
        json.dumps(
            {
                "overall_status": "success",
                "completion_score": 92,
                "confidence": "high",
                "summary": "任务完成质量较高。",
                "findings": [{"severity": "info", "title": "Evidence", "recommendation": "保留导出记录。"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)

    client = TestClient(app)
    response = client.get("/api/runs/run-1/export.md")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert 'filename="run-1-evaluation.md"' in response.headers["content-disposition"]
    assert "# Evaluation Report: run-1" in response.text
    assert "任务完成质量较高。" in response.text
    assert "| Status | success |" in response.text
    assert "| Tool Calls | 1 |" in response.text
    assert "保留导出记录。" in response.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_backend_routes.py::test_export_run_markdown_report_includes_runtime_and_judge -q`

Expected: FAIL because `/api/runs/run-1/export.md` is not implemented.

- [ ] **Step 3: Implement the endpoint and renderer**

In `backend/app/routes/runs.py`, import `Response`, add the route before `/runs/{run_id}`, and implement helpers that escape table values and render sections from existing artifacts.

- [ ] **Step 4: Run backend test to verify it passes**

Run: `uv run pytest tests/test_backend_routes.py::test_export_run_markdown_report_includes_runtime_and_judge -q`

Expected: PASS.

### Task 2: Frontend Download Action

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/views/RunView.vue`
- Test: `tests/test_frontend_app_state.py`

**Interfaces:**
- Produces: `apiDownload(path: string): Promise<Blob>`.
- Produces: `exportMarkdown(): Promise<void>` in `RunView.vue`.

- [ ] **Step 1: Write the failing source test**

Add a test in `tests/test_frontend_app_state.py`:

```python
def test_run_view_can_export_markdown_report():
    source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "导出 Markdown" in source
    assert "exportMarkdown" in source
    assert "/export.md" in source
    assert "apiDownload" in api_source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_frontend_app_state.py::test_run_view_can_export_markdown_report -q`

Expected: FAIL because the frontend export action does not exist.

- [ ] **Step 3: Implement frontend download**

Add `apiDownload` to `frontend/src/api.ts`. Import it in `RunView.vue`, add an `exporting` state, create/revoke an object URL from the blob, and add a `导出 Markdown` button in `.hero-actions`.

- [ ] **Step 4: Run frontend source test to verify it passes**

Run: `uv run pytest tests/test_frontend_app_state.py::test_run_view_can_export_markdown_report -q`

Expected: PASS.

### Task 3: Verification

**Files:**
- Verify: backend route tests
- Verify: frontend source tests

- [ ] **Step 1: Run focused backend tests**

Run: `uv run pytest tests/test_backend_routes.py -q`

Expected: PASS.

- [ ] **Step 2: Run focused frontend source tests**

Run: `uv run pytest tests/test_frontend_app_state.py -q`

Expected: PASS.

- [ ] **Step 3: Build frontend**

Run: `cd frontend && npm run build`

Expected: PASS.
