"""Run Contract v1: ``run.json`` lifecycle metadata and ``error.json``.

``run.json`` carries only platform metadata and lifecycle state — never
session-derived summary fields (those stay in ``summary.json``). All writes go
through :func:`auditrail.trajectory.write_json`, which stages in the same
directory and atomically replaces, so an interrupted process never leaves a
half-written ``run.json``; at most a ``.*.tmp`` staging file remains, which is
excluded from the artifact manifest.

Legacy runs (no ``run.json``) are read through :func:`load_run_state`, which
infers ``status=completed`` from existing report files and returns ``null``
references, without writing anything back.

Lifecycle state machine: ``queued -> running -> completed|failed|timed_out|cancelled|skipped``.
These statuses are distinct from the judge overall status
(``success|partial|failed|unknown``) and dimension status
(``pass|partial|fail|unknown``); they must never be mixed.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .trajectory import write_json

SCHEMA_VERSION = 1

#: Legal values for ``run.json.status`` (Run lifecycle only).
RUN_STATUSES = ("queued", "running", "completed", "failed", "timed_out", "cancelled", "skipped")
TERMINAL_STATUSES = frozenset({"completed", "failed", "timed_out", "cancelled", "skipped"})

#: Formal run artifacts eligible for ``artifact_manifest``. Staging files
#: (``.*.tmp``) and ``run.json`` itself are never listed; ``run.json`` is
#: always the last file replaced.
MANIFEST_CANDIDATES = (
    "session.json",
    "summary.json",
    "trajectory.jsonl",
    "audit.json",
    "tool_audit.md",
    "source.json",
    "expectations.json",
    "llm_calls.json",
    "evaluation.json",
    "agent_judge.json",
    "agent_judge.md",
    "chart_data.json",
    "error.json",
)

RUN_JSON = "run.json"
ERROR_JSON = "error.json"

#: Structured error codes for the Phase 0 error protocol
#: ``{code, message, run_id?, stage?, retryable?, external_status?}``.
ERROR_CODES = {
    "endpoint_rejected",
    "capability_not_supported",
    "unsupported_adapter",
    "agent_unreachable",
    "agent_timeout",
    "agent_http_error",
    "agent_stream_error",
    "invalid_agent_response",
    "evaluation_invalid",
    "internal_error",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return "%s-%s-%s" % (prefix, stamp, uuid.uuid4().hex[:8])


def new_run_record(
    run_id: str,
    *,
    agent_ref: Optional[Dict[str, Any]] = None,
    test_case_ref: Optional[Dict[str, Any]] = None,
    source_ref: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Create a fresh ``queued`` Run Contract v1 record."""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "status": "queued",
        "status_reason": None,
        "started_at": None,
        "finished_at": None,
        "agent_ref": agent_ref,
        "test_case_ref": test_case_ref,
        "suite_ref": None,
        "experiment_ref": None,
        "prompt_ref": None,
        "model_ref": None,
        "source_ref": source_ref or {"system": "platform", "source_id": None},
        "artifact_manifest": [],
    }


def validate_status(status: Any) -> str:
    text = str(status or "").strip()
    if text not in RUN_STATUSES:
        raise ValueError("run status must be one of %s, got %r" % (list(RUN_STATUSES), status))
    return text


def write_run_record(run_dir: Path, record: Dict[str, Any]) -> None:
    """Validate and atomically write ``run.json``."""
    validate_status(record.get("status"))
    record = dict(record)
    record["schema_version"] = SCHEMA_VERSION
    record.setdefault("artifact_manifest", [])
    write_json(Path(run_dir) / RUN_JSON, record)


def read_run_record(run_dir: Path) -> Optional[Dict[str, Any]]:
    """Read ``run.json``; return ``None`` when absent or unparseable."""
    path = Path(run_dir) / RUN_JSON
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("status") not in RUN_STATUSES:
        return None
    return data


