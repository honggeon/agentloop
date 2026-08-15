# Agentloop Web Dashboard And Env Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI backend and Vue dashboard that read a shared root `.env`, expose Agentloop run data, and let users view or trigger DeepAgent reports.

**Architecture:** Keep analysis logic in the existing `agentloop` package. Add a backend service that wraps run artifacts and judge execution through HTTP APIs. Add a Vue frontend that consumes those APIs and renders a usable dashboard with charts.

**Tech Stack:** Python `>=3.11,<4.0`, FastAPI, Uvicorn, python-dotenv, pytest, Vue 3, Vite, TypeScript, ECharts.

## Global Constraints

- Use one root `.env.example`.
- Backend may read model keys; frontend may only read `VITE_` public variables.
- Do not expose `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, or any model credential through frontend files or API responses.
- First screen must be the usable dashboard, not a landing page.
- Do not implement authentication, database persistence, production reverse proxy, remote deployment, or live xuanjian-agent instrumentation.
- Run IDs are directory names under `AGENTLOOP_RUNS_DIR`; backend must reject path traversal.

---

## File Structure

- Create `.env.example`: root environment template.
- Modify `pyproject.toml`: add backend dependencies and optional frontend-independent test support.
- Create `backend/app/__init__.py`: backend package marker.
- Create `backend/app/config.py`: `.env` loading and settings object.
- Create `backend/app/routes/__init__.py`: route package marker.
- Create `backend/app/routes/runs.py`: run listing and artifact APIs.
- Create `backend/app/routes/judge.py`: DeepAgent judge APIs.
- Create `backend/app/main.py`: FastAPI app, CORS, routes, health.
- Create `tests/test_backend_config.py`: config and safe path tests.
- Create `tests/test_backend_routes.py`: FastAPI route tests.
- Create `frontend/package.json`: Vue/Vite dependencies and scripts.
- Create `frontend/index.html`: app shell.
- Create `frontend/vite.config.ts`: Vite config.
- Create `frontend/src/main.ts`: Vue bootstrap.
- Create `frontend/src/api.ts`: typed API client.
- Create `frontend/src/App.vue`: dashboard UI.
- Create `frontend/src/styles.css`: restrained dashboard styling.
- Modify `README.md`: add backend/frontend startup instructions.

---

### Task 1: Env Template And Backend Config

**Files:**
- Create: `.env.example`
- Create: `backend/app/__init__.py`
- Create: `backend/app/config.py`
- Test: `tests/test_backend_config.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Produces: `Settings` dataclass with `runs_dir: Path`, `backend_host: str`, `backend_port: int`, `cors_origins: list[str]`.
- Produces: `get_settings() -> Settings`.
- Produces: `resolve_run_path(run_id: str, settings: Settings | None = None) -> Path`.

- [ ] **Step 1: Write failing config tests**

Create `tests/test_backend_config.py`:

```python
from pathlib import Path

import pytest

from backend.app.config import Settings, resolve_run_path


def test_resolve_run_path_accepts_child_directory(tmp_path):
    run = tmp_path / "runs" / "20260807-safe"
    run.mkdir(parents=True)
    path = resolve_run_path("20260807-safe", Settings(runs_dir=tmp_path / "runs"))
    assert path == run


def test_resolve_run_path_rejects_path_traversal(tmp_path):
    (tmp_path / "runs").mkdir()
    with pytest.raises(ValueError):
        resolve_run_path("../secret", Settings(runs_dir=tmp_path / "runs"))
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_backend_config.py -q`

Expected: import failure because `backend.app.config` does not exist.

- [ ] **Step 3: Add backend dependencies**

Modify `pyproject.toml`:

```toml
dependencies = [
  "fastapi>=0.115",
  "python-dotenv>=1.0",
  "uvicorn>=0.30",
]

[project.optional-dependencies]
dev = ["pytest>=8", "httpx>=0.27"]
```

Keep the existing `agent` extra.

- [ ] **Step 4: Add env template**

Create `.env.example`:

