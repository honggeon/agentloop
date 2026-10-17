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
