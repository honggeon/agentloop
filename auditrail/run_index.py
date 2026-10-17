"""Phase 2 incremental run index: a local, file-based cache over ``runs/``.

The index exists to make list/stats/overview reads fast when the runs
directory grows large. It is a *cache*, never evidence:

- run detail pages and quality verdicts still read the formal run artifacts;
- entries carry summary fields only — no prompts, no trajectory content, no
  secrets;
- a missing or corrupt index never blocks reads: callers fall back to a safe
  directory scan.

Storage (atomic writes via ``auditrail.trajectory.write_json``)::

    platform/
      run_index.json        # {"schema_version": 1, "entries": {run_id: {...}}}
      run_index_meta.json   # {"schema_version": 1, "status": "ready", ...}

Incremental behavior: ``ensure_index`` diffs the runs directory against the
stored entries — new or changed run directories (detected via a content
signature of key artifact mtimes/sizes) are re-read, vanished runs are
dropped, and ``.<name>.*.tmp`` staging files / hidden dirs never enter the
index. One bad run directory is recorded in the meta ``errors`` summary and
never aborts the scan.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import run_contract
from .run_contract import utc_now
from .trajectory import write_json

SCHEMA_VERSION = 1

INDEX_FILE = "run_index.json"
META_FILE = "run_index_meta.json"

#: Index lifecycle statuses surfaced by the API.
STATUS_READY = "ready"
STATUS_UNAVAILABLE = "unavailable"

#: Meta error summary bound; full detail stays out of the index.
_MAX_META_ERRORS = 20

_LOCK = threading.RLock()

#: Artifacts whose mtime/size fingerprint an entry. ``run.json`` carries the
#: lifecycle and references; the others change when judge/cost/token data
#: lands. Trajectory content itself is never fingerprinted into the index.
_SIGNATURE_FILES = (
    "run.json",
    "agent_judge.json",
    "evaluation.json",
    "audit.json",
    "llm_calls.json",
)


def _index_path(platform_dir: Path) -> Path:
    return Path(platform_dir) / INDEX_FILE


def _meta_path(platform_dir: Path) -> Path:
    return Path(platform_dir) / META_FILE


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def load_index(platform_dir: Path) -> Optional[Dict[str, Any]]:
    """Load the index; ``None`` when missing or corrupt (caller falls back)."""
    path = _index_path(platform_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    if not isinstance(data.get("entries"), dict):
        return None
    return data


def load_meta(platform_dir: Path) -> Optional[Dict[str, Any]]:
    path = _meta_path(platform_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def index_status(platform_dir: Path) -> Dict[str, Any]:
    """Public status snapshot for the admin/status API."""
    meta = load_meta(platform_dir)
    index = load_index(platform_dir)
    if meta is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "status": STATUS_UNAVAILABLE if index is not None else "missing",
            "last_scan_at": None,
            "indexed_count": len(index.get("entries") or {}) if index else 0,
            "error_count": 0,
            "errors": [],
        }
    status = str(meta.get("status") or STATUS_READY)
    if index is None:
        status = STATUS_UNAVAILABLE
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "last_scan_at": meta.get("last_scan_at"),
        "indexed_count": int(meta.get("indexed_count") or 0),
        "error_count": int(meta.get("error_count") or 0),
        "errors": list(meta.get("errors") or [])[:_MAX_META_ERRORS],
    }


# ---------------------------------------------------------------------------
# entry construction
# ---------------------------------------------------------------------------


def _signature(run_dir: Path) -> List[List[int]]:
    """(mtime_ns, size) pairs for the fingerprinted artifacts."""
    signature = []
    for name in _SIGNATURE_FILES:
        path = run_dir / name
        try:
            stat = path.stat()
        except OSError:
            signature.append([0, 0])
            continue
        signature.append([stat.st_mtime_ns, stat.st_size])
    return signature


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _token_count(run_dir: Path) -> Optional[int]:
    """Sum observed LLM tokens for a run; ``None`` when no LLM data exists."""
    path = run_dir / "llm_calls.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    entries = data.get("llm_calls") if isinstance(data, dict) else data
    if not isinstance(entries, list):
        return None
    total = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        usage = entry.get("tokenUsage") or entry.get("token_usage") or entry.get("usage") or {}
        containers = [entry, usage] if isinstance(usage, dict) else [entry]
        for container in containers:
            found = False
            for key in ("totalTokens", "total_tokens", "total"):
                value = container.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    total += int(value)
                    found = True
                    break
            if found:
                break
    return total


def _iter_run_dirs(runs_dir: Path):
    root = Path(runs_dir)
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        # Hidden dirs and atomic-write staging leftovers never enter the index.
        if child.name.startswith(".") or child.name.endswith(".tmp"):
            continue
        if child.is_dir() and not child.is_symlink():
            yield child


def build_entry(
    run_dir: Path,
    *,
    runs_dir: Path,
    cost_lookup: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build one index entry from the formal run artifacts. Raises nothing
    for missing artifacts (legacy runs); genuinely unreadable state raises and
    is recorded by the caller as an index error."""
    run_dir = Path(run_dir)
    state = run_contract.load_run_state(run_dir)
    agent_ref = state.get("agent_ref") or {}
    suite_ref = state.get("suite_ref") or {}
    test_case_ref = state.get("test_case_ref") or {}
    source = _read_json(run_dir / "source.json") or {}

    judge = _read_json(run_dir / "agent_judge.json")
    judge_overall_status: Optional[str] = None
    completion_score: Optional[float] = None
    if judge is not None:
        judge_overall_status = str(judge.get("overall_status") or "unknown")
        score = judge.get("completion_score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            completion_score = float(score)

    manifest = state.get("artifact_manifest") or []
    started_at = state.get("started_at") if isinstance(state.get("started_at"), str) else None
    finished_at = state.get("finished_at") if isinstance(state.get("finished_at"), str) else None

    total_cost: Optional[float] = None
    cost_source = "missing"
    if cost_lookup is not None:
        try:
            summary = cost_lookup(run_dir.name) or {}
        except Exception:
            summary = {}
        value = summary.get("total")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total_cost = float(value)
        source_name = str(summary.get("source") or "missing")
        cost_source = source_name if source_name else "missing"

    effective = _parse_iso(started_at) or _parse_iso(finished_at)
    return {
        "run_id": str(state.get("run_id") or run_dir.name),
        "path": "%s/%s" % (Path(runs_dir).name, run_dir.name),
        "agent_id": str(agent_ref.get("id") or "") or None,
        "agent_name": str(agent_ref.get("name") or "") or None,
        "agent_environment": str(agent_ref.get("environment") or "") or None,
        "source_host": str(source.get("server_host") or "").lower() or None,
        "suite_id": str(suite_ref.get("id") or suite_ref.get("suite_id") or "") or None,
        "job_id": str(suite_ref.get("job_id") or "") or None,
        "case_id": str(test_case_ref.get("id") or test_case_ref.get("case_id") or "") or None,
        "case_execution_id": str(suite_ref.get("case_execution_id") or "") or None,
        "attempt_id": str(suite_ref.get("attempt_id") or "") or None,
        "retry_index": suite_ref.get("retry_index") if isinstance(suite_ref.get("retry_index"), int) else None,
        "status": str(state.get("status") or "failed"),
        "judge_overall_status": judge_overall_status,
        "completion_score": completion_score,
        "started_at": started_at,
        "finished_at": finished_at,
        "updated_at": finished_at or started_at,
        "has_trajectory": "trajectory.jsonl" in manifest or (run_dir / "trajectory.jsonl").is_file(),
        "has_audit": "audit.json" in manifest or (run_dir / "audit.json").is_file(),
        "has_evaluation": "evaluation.json" in manifest or (run_dir / "evaluation.json").is_file(),
        "has_judge": judge is not None,
        "legacy_inferred": bool(state.get("legacy_inferred")),
        "token_count": _token_count(run_dir),
        "total_cost": total_cost,
        "cost_source": cost_source,
        # Internal bookkeeping: effective timestamp for range filtering and the
        # content signature for incremental updates. Both are index metadata,
        # never business timestamps.
        "effective_ts": effective.isoformat().replace("+00:00", "Z") if effective else None,
        "signature": _signature(run_dir),
    }


# ---------------------------------------------------------------------------
# incremental ensure / rebuild
# ---------------------------------------------------------------------------


def ensure_index(
    runs_dir: Path,
    platform_dir: Path,
    *,
    cost_lookup: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Tuple[Optional[Dict[str, Dict[str, Any]]], Dict[str, Any]]:
    """Return ``(entries, status_block)``; ``entries=None`` means fall back.

    Applies incremental updates when a valid index exists, otherwise performs
    a full build. A corrupt index is reported as ``unavailable`` with
    ``entries=None`` so callers fall back to a safe scan; use
    :func:`rebuild_index` to recover explicitly.

    The runs-directory scan and per-entry reads happen *outside* the lock —
    only the short read/merge/write of the index files themselves is locked,
    and those writes are atomic (staging file + ``os.replace``), so readers
    never observe a half-written index.
    """
    stored = load_index(platform_dir)
    if stored is None and _index_path(platform_dir).is_file():
        # Corrupt or schema-mismatched index: do not trust it. The caller
        # falls back to a safe scan; an explicit rebuild recovers.
        meta = load_meta(platform_dir) or {}
        return None, {
            "used": False,
            "status": STATUS_UNAVAILABLE,
            "last_scan_at": meta.get("last_scan_at"),
            "indexed_count": 0,
            "error_count": int(meta.get("error_count") or 0) + 1,
        }
    entries, meta = _scan(runs_dir, stored, cost_lookup=cost_lookup)
    with _LOCK:
        _write_index(platform_dir, entries, meta, previous=stored)
    return entries, {
        "used": True,
        "status": STATUS_READY,
        "last_scan_at": meta.get("last_scan_at"),
        "indexed_count": len(entries),
        "error_count": len(meta.get("errors") or []),
    }


def rebuild_index(
    runs_dir: Path,
    platform_dir: Path,
    *,
    cost_lookup: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Explicit full rebuild (admin API); returns the fresh status block."""
    entries, meta = _scan(runs_dir, None, cost_lookup=cost_lookup)
    with _LOCK:
        _write_index(platform_dir, entries, meta, previous=None, force=True)
    return index_status(platform_dir)


def _scan(
    runs_dir: Path,
    stored: Optional[Dict[str, Any]],
    *,
    cost_lookup: Optional[Callable[[str], Dict[str, Any]]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Any]]:
    """Diff the runs directory against ``stored`` without holding any lock."""
    runs_dir = Path(runs_dir)
    previous = (stored or {}).get("entries") or {}
    entries: Dict[str, Dict[str, Any]] = {}
    errors: List[Dict[str, str]] = []

    for run_dir in _iter_run_dirs(runs_dir):
        run_id = run_dir.name
        old = previous.get(run_id)
        signature = _signature(run_dir)
        if old is not None and old.get("signature") == signature:
            entries[run_id] = old
            continue
        try:
            entries[run_id] = build_entry(run_dir, runs_dir=runs_dir, cost_lookup=cost_lookup)
        except Exception as exc:  # one bad run never blocks the whole index
            errors.append({"run_id": run_id, "stage": "index", "message": "index build failed: %s" % type(exc).__name__})
            if old is not None:
                entries[run_id] = old
    meta = {
        "schema_version": SCHEMA_VERSION,
        "status": STATUS_READY,
        "last_scan_at": utc_now(),
        "indexed_count": len(entries),
        "error_count": len(errors),
        "errors": errors[:_MAX_META_ERRORS],
    }
    return entries, meta


def _write_index(
    platform_dir: Path,
    entries: Dict[str, Dict[str, Any]],
    meta: Dict[str, Any],
    *,
    previous: Optional[Dict[str, Any]],
    force: bool = False,
) -> None:
    """Persist index + meta atomically, skipping unchanged writes.

    Vanished runs must not keep serving stale paths, so any membership change
    forces a write even when no single entry changed.
    """
    old_entries = (previous or {}).get("entries") or {}
    changed = force or set(old_entries) != set(entries) or any(
        old_entries.get(run_id) != entry for run_id, entry in entries.items()
    )
    if not changed and _index_path(platform_dir).is_file():
        return
    Path(platform_dir).mkdir(parents=True, exist_ok=True)
    write_json(_index_path(platform_dir), {"schema_version": SCHEMA_VERSION, "entries": entries})
    write_json(_meta_path(platform_dir), meta)