```bash
AGENTLOOP_RUNS_DIR=/Users/chen/agentloop/runs
BACKEND_HOST=0.0.0.0
BACKEND_PORT=18080
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

MODEL_PROVIDER=openai
MODEL_NAME=gpt-4.1
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
CHECKPOINTER_TYPE=memory

VITE_API_BASE_URL=http://localhost:18080
```

- [ ] **Step 5: Implement config**

Create `backend/app/config.py` with:

```python
from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    runs_dir: Path = PROJECT_ROOT / "runs"
    backend_host: str = "0.0.0.0"
    backend_port: int = 18080
    cors_origins: list[str] | None = None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    origins = os.getenv("CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173")
    return Settings(
        runs_dir=Path(os.getenv("AGENTLOOP_RUNS_DIR", str(PROJECT_ROOT / "runs"))).expanduser(),
        backend_host=os.getenv("BACKEND_HOST", "0.0.0.0"),
        backend_port=int(os.getenv("BACKEND_PORT", "18080")),
        cors_origins=[item.strip() for item in origins.split(",") if item.strip()],
    )


def resolve_run_path(run_id: str, settings: Settings | None = None) -> Path:
    if "/" in run_id or "\\" in run_id or run_id in {"", ".", ".."}:
        raise ValueError("invalid run_id")
    current = settings or get_settings()
    root = current.runs_dir.resolve()
    path = (root / run_id).resolve()
    if root != path and root not in path.parents:
        raise ValueError("invalid run_id")
    if not path.exists() or not path.is_dir():
        raise FileNotFoundError("missing run %s" % run_id)
    return path
```

- [ ] **Step 6: Run config tests**

Run: `uv run pytest tests/test_backend_config.py -q`

Expected: pass.

---

### Task 2: Backend Run And Judge APIs

**Files:**
- Create: `backend/app/routes/__init__.py`
- Create: `backend/app/routes/runs.py`
- Create: `backend/app/routes/judge.py`
- Create: `backend/app/main.py`
- Test: `tests/test_backend_routes.py`

**Interfaces:**
- Consumes: `get_settings() -> Settings`.
- Consumes: `resolve_run_path(run_id: str, settings: Settings | None = None) -> Path`.
- Consumes: `agentloop.agents.tools` artifact helpers.
- Produces: FastAPI app `backend.app.main:app`.

- [ ] **Step 1: Write failing route tests**

Create `tests/test_backend_routes.py`:

```python
import json

from fastapi.testclient import TestClient

from backend.app.config import Settings
from backend.app.main import app


def _write_run(tmp_path):
    run = tmp_path / "runs" / "run-1"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
    (run / "audit.json").write_text(json.dumps({"tool_calls": [], "findings": [], "phases": []}), encoding="utf-8")
    (run / "trajectory.jsonl").write_text("", encoding="utf-8")
    return run


def test_health():
    client = TestClient(app)
    assert client.get("/api/health").json()["status"] == "ok"


def test_list_runs(tmp_path, monkeypatch):
    _write_run(tmp_path)
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: Settings(runs_dir=tmp_path / "runs"))
    client = TestClient(app)
    data = client.get("/api/runs").json()
    assert data["runs"][0]["name"] == "run-1"


def test_get_run_summary(tmp_path, monkeypatch):
    _write_run(tmp_path)
    settings = Settings(runs_dir=tmp_path / "runs")
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)
    client = TestClient(app)
    assert client.get("/api/runs/run-1").json()["summary"]["session_id"] == "s1"
```

- [ ] **Step 2: Run route test to verify failure**

Run: `uv run pytest tests/test_backend_routes.py -q`

Expected: import failure because `backend.app.main` does not exist.

- [ ] **Step 3: Implement routes**

`backend/app/routes/runs.py` must provide:

- `router = APIRouter(prefix="/api")`
- `GET /runs`: returns `{"runs": list_runs(str(settings.runs_dir))}`
- `GET /runs/{run_id}`: returns `{"run_id": run_id, "summary": load_run_summary(path)}`
- `GET /runs/{run_id}/audit`: returns `load_audit(path)`
- `GET /runs/{run_id}/judge`: returns existing `agent_judge.json` or `{"status": "missing"}`
- `GET /runs/{run_id}/charts`: returns existing `chart_data.json` or `build_chart_data(path)`

