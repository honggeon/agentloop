"""Phase 3/4: owns the process-wide run-index watcher instance.

The watcher itself lives in ``auditrail.run_index_watcher`` (core logic); this
module only wires it into the FastAPI lifecycle and exposes its status to the
admin API. The watcher is strictly opt-in (``AUDITRAIL_RUN_INDEX_WATCHER``),
never blocks the main API, and is always stopped on shutdown. After a process
restart nothing assumes a watcher ever ran: the status simply reports
``disabled``/``stopped``.

Phase 4 adds pause/resume: the paused flag is persisted in the index meta
file (``run_index.set_paused``) so a restart restores the operator's choice
without ever triggering an implicit full rebuild. Manual rebuilds stay
available while paused.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Optional

from auditrail import run_index
from auditrail import run_index_watcher as watcher_mod

from backend.app import cost_service
from backend.app.config import Settings, get_settings

_LOCK = threading.RLock()
_WATCHER: Optional[watcher_mod.RunIndexWatcher] = None


def start_run_index_watcher(settings: Optional[Settings] = None) -> None:
    """Startup hook: start the watcher only when explicitly enabled.

    Restores the persisted paused state; startup never triggers a full
    rebuild on its own.
    """
    global _WATCHER
    current = settings or get_settings()
    if not current.run_index_watcher_enabled:
        return
    with _LOCK:
        if _WATCHER is not None:
            return
        _WATCHER = watcher_mod.watcher_from_env(
            Path(current.runs_dir),
            Path(current.platform_dir),
            enabled=True,
            paused=run_index.is_paused(Path(current.platform_dir)),
            cost_lookup=cost_service.cost_lookup(current),
            debounce_ms=current.run_index_debounce_ms,
            max_batch=current.run_index_max_batch,
            poll_interval_ms=current.run_index_poll_interval_ms,
        )


def stop_run_index_watcher() -> None:
    """Shutdown hook: stop the thread and release resources."""
    global _WATCHER
    with _LOCK:
        watcher = _WATCHER
        _WATCHER = None
    if watcher is not None:
        watcher.stop()


def watcher_status(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Status block for the admin API; never raises."""
    current = settings or get_settings()
    with _LOCK:
        watcher = _WATCHER
    if watcher is None:
        status = watcher_mod.disabled_status()
        if current.run_index_watcher_enabled:
            status.update({"enabled": True, "status": "stopped"})
        status["paused"] = run_index.is_paused(Path(current.platform_dir))
        return status
    status = watcher.status()
    status["paused"] = run_index.is_paused(Path(current.platform_dir)) or status.get("paused", False)
    return status


def pause_watcher(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Pause automatic refreshes (persisted); manual rebuild stays allowed."""
    current = settings or get_settings()
    run_index.set_paused(Path(current.platform_dir), True)
    with _LOCK:
        watcher = _WATCHER
    if watcher is not None:
        watcher.pause()
    status = watcher_status(current)
    status["paused"] = True
    return status


def resume_watcher(settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Resume automatic refreshes (persisted)."""
    current = settings or get_settings()
    run_index.set_paused(Path(current.platform_dir), False)
    with _LOCK:
        watcher = _WATCHER
    if watcher is not None:
        watcher.resume()
    status = watcher_status(current)
    status["paused"] = False
    return status


def notify_import_complete(settings: Optional[Settings] = None) -> None:
    """Hint the watcher that an import finished (costs may have changed)."""
    with _LOCK:
        watcher = _WATCHER
    if watcher is not None:
        watcher.notify(reason="import_complete")
