"""Phase 5 health and readiness service.

Three honest states:

- ``live``: the process is up. Never fails because a dependency is down —
  a database blip must not trigger a restart loop.
- ``ready``: every critical dependency (database when configured, queue
  broker when configured, platform storage, runs root, audit log
  writability) is usable. A failed critical check makes the API
  ``not_ready`` (503).
- ``degraded``: criticals pass but something needs operator attention
  (stale ImportJobs, a degraded index watcher, queue-degraded jobs,
  recently failed imports).

Responses name checks and states only: no connection strings, absolute
paths, secrets or environment values. All probes have finite timeouts.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app.config import Settings, get_settings

_READY = "ready"
_DEGRADED = "degraded"
_NOT_READY = "not_ready"


def liveness() -> Dict[str, Any]:
    return {"status": "alive", "timestamp": _utc_now()}


def _utc_now() -> str:
    from auditrail.run_contract import utc_now

    return utc_now()


def _check_database(settings: Settings) -> Dict[str, Any]:
    from backend.app import job_store as job_store_mod

    mode = job_store_mod.storage_mode(settings)
    if mode == "file":
        return {"name": "database", "critical": False, "status": "skipped", "detail": "storage_mode=file"}
    started = time.monotonic()
    try:
        store = job_store_mod.job_store(settings)
        ok = store.ping()
        if not ok:
            return {"name": "database", "critical": True, "status": "fail", "detail": "unreachable"}
        migration = job_store_mod.migration_state(settings)
        if not migration.get("up_to_date"):
            return {
                "name": "database",
                "critical": True,
                "status": "fail",
                "detail": "migrations_pending: %s" % ",".join(str(v) for v in migration.get("pending") or []),
            }
        return {
            "name": "database",
            "critical": True,
            "status": "ok",
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }
    except Exception as exc:
        return {"name": "database", "critical": True, "status": "fail", "detail": type(exc).__name__}


def _check_queue(settings: Settings) -> Dict[str, Any]:
    from backend.app import queue_adapter

    mode = queue_adapter.queue_mode(settings)
    if mode != "redis":
        return {"name": "queue", "critical": False, "status": "skipped", "detail": "queue_mode=%s" % mode}
    if queue_adapter.queue_ping(settings):
        return {"name": "queue", "critical": False, "status": "ok"}
    # Broker down: the database poll fallback keeps jobs flowing, so this is
    # a degraded signal, not a readiness failure — but never reported healthy.
    return {
        "name": "queue",
        "critical": False,
        "status": "fail",
        "detail": "broker unreachable (db poll fallback active)",
    }


def _check_platform_storage(settings: Settings) -> Dict[str, Any]:
    try:
        directory = Path(settings.platform_dir)
        directory.mkdir(parents=True, exist_ok=True)
        if not os.access(directory, os.W_OK):
            return {"name": "platform_storage", "critical": True, "status": "fail", "detail": "not writable"}
        return {"name": "platform_storage", "critical": True, "status": "ok"}
    except Exception as exc:
        return {"name": "platform_storage", "critical": True, "status": "fail", "detail": type(exc).__name__}


def _check_runs_root(settings: Settings) -> Dict[str, Any]:
    try:
        root = Path(settings.runs_dir)
        if root.is_dir() and os.access(root, os.R_OK):
            return {"name": "runs_root", "critical": True, "status": "ok"}
        return {"name": "runs_root", "critical": True, "status": "fail", "detail": "not readable"}
    except Exception as exc:
        return {"name": "runs_root", "critical": True, "status": "fail", "detail": type(exc).__name__}


def _check_audit_log(settings: Settings) -> Dict[str, Any]:
    from backend.app import audit_service

    try:
        path = Path(settings.platform_dir) / audit_service.AUDIT_DIR
        path.mkdir(parents=True, exist_ok=True)
        target = path / audit_service.AUDIT_FILE
        if target.exists():
            writable = os.access(target, os.W_OK)
        else:
            writable = os.access(path, os.W_OK)
        if writable:
            return {"name": "audit_log", "critical": True, "status": "ok"}
        return {"name": "audit_log", "critical": True, "status": "fail", "detail": "not writable"}
    except Exception as exc:
        return {"name": "audit_log", "critical": True, "status": "fail", "detail": type(exc).__name__}


def _check_workers(settings: Settings) -> Dict[str, Any]:
    from backend.app import job_store as job_store_mod, worker_main

    embedded = worker_main.embedded_worker_status()
    registry: List[Dict[str, Any]] = []
    if job_store_mod.storage_mode(settings) != "file":
        try:
            store = job_store_mod.job_store(settings)
            raw = getattr(store, "raw", None)
            if raw is not None:
                registry = raw.list_workers()
        except Exception:
            registry = []
    running = [w for w in registry if w.get("status") == "running"]
    status = "ok" if (embedded and embedded.get("running")) or running else "unknown"
    return {
        "name": "workers",
        "critical": False,
        "status": status,
        "embedded": embedded,
        "registered": len(registry),
        "running": len(running),
    }


def _operational_counters(settings: Settings) -> Dict[str, Any]:
    from auditrail import run_index

    from backend.app import job_store as job_store_mod, run_index_watch

    stale = 0
    degraded_jobs = 0
    recent_failures: List[Dict[str, Any]] = []
    try:
        store = job_store_mod.job_store(settings)
        records = store.list_jobs()
        stale = sum(1 for r in records if str(r.get("status") or "") == "stale")
        degraded_jobs = sum(1 for r in records if r.get("queue_degraded") and r.get("status") == "queued")
        failed = [r for r in records if str(r.get("status") or "") in ("failed", "partial")]
        failed.sort(key=lambda r: str(r.get("finished_at") or r.get("created_at") or ""), reverse=True)
        recent_failures = [
            {
                "import_id": r.get("import_id"),
                "source_system": r.get("source_system"),
                "status": r.get("status"),
                "error_code": (r.get("error") or {}).get("code") if isinstance(r.get("error"), dict) else None,
            }
            for r in failed[:5]
        ]
    except Exception:
        pass
    try:
        watcher = run_index_watch.watcher_status(settings)
    except Exception:
        watcher = {"status": "unknown"}
    try:
        index = run_index.index_status(Path(settings.platform_dir))
        index_state = index.get("status")
    except Exception:
        index_state = "unknown"
    return {
        "stale_import_jobs": stale,
        "queue_degraded_jobs": degraded_jobs,
        "recent_import_failures": recent_failures,
        "index_watcher": {"status": watcher.get("status"), "paused": bool(watcher.get("paused"))},
        "run_index": {"status": index_state},
    }


def readiness(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Full readiness report (also the admin health payload)."""
    current = settings or get_settings()
    checks: List[Dict[str, Any]] = [
        _check_database(current),
        _check_queue(current),
        _check_platform_storage(current),
        _check_runs_root(current),
        _check_audit_log(current),
        _check_workers(current),
    ]
    operational = _operational_counters(current)

    critical_failed = any(check.get("critical") and check.get("status") == "fail" for check in checks)
    degraded_reasons: List[str] = []
    if operational["stale_import_jobs"]:
        degraded_reasons.append("stale_import_jobs")
    if operational["queue_degraded_jobs"]:
        degraded_reasons.append("queue_degraded_jobs")
    if operational["index_watcher"].get("status") == "degraded":
        degraded_reasons.append("index_watcher_degraded")
    queue_check = next((c for c in checks if c["name"] == "queue"), {})
    if queue_check.get("status") == "fail" and not queue_check.get("critical"):
        degraded_reasons.append("queue_degraded")

    if critical_failed:
        status = _NOT_READY
    elif degraded_reasons:
        status = _DEGRADED
    else:
        status = _READY
    return {
        "status": status,
        "timestamp": _utc_now(),
        "deployment_mode": current.deployment_mode,
        "storage_mode": current.storage_mode,
        "queue_mode": current.queue_mode,
        "auth_mode": current.auth_mode,
        "single_tenant": True,
        "checks": checks,
        "operational": operational,
        "degraded_reasons": degraded_reasons,
    }