def update_run_status(
    run_dir: Path,
    status: str,
    *,
    status_reason: Optional[str] = None,
    refresh_manifest: bool = False,
) -> Dict[str, Any]:
    """Atomically advance the lifecycle state of an existing run record."""
    run_dir = Path(run_dir)
    record = read_run_record(run_dir)
    if record is None:
        raise FileNotFoundError("missing %s in %s" % (RUN_JSON, run_dir))
    record["status"] = validate_status(status)
    record["status_reason"] = status_reason
    now = utc_now()
    if status == "running" and not record.get("started_at"):
        record["started_at"] = now
    if status in TERMINAL_STATUSES:
        record["finished_at"] = now
    if refresh_manifest or status in TERMINAL_STATUSES:
        record["artifact_manifest"] = collect_artifact_manifest(run_dir)
    write_run_record(run_dir, record)
    return record


def collect_artifact_manifest(run_dir: Path) -> List[str]:
    """List formal artifacts that exist and parse; staging files never count."""
    run_dir = Path(run_dir)
    manifest = []
    for name in MANIFEST_CANDIDATES:
        path = run_dir / name
        if not path.is_file():
            continue
        if name.endswith(".json") and not _parses_as_json(path):
            continue
        manifest.append(name)
    return manifest


def _parses_as_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False
    return True


def write_error(
    run_dir: Path,
    *,
    phase: str,
    error_type: str,
    retryable: bool,
    safe_message: str,
    external_status: Optional[int] = None,
) -> Dict[str, Any]:
    """Atomically write ``error.json`` for a run that failed before/during collection.

    ``safe_message`` must already be redacted; raw exception text with secrets
    must never be persisted here.
    """
    payload = {
        "schema_version": SCHEMA_VERSION,
        "phase": str(phase or "unknown"),
        "error_type": str(error_type or "internal_error"),
        "retryable": bool(retryable),
        "safe_message": str(safe_message or "")[:500],
        "external_status": external_status,
        "timestamp": utc_now(),
    }
    write_json(Path(run_dir) / ERROR_JSON, payload)
    return payload


def read_error(run_dir: Path) -> Optional[Dict[str, Any]]:
    path = Path(run_dir) / ERROR_JSON
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def load_run_state(run_dir: Path) -> Dict[str, Any]:
    """Return the Run Contract view of a run directory, compatibly.

    Runs without ``run.json`` (everything written before the contract existed)
    are inferred as ``completed`` when report artifacts are present, else
    ``failed``; all references are ``null``. Nothing is written back.
    """
    run_dir = Path(run_dir)
    record = read_run_record(run_dir)
    if record is not None:
        state = dict(record)
        state["legacy_inferred"] = False
        return state
    manifest = collect_artifact_manifest(run_dir)
    has_reports = any(name in manifest for name in ("summary.json", "session.json", "audit.json"))
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_dir.name,
        "status": "completed" if has_reports else "failed",
        "status_reason": None if has_reports else "missing run.json and report artifacts",
        "started_at": None,
        "finished_at": None,
        "agent_ref": None,
        "test_case_ref": None,
        "suite_ref": None,
        "experiment_ref": None,
        "prompt_ref": None,
        "model_ref": None,
        "source_ref": None,
        "artifact_manifest": manifest,
        "legacy_inferred": True,
    }


def safe_status_subset(state: Dict[str, Any], *, stale: bool = False) -> Dict[str, Any]:
    """Project a run state to the fields the status API may expose."""
    return {
        "run_id": state.get("run_id"),
        "status": state.get("status"),
        "status_reason": state.get("status_reason"),
        "started_at": state.get("started_at"),
        "finished_at": state.get("finished_at"),
        "agent_ref": state.get("agent_ref"),
        "test_case_ref": state.get("test_case_ref"),
        "suite_ref": state.get("suite_ref"),
        "experiment_ref": state.get("experiment_ref"),
        "prompt_ref": state.get("prompt_ref"),
        "model_ref": state.get("model_ref"),
        "artifact_manifest": list(state.get("artifact_manifest") or []),
        "legacy_inferred": bool(state.get("legacy_inferred")),
        "stale": bool(stale),
    }


def is_active_status(status: Any) -> bool:
    return status in {"queued", "running"}


def safe_message_for(exc: BaseException, limit: int = 300) -> str:
    """Redact an exception to a short single-line message safe for artifacts/APIs."""
    text = " ".join(str(exc).split())
    return text[:limit]


def manifests_equal(left: Iterable[str], right: Iterable[str]) -> bool:
    return sorted(left) == sorted(right)
