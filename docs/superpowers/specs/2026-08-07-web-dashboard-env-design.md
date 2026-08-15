# Agentloop Web Dashboard And Env Design

## Goal

Add a front/back separated web service for Agentloop so other users can open a browser, inspect exported runs, view deterministic audit data, trigger DeepAgent analysis, and review chart-based reports.

## Scope

In scope:

- Add a root `.env.example`.
- Add a Python FastAPI backend.
- Add a Vue frontend with chart-oriented dashboard views.
- Reuse existing `agentloop` run files and DeepAgent analysis module.
- Provide local network startup commands.

Out of scope:

- Authentication and user management.
- Production reverse proxy.
- Database persistence.
- Live instrumentation inside xuanjian-agent.
- Automatic remote deployment to `192.168.1.238`.

## Environment Model

The project root owns `.env`.

Backend reads all variables:

- `AGENTLOOP_RUNS_DIR=/Users/chen/agentloop/runs`
- `BACKEND_HOST=0.0.0.0`
- `BACKEND_PORT=18080`
- `CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173`
- `MODEL_PROVIDER=openai`
- `MODEL_NAME=gpt-4.1`
- `OPENAI_API_KEY=`
- `DEEPSEEK_API_KEY=`
- `CHECKPOINTER_TYPE=memory`

Frontend only reads public `VITE_` variables:

- `VITE_API_BASE_URL=http://localhost:18080`

Model provider keys must never be copied into frontend files or browser-visible responses.

## Backend

Backend directory:

```text
backend/
  app/
    __init__.py
    config.py
    main.py
    routes/
      __init__.py
      runs.py
      judge.py
```

Backend uses FastAPI and directly imports the existing `agentloop.agents.tools` helpers for file-based run inspection. DeepAgent execution is exposed only through the judge route.

API:

- `GET /api/health`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/audit`
- `GET /api/runs/{run_id}/judge`
- `POST /api/runs/{run_id}/judge`
- `GET /api/runs/{run_id}/charts`

Run IDs are directory names under `AGENTLOOP_RUNS_DIR`. The backend must resolve run paths safely and reject path traversal.

Startup:

```bash
uv run uvicorn backend.app.main:app --host 0.0.0.0 --port 18080 --reload
```

## Frontend

Frontend directory:

```text
frontend/
  package.json
  index.html
  vite.config.ts
  src/
    main.ts
    App.vue
    api.ts
    styles.css
```

Frontend uses Vue 3, Vite, and ECharts. It reads `VITE_API_BASE_URL`.

First screen is the usable dashboard, not a landing page. It should show:

- Run list.
- Selected run summary.
- Completion score.
- Tool distribution chart.
- Phase timeline chart.
- Findings by severity.
- Deterministic audit findings.
- Agent judge report when available.
- Button to trigger DeepAgent analysis.

Startup:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5173
```

## Security

`POST /api/runs/{run_id}/judge` may send private trajectory data to the configured model provider. The UI must label this action as model analysis and the backend must rely on `.env` model credentials. No model credentials are returned to the frontend.

## Testing

Backend tests:

- Config defaults.
- Safe run path resolution.
- Health route.
- Run listing and artifact loading.

Frontend verification:

- `npm run build`.
- Manual local browser check after backend starts.

Full local verification:

```bash
uv run pytest -q
uv run uvicorn backend.app.main:app --host 0.0.0.0 --port 18080 --reload
cd frontend && npm run dev -- --host 0.0.0.0 --port 5173
```
