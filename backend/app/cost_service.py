"""Phase 2 backend glue for cost records: combines the persistent store
(imported/estimated) with observed records derived from run artifacts, and
wires the run index's per-run cost lookup.

Observed data always comes from the run's own ``llm_calls.json`` and is never
persisted into the cost store, so imports can never overwrite it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from auditrail import costs as cost_mod
from auditrail import run_index

from backend.app import platform_store
from backend.app.config import Settings


def stored_records(settings: Settings) -> List[Dict[str, Any]]:
    return cost_mod.load_cost_records(Path(settings.platform_dir))


def price_table(settings: Settings) -> Dict[str, Any]:
    return cost_mod.load_price_table(Path(settings.platform_dir))


def cost_lookup(settings: Settings) -> Callable[[str], Dict[str, Any]]:
    """``run_id -> cost summary`` for index builds and per-run rollups."""
    records = stored_records(settings)
    runs_dir = Path(settings.runs_dir)

    def lookup(run_id: str) -> Dict[str, Any]:
        combined = cost_mod.observed_records_for_run(runs_dir / run_id) + cost_mod.records_for_run(records, run_id)
        return cost_mod.summarize_records(combined)

    return lookup


def run_costs(run_id: str, settings: Settings) -> Dict[str, Any]:
    records = stored_records(settings)
    observed = cost_mod.observed_records_for_run(Path(settings.runs_dir) / run_id)
    stored = cost_mod.records_for_run(records, run_id)
    return {
        "run_id": run_id,
        "records": observed + stored,
        "summary": cost_mod.summarize_records(observed + stored),
    }


def _attributed_run_ids(agent_id: str, settings: Settings) -> List[str]:
    """Runs attributed to the agent, preferring the run index.

    Falls back to ``None`` (caller scans) only when the index itself is
    unavailable; an empty list is a real answer.
    """
    entries, _status = run_index.ensure_index(
        Path(settings.runs_dir), Path(settings.platform_dir), cost_lookup=cost_lookup(settings)
    )
    if entries is None:
        return []
    config = platform_store.get_agent(agent_id, settings)
    endpoint_host = ""
    if config:
        from urllib.parse import urlparse

        endpoint_host = (urlparse(str(config.get("endpoint") or "")).hostname or "").lower()
    matched = []
    for run_id, entry in entries.items():
        ref = entry.get("agent_id")
        if ref:
            if ref == agent_id:
                matched.append(run_id)
        elif endpoint_host and entry.get("source_host") == endpoint_host:
            matched.append(run_id)
    return matched


def agent_costs(agent_id: str, settings: Settings) -> Dict[str, Any]:
    records = stored_records(settings)
    run_ids = _attributed_run_ids(agent_id, settings)
    run_set = set(run_ids)
    combined: List[Dict[str, Any]] = [
        record
        for record in records
        if str(record.get("agent_id") or "") == agent_id or str(record.get("run_id") or "") in run_set
    ]
    for run_id in run_ids:
        combined.extend(cost_mod.observed_records_for_run(Path(settings.runs_dir) / run_id))
    return {
        "agent_id": agent_id,
        "records": combined,
        "summary": cost_mod.summarize_records(combined),
    }


def import_costs(payload: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    """Validate an import request and run the import pipeline.

    The source file must resolve inside the runs or platform directory —
    absolute paths outside them, traversal and symlink escapes are rejected by
    ``auditrail.costs.resolve_import_path``.
    """
    source_system = str(payload.get("source_system") or "").strip()
    if not source_system:
        raise ValueError("source_system is required")
    source_file = payload.get("source_file")
    if source_file is None:
        raise ValueError("source_file is required")
    allowed_roots = [Path(settings.runs_dir), Path(settings.platform_dir)]
    path = cost_mod.resolve_import_path(source_file, allowed_roots)
    default_run_id = payload.get("run_id")
    if default_run_id is not None:
        default_run_id = str(default_run_id).strip() or None
    default_agent_id = payload.get("agent_id")
    if default_agent_id is not None:
        default_agent_id = str(default_agent_id).strip() or None
    return cost_mod.import_cost_file(
        Path(settings.platform_dir),
        path,
        source_system=source_system,
        default_run_id=default_run_id,
        default_agent_id=default_agent_id,
        dry_run=bool(payload.get("dry_run")),
    )


def get_import_report(import_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    return cost_mod.load_import_report(Path(settings.platform_dir), import_id)


# ---------------------------------------------------------------------------
# Phase 5: cost imports run through the unified ImportJob state machine
# ---------------------------------------------------------------------------


def create_cost_import_job(
    payload: Dict[str, Any],
    settings: Settings,
    *,
    actor: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and persist a cost ImportJob (``kind="cost_records"``).

    Same state machine as Langfuse/promptfoo imports: checkpointed, paused/
    resumed/cancelled/retried identically. The per-record ``source_system``
    (e.g. ``litellm``) is preserved for provenance; dispatch keys on
    ``kind``. Small/dry-run imports stay synchronous, large files go async.
    """
    from auditrail.integrations import import_jobs

    from backend.app import integration_service, job_store as job_store_mod

    source_system = str(payload.get("source_system") or "").strip()
    if not source_system:
        raise ValueError("source_system is required")
    if len(source_system) > 64:
        raise ValueError("source_system must be at most 64 characters")
    source_file = payload.get("source_file")
    if source_file is None:
        raise ValueError("source_file is required")
    path = integration_service.resolve_import_source(source_file, settings)  # path safety now
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError("source_file is not readable: %s" % type(exc).__name__) from exc
    if size > cost_mod.MAX_IMPORT_BYTES:
        # Synchronous validation parity with the Phase 2 API contract (400).
        raise ValueError("source_file exceeds the %d byte limit" % cost_mod.MAX_IMPORT_BYTES)

    dry_run = bool(payload.get("dry_run"))
    explicit_async = payload.get("async") is True or str(payload.get("mode") or "") == "async"
    large = False
    try:
        large = path.stat().st_size > integration_service.ASYNC_FILE_THRESHOLD_BYTES
    except OSError:
        large = False
    mode = "sync" if dry_run else ("async" if (explicit_async or large) else "sync")

    fingerprint = integration_service.job_fingerprint("costs", payload)
    job = import_jobs.new_import_job(
        source_system=source_system,
        source_type="file",
        kind="cost_records",
        source_fingerprint=fingerprint,
        mode=mode,
        dry_run=dry_run,
        actor=actor,
    )
    job["request_fingerprint"] = fingerprint
    if idempotency_key:
        job["idempotency_key"] = str(idempotency_key)[:128]
    default_run_id = payload.get("run_id")
    default_agent_id = payload.get("agent_id")
    job_store_mod.job_store(settings).save(job)
    integration_service.save_import_payload(
        str(job["import_id"]),
        {
            "source_file": source_file,
            "kind": "cost_records",
            "dry_run": dry_run,
            "run_id": str(default_run_id).strip() if default_run_id else None,
            "agent_id": str(default_agent_id).strip() if default_agent_id else None,
        },
        settings,
    )
    return job


def get_cost_import(import_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    """Job-aware read with legacy Phase 2 report fallback."""
    from auditrail.integrations import import_jobs

    from backend.app import job_store as job_store_mod

    store = job_store_mod.job_store(settings)
    store.refresh_stale()
    record = store.load(import_id)
    if record is not None and str(record.get("kind") or "") == "cost_records":
        return import_jobs.job_view(record)
    legacy = get_import_report(import_id, settings)
    if legacy is not None:
        legacy = dict(legacy)
        legacy.setdefault("legacy_report", True)
        return legacy
    if record is not None:
        return import_jobs.job_view(record)
    return None
