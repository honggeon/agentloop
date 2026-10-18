from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from backend.app.auth_middleware import AuthMiddleware
from backend.app.config import get_settings, validate_startup_settings
from backend.app.metrics import HttpMetricsMiddleware
from backend.app.routes import admin, auth_admin, costs, dashboard, dsh, dsh_acp, dsh_chat, integrations, judge, live, platform, prices, runs
from backend.app import health as health_mod
from backend.app import import_worker, run_index_watch, worker_main


settings = get_settings()
app = FastAPI(title="Auditrail")

# Phase 5: auth resolution runs first (inner), metrics outermost so rejected
# requests are still counted.
app.add_middleware(AuthMiddleware)
app.add_middleware(HttpMetricsMiddleware)

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
app.include_router(admin.router)
app.include_router(auth_admin.router)


def _validate_startup() -> None:
    validate_startup_settings()


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
# Phase 4: ImportJob worker — marks expired-lease imports stale on startup
# (never auto-completes them) and stops accepting work on shutdown.
app.router.add_event_handler("startup", import_worker.start_import_worker)
app.router.add_event_handler("shutdown", import_worker.stop_import_worker)
# Phase 5: fail fast on impossible production configurations (e.g.
# production + disabled auth), and start the embedded cross-process worker
# loop when the queue mode calls for it.
app.router.add_event_handler("startup", _validate_startup)
app.router.add_event_handler("startup", worker_main.start_embedded_worker)
app.router.add_event_handler("shutdown", worker_main.stop_embedded_worker)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/health/live")
def health_live() -> dict:
    """Liveness: the process is up. Dependency failures never flip this —
    a database blip must not cause a restart loop."""
    return health_mod.liveness()


@app.get("/api/health/ready")
def health_ready() -> Any:
    """Readiness: critical dependencies usable. 503 when not ready."""
    from fastapi.responses import JSONResponse

    report = health_mod.readiness(get_settings())
    status_code = 200 if report["status"] in ("ready", "degraded") else 503
    return JSONResponse(status_code=status_code, content=report)


@app.get("/api/metrics")
def metrics_endpoint() -> PlainTextResponse:
    """Prometheus text exposition. No secrets, no high-cardinality labels."""
    from backend.app import metrics as metrics_mod

    _register_operational_gauges()
    return PlainTextResponse(metrics_mod.render_prometheus(), media_type="text/plain; version=0.0.4")


def _register_operational_gauges() -> None:
    """Scrape-time gauges (cheap callbacks, registered once)."""
    from backend.app import metrics as metrics_mod

    metrics_mod.gauge_callback(
        "auditrail_import_jobs_active",
        _gauge_active_import_jobs,
        _help="ImportJobs in active statuses",
    )
    metrics_mod.gauge_callback(
        "auditrail_import_job_stale_total",
        _gauge_stale_import_jobs,
        _help="ImportJobs currently stale",
    )
    metrics_mod.gauge_callback(
        "auditrail_run_index_errors_total_current",
        _gauge_index_errors,
        _help="Run index watcher consecutive failure count",
    )
    metrics_mod.gauge_callback(
        "auditrail_run_index_refreshes_total_current",
        _gauge_index_refreshes,
        _help="Run index recorded refresh count",
    )


def _gauge_index_errors() -> list:
    from backend.app import run_index_watch

    try:
        status = run_index_watch.watcher_status(get_settings())
    except Exception:
        return []
    return [({}, float(status.get("failure_count") or 0))]


def _gauge_index_refreshes() -> list:
    from auditrail import run_index

    try:
        status = run_index.index_status(Path(get_settings().platform_dir))
    except Exception:
        return []
    history = status.get("refresh_history") or []
    return [({}, float(len(history)))]


def _gauge_active_import_jobs() -> list:
    from backend.app import job_store as job_store_mod

    try:
        records = job_store_mod.job_store(get_settings()).list_jobs()
    except Exception:
        return []
    active = sum(1 for record in records if str(record.get("status") or "") in ("queued", "validating", "running", "paused"))
    return [({}, float(active))]


def _gauge_stale_import_jobs() -> list:
    from backend.app import job_store as job_store_mod

    try:
        records = job_store_mod.job_store(get_settings()).list_jobs()
    except Exception:
        return []
    stale = sum(1 for record in records if str(record.get("status") or "") == "stale")
    return [({}, float(stale))]