`backend/app/routes/judge.py` must provide:

- `POST /api/runs/{run_id}/judge`
- It calls `agentloop.agents.agent.invoke_agent`, then `write_judge_report`.
- It returns `{"status": "ok", "paths": paths, "report": report}`.

`backend/app/main.py` must:

- Create `app = FastAPI(title="Agentloop")`.
- Add CORS middleware using `settings.cors_origins`.
- Register both routers.
- Add `GET /api/health`.

- [ ] **Step 4: Run route tests**

Run: `uv run pytest tests/test_backend_routes.py -q`

Expected: pass.

---

### Task 3: Frontend Dashboard

**Files:**
- Create: `frontend/package.json`
- Create: `frontend/index.html`
- Create: `frontend/vite.config.ts`
- Create: `frontend/src/main.ts`
- Create: `frontend/src/api.ts`
- Create: `frontend/src/App.vue`
- Create: `frontend/src/styles.css`

**Interfaces:**
- Consumes backend APIs from Task 2.
- Produces Vite app with scripts `dev` and `build`.

- [ ] **Step 1: Create frontend package**

Create `frontend/package.json`:

```json
{
  "name": "agentloop-frontend",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc --noEmit && vite build",
    "preview": "vite preview"
  },
  "dependencies": {
    "@vitejs/plugin-vue": "^5.2.0",
    "echarts": "^5.5.1",
    "vue": "^3.5.0",
    "vue-echarts": "^7.0.3"
  },
  "devDependencies": {
    "typescript": "^5.6.0",
    "vite": "^6.0.0",
    "vue-tsc": "^2.1.10"
  }
}
```

- [ ] **Step 2: Add frontend source files**

Implement:

- `api.ts`: `apiGet(path)` and `apiPost(path)` using `import.meta.env.VITE_API_BASE_URL`.
- `App.vue`: dashboard with run list, summary panel, charts, findings, and judge button.
- `styles.css`: responsive dashboard layout with neutral work-focused styling.

The judge button text must make model analysis explicit: `运行 DeepAgent 分析`.

- [ ] **Step 3: Install frontend dependencies**

Run:

```bash
cd frontend
npm install
```

Expected: install succeeds and creates `package-lock.json`.

- [ ] **Step 4: Build frontend**

Run:

```bash
cd frontend
npm run build
```

Expected: build succeeds.

---

### Task 4: Startup Docs And Full Verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes all prior tasks.
- Produces documented startup flow.

- [ ] **Step 1: Update README**

Add:

```markdown
## Web dashboard

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
```

- [ ] **Step 2: Run backend tests**

Run:

```bash
uv run pytest tests/test_backend_config.py tests/test_backend_routes.py -q
```

Expected: pass.

- [ ] **Step 3: Run full Python tests**

Run:

```bash
uv run pytest -q
```

Expected: pass.

- [ ] **Step 4: Run frontend build**

Run:

```bash
cd frontend
npm run build
```

Expected: pass.

- [ ] **Step 5: Manual service smoke check**

Run backend:

```bash
uv run uvicorn backend.app.main:app --host 0.0.0.0 --port 18080
```

In another terminal:

```bash
curl http://localhost:18080/api/health
curl http://localhost:18080/api/runs
```

Expected health response contains `{"status":"ok"}` and runs response contains a `runs` array.

---

## Self-Review

Spec coverage:

- `.env.example`: Task 1.
- Backend config and safe path resolution: Task 1.
- Backend APIs: Task 2.
- Vue dashboard: Task 3.
- Startup docs and verification: Task 4.

Placeholder scan:

- No placeholder markers are intentionally left in this plan.
- Each task includes concrete files, interfaces, tests, and commands.

Type consistency:

- `Settings`, `get_settings`, and `resolve_run_path` signatures are consistent across backend tasks.
- Backend API paths match the design spec.
