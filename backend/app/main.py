from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.app.config import get_settings
from backend.app.routes import costs, dashboard, dsh, dsh_acp, dsh_chat, integrations, judge, live, platform, prices, runs
from backend.app import run_index_watch


settings = get_settings()
app = FastAPI(title="Auditrail")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(runs.router)
app.include_router(platform.router)
app.include_router(costs.router)
app.include_router(integrations.router)
app.include_router(prices.router)
app.include_router(judge.router)
app.include_router(live.router)
app.include_router(dsh.router)
app.include_router(dsh_chat.router)
app.include_router(dsh_acp.router)
app.include_router(dashboard.router)
app.router.add_event_handler("shutdown", dsh_chat.shutdown_active_operations)
app.router.add_event_handler("shutdown", dsh_acp.shutdown_acp_runtime)
# Phase 1.1: resume SuiteJobs left queued/running by a previous process so a
# restart never strands a batch; per-case evidence is preserved and orphaned
# in-flight attempts are reconciled (failed/executor_interrupted, retryable).
app.router.add_event_handler("startup", platform.recover_stale_suite_jobs)
# Phase 3: opt-in run-index watcher (controlled polling). It only triggers
# index refreshes, never touches the main API path, and is always stopped.
app.router.add_event_handler("startup", run_index_watch.start_run_index_watcher)
app.router.add_event_handler("shutdown", run_index_watch.stop_run_index_watcher)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
