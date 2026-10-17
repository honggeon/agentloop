"""Controlled run-index watcher (Phase 3).

The incremental run index is normally refreshed lazily on read. This module
adds an *opt-in* background watcher that triggers index refreshes when the
runs directory changes.

Design constraints:

- **disabled by default** — created and started only through explicit
  configuration (``AUDITRAIL_RUN_INDEX_WATCHER``);
- the environment has no reliable file-watcher dependency (``watchdog`` is
  not installed), so this is a **controlled polling** implementation — it is
  always labelled ``mode="polling"``, never disguised as an OS watcher;
- only the runs *root directory* is observed; ``.tmp`` staging files, hidden
  directories and the platform directory never trigger refreshes;
- events are debounced and bounded (``max_batch``): a burst of file changes
  collapses into at most one refresh per debounce window, never an infinite
  rebuild loop;
- the watcher only triggers :func:`auditrail.run_index.ensure_index` — it
  never executes file content, never replaces consistency checks, and a
  watcher failure never affects the main API (errors flip the status to
  ``degraded`` and are counted);
- the background thread is stoppable and joined on shutdown; after a process
  restart nothing assumes the watcher ever ran.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import run_index
from .run_contract import utc_now

WATCHER_STATUSES = ("disabled", "running", "degraded", "stopped", "paused")

DEFAULT_DEBOUNCE_MS = 500
DEFAULT_MAX_BATCH = 1000
DEFAULT_POLL_INTERVAL_MS = 2000

#: Why a refresh ran: ``file_change`` (fingerprint moved), ``scheduled``
#: (external notify), ``import_complete`` (an import finished, costs may have
#: changed), ``recovery`` (first pass after (re)start), ``manual`` (API).
REFRESH_REASONS = run_index.REFRESH_REASONS


def _cheap_fingerprint(runs_dir: Path) -> Tuple[Tuple[str, int, int], ...]:
    """A cheap signature of the runs root: run dir names plus their newest
    signature-artifact mtime/size. Hidden dirs, ``.tmp`` staging leftovers and
    symlinks are ignored exactly like the index itself."""
    root = Path(runs_dir)
    if not root.is_dir():
        return ()
    fingerprint: List[Tuple[str, int, int]] = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return ()
    for child in children:
        if child.name.startswith(".") or child.name.endswith(".tmp"):
            continue
        try:
            if not child.is_dir() or child.is_symlink():
                continue
        except OSError:
            continue
        newest_mtime = 0
        total_size = 0
        for name in run_index._SIGNATURE_FILES:
            try:
                stat = (child / name).stat()
            except OSError:
                continue
            newest_mtime = max(newest_mtime, stat.st_mtime_ns)
            total_size += stat.st_size
        fingerprint.append((child.name, newest_mtime, total_size))
    return tuple(fingerprint)


class RunIndexWatcher:
    """A debounced, stoppable polling refresher for the run index."""

    def __init__(
        self,
        runs_dir: Path,
        platform_dir: Path,
        *,
        cost_lookup: Optional[Callable[[str], Dict[str, Any]]] = None,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        max_batch: int = DEFAULT_MAX_BATCH,
        poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
        sleeper: Optional[Callable[[float], None]] = None,
    ) -> None:
        self._runs_dir = Path(runs_dir)
        self._platform_dir = Path(platform_dir)
        self._cost_lookup = cost_lookup
        self._debounce_seconds = max(int(debounce_ms), 0) / 1000.0
        self._max_batch = max(int(max_batch), 1)
        self._poll_seconds = max(int(poll_interval_ms), 100) / 1000.0
        self._sleeper = sleeper
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._status = "stopped"
        self._paused = False
        self._last_event_at: Optional[str] = None
        self._last_refresh_at: Optional[str] = None
        self._last_refresh_reason: Optional[str] = None
        self._pending_events = 0
        self._pending_reason: Optional[str] = None
        self._error_count = 0
        self._failure_count = 0
        self._last_error: Optional[str] = None
        self._last_fingerprint: Optional[Tuple[Tuple[str, int, int], ...]] = None

    # ------------------------------------------------------------------ API

    def start(self, *, paused: bool = False) -> None:
        """Start the polling thread (idempotent). ``paused=True`` restores a
        persisted paused state without triggering any refresh."""
        with self._lock:
            self._paused = bool(paused)
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._status = "paused" if self._paused else "running"
            self._last_fingerprint = _cheap_fingerprint(self._runs_dir)
            self._thread = threading.Thread(
                target=self._run_loop,
                name="auditrail-run-index-watcher",
                daemon=True,
            )
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the thread and release resources (idempotent)."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        with self._lock:
            self._thread = None
            if self._status != "degraded":
                self._status = "stopped"

    def pause(self) -> None:
        """Stop automatic refreshes; manual rebuild stays available."""
        with self._lock:
            self._paused = True
            if self._status == "running":
                self._status = "paused"

    def resume(self) -> None:
        """Resume automatic refreshes after a pause."""
        with self._lock:
            self._paused = False
            if self._status == "paused":
                self._status = "running"

    def notify(self, *, reason: str = "scheduled") -> None:
        """Record an external change hint (bounded, debounced)."""
        with self._lock:
            if self._pending_events < self._max_batch:
                self._pending_events += 1
            self._pending_reason = reason if reason in REFRESH_REASONS else "scheduled"
            self._last_event_at = utc_now()

    def status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "mode": "polling",
                "status": self._status,
                "paused": self._paused,
                "poll_interval_ms": int(self._poll_seconds * 1000),
                "debounce_ms": int(self._debounce_seconds * 1000),
                "max_batch": self._max_batch,
                "last_event_at": self._last_event_at,
                "last_refresh_at": self._last_refresh_at,
                "last_refresh_reason": self._last_refresh_reason,
                "pending_events": self._pending_events,
                "error_count": self._error_count,
                "failure_count": self._failure_count,
                "last_error": self._last_error,
            }

    # --------------------------------------------------------------- worker

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if self._sleeper is not None:
                self._sleeper(self._poll_seconds)
            else:
                self._stop_event.wait(self._poll_seconds)
            if self._stop_event.is_set():
                break
            with self._lock:
                if self._paused:
                    continue
            try:
                fingerprint = _cheap_fingerprint(self._runs_dir)
            except Exception as exc:
                self._record_error(exc)
                continue
            if fingerprint == self._last_fingerprint and self._pending_events == 0:
                continue
            reason = "scheduled"
            if self._last_fingerprint is not None and fingerprint != self._last_fingerprint:
                reason = "file_change"
            with self._lock:
                if self._pending_reason:
                    reason = self._pending_reason if self._pending_events else reason
            if reason == "file_change":
                self.notify(reason="file_change")
            self._last_fingerprint = fingerprint
            # Debounce: collapse bursts into one refresh per window.
            if self._sleeper is not None:
                self._sleeper(self._debounce_seconds)
            else:
                self._stop_event.wait(self._debounce_seconds)
            if self._stop_event.is_set():
                break
            with self._lock:
                if self._paused:
                    self._pending_events = 0
                    self._pending_reason = None
                    continue
            self._refresh(reason)

    def _refresh(self, reason: str = "scheduled") -> None:
        try:
            run_index.ensure_index(
                self._runs_dir,
                self._platform_dir,
                cost_lookup=self._cost_lookup,
                reason=reason if reason in REFRESH_REASONS else "scheduled",
            )
        except Exception as exc:
            self._record_error(exc)
            with self._lock:
                self._pending_events = 0
                self._pending_reason = None
            return
        with self._lock:
            self._pending_events = 0
            self._pending_reason = None
            self._last_refresh_at = utc_now()
            self._last_refresh_reason = reason
            self._failure_count = 0
            self._last_error = None
            if self._status == "degraded":
                self._status = "paused" if self._paused else "running"

    def _record_error(self, exc: Optional[BaseException] = None) -> None:
        with self._lock:
            self._error_count += 1
            self._failure_count += 1
            # Redacted: exception type only, never the raw message (which may
            # carry paths or unexpected content).
            self._last_error = type(exc).__name__ if exc is not None else "unknown"
            self._status = "degraded"


def disabled_status() -> Dict[str, Any]:
    """The watcher status block when the watcher is not configured."""
    return {
        "enabled": False,
        "mode": "polling",
        "status": "disabled",
        "paused": False,
        "last_event_at": None,
        "last_refresh_at": None,
        "last_refresh_reason": None,
        "pending_events": 0,
        "error_count": 0,
        "failure_count": 0,
        "last_error": None,
    }


def watcher_from_env(
    runs_dir: Path,
    platform_dir: Path,
    *,
    enabled: bool,
    paused: bool = False,
    cost_lookup: Optional[Callable[[str], Dict[str, Any]]] = None,
    debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    max_batch: int = DEFAULT_MAX_BATCH,
    poll_interval_ms: int = DEFAULT_POLL_INTERVAL_MS,
) -> Optional[RunIndexWatcher]:
    """Create and start a watcher only when explicitly enabled. A persisted
    paused state is restored without triggering a full rebuild."""
    if not enabled:
        return None
    watcher = RunIndexWatcher(
        runs_dir,
        platform_dir,
        cost_lookup=cost_lookup,
        debounce_ms=debounce_ms,
        max_batch=max_batch,
        poll_interval_ms=poll_interval_ms,
    )
    watcher.start(paused=paused)
    return watcher
