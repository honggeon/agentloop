"""Phase 3: owns the process-wide run-index watcher instance.

The watcher itself lives in ``auditrail.run_index_watcher`` (core logic); this
module only wires it into the FastAPI lifecycle and exposes its status to the
admin API. The watcher is strictly opt-in (``AUDITRAIL_RUN_INDEX_WATCHER``),
never blocks the main API, and is always stopped on shutdown. After a process
restart nothing assumes a watcher ever ran: the status simply reports
``disabled``/``stopped``.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, Optional

from auditrail import run_index_watcher as watcher_mod

from backend.app import cost_service
from backend.app.config import Settings, get_settings

_LOCK = threading.RLock()
_WATCHER: Optional[watcher_mod.RunIndexWatcher] = None


def start_run_index_watcher(settings: Optional[Settings] = None) -> None:
    """Startup hook: start the watcher only when explicitly enabled."""
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
        if not current.run_index_watcher_enabled:
            return watcher_mod.disabled_status()
        status = watcher_mod.disabled_status()
        status.update({"enabled": True, "status": "stopped"})
        return status
    return watcher.status()
