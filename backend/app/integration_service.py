"""Phase 3 integration service: orchestration between the HTTP layer and the
``auditrail.integrations`` adapters.

Responsibilities (HTTP assembly only — normalization/linking rules live in
the core adapters):

- build the platform association context (run ids, session ids, agent time
  windows) used to link external traces;
- resolve operator-supplied import paths inside the allowed roots;
- implement ``Idempotency-Key`` semantics for imports (same key + same
  fingerprint replays the original import report; same key + different
  fingerprint is a 409);
- read external evidence for run/agent views.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from auditrail import run_contract
from auditrail.integrations import external_models as ext
from auditrail.integrations import langfuse as langfuse_adapter
from auditrail.integrations import promptfoo as promptfoo_adapter
from auditrail.trajectory import write_json

from backend.app.config import Settings

_LOCK = threading.RLock()

IMPORT_SOURCES = ("langfuse", "promptfoo")
_IDEMPOTENCY_FILE = "idempotency.json"


# ---------------------------------------------------------------------------
# association context
# ---------------------------------------------------------------------------


def _iter_run_dirs(runs_dir: Path):
    root = Path(runs_dir)
    if not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if child.name.startswith(".") or child.name.endswith(".tmp"):
            continue
        if child.is_dir() and not child.is_symlink():
            yield child


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def build_link_context(settings: Settings) -> Dict[str, Any]:
    """Association knowledge for external trace linking.

    - ``run_ids``: every platform run directory;
    - ``sessions``: ``session_id -> run_id`` from each run's ``source.json``;
    - ``agent_runs``: ``agent_id -> [{run_id, started_at, finished_at}]`` for
      weak time-window linking.
    """
    context = langfuse_adapter.new_run_context()
    for run_dir in _iter_run_dirs(Path(settings.runs_dir)):
        run_id = run_dir.name
        context["run_ids"].add(run_id)
        state = run_contract.load_run_state(run_dir)
        source = _read_json(run_dir / "source.json") or {}
        session_id = source.get("session_id") or source.get("sessionId")
        if isinstance(session_id, str) and session_id.strip():
            context["sessions"][session_id.strip()] = run_id
        agent_ref = state.get("agent_ref") or {}
        agent_id = agent_ref.get("id")
        if agent_id:
            context["agent_runs"].setdefault(str(agent_id), []).append(
                {
                    "run_id": run_id,
                    "started_at": state.get("started_at"),
                    "finished_at": state.get("finished_at"),
                }
            )
    return context


def allowed_import_roots(settings: Settings) -> List[Path]:
    return [Path(settings.platform_dir), Path(settings.runs_dir)]


# ---------------------------------------------------------------------------
# import idempotency
# ---------------------------------------------------------------------------


def import_fingerprint(payload: Dict[str, Any]) -> str:
    """Stable fingerprint of an import request (content, not timing)."""
    relevant = {
        key: payload.get(key)
        for key in ("source_file", "kind", "dry_run", "association", "config", "options")
        if payload.get(key) is not None
    }
    text = json.dumps(relevant, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _idempotency_path(settings: Settings) -> Path:
    return Path(settings.platform_dir) / ext.EXTERNAL_DIR / _IDEMPOTENCY_FILE


def lookup_import_idempotency(key: str, settings: Settings) -> Optional[Dict[str, Any]]:
    path = _idempotency_path(settings)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    entry = data.get(key) if isinstance(data, dict) else None
    return entry if isinstance(entry, dict) else None


def store_import_idempotency(key: str, fingerprint: str, import_id: str, settings: Settings) -> None:
    with _LOCK:
        path = _idempotency_path(settings)
        data: Dict[str, Any] = {}
        if path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                loaded = {}
            if isinstance(loaded, dict):
                data = loaded
        data[key] = {"fingerprint": fingerprint, "import_id": import_id}
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, data)


# ---------------------------------------------------------------------------
# import execution
# ---------------------------------------------------------------------------


def run_langfuse_import(
    payload: Dict[str, Any],
    settings: Settings,
    *,
    fetcher: Optional[Any] = None,
) -> Dict[str, Any]:
    kind = str(payload.get("kind") or "traces").strip()
    if kind not in ("traces", "scores"):
        raise ValueError("kind must be one of traces|scores")
    source_file = payload.get("source_file")
    config = payload.get("config") if isinstance(payload.get("config"), dict) else None
    if source_file:
        source: Dict[str, Any] = {"kind": "file", "path": source_file}
    elif config and config.get("enabled"):
        source = {"kind": "http"}
    else:
        raise ValueError("source_file is required (or an explicitly enabled HTTP config)")
    roots = allowed_import_roots(settings)
    options = payload.get("options") if isinstance(payload.get("options"), dict) else {}
    options = dict(options)
    options["dry_run"] = bool(payload.get("dry_run"))
    association = payload.get("association") if isinstance(payload.get("association"), dict) else None
    if association:
        options["association"] = association

    if kind == "traces":
        return langfuse_adapter.import_traces(
            Path(settings.platform_dir),
            source,
            allowed_roots=roots,
            options=options,
            config=config,
            run_context=build_link_context(settings),
            fetcher=fetcher,
        )
    return langfuse_adapter.import_scores(
        Path(settings.platform_dir),
        source,
        allowed_roots=roots,
        options=options,
        config=config,
        fetcher=fetcher,
    )


def run_promptfoo_import(payload: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    source_file = payload.get("source_file")
    if not source_file:
        raise ValueError("source_file is required")
    return promptfoo_adapter.import_file(
        Path(settings.platform_dir),
        source_file,
        allowed_roots=allowed_import_roots(settings),
        dry_run=bool(payload.get("dry_run")),
    )


def validate_source(source: str, payload: Dict[str, Any], settings: Settings) -> Dict[str, Any]:
    """Validate an integration config/source without importing anything."""
    if source == "langfuse":
        errors: List[str] = []
        normalized: Optional[Dict[str, Any]] = None
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        try:
            normalized = langfuse_adapter.validate_config(config)
        except ValueError as exc:
            errors.append(str(exc)[:300])
        if payload.get("source_file"):
            try:
                from auditrail.costs import resolve_import_path

                resolve_import_path(payload.get("source_file"), allowed_import_roots(settings))
            except ValueError as exc:
                errors.append(str(exc)[:300])
        return {"source_system": source, "valid": not errors, "errors": errors, "normalized_config": normalized}
    if source == "promptfoo":
        if not payload.get("source_file"):
            return {
                "source_system": source,
                "valid": False,
                "errors": ["source_file is required"],
                "normalized_config": None,
            }
        try:
            report = run_promptfoo_import({"source_file": payload.get("source_file"), "dry_run": True}, settings)
        except ValueError as exc:
            return {"source_system": source, "valid": False, "errors": [str(exc)[:300]], "normalized_config": None}
        return {
            "source_system": source,
            "valid": int(report.get("invalid_records") or 0) == 0,
            "errors": list(report.get("errors") or []),
            "warnings": list(report.get("warnings") or []),
            "total_records": report.get("total_records"),
            "normalized_config": None,
        }
    raise ValueError("unsupported integration source %r" % source)


# ---------------------------------------------------------------------------
# external evidence reads
# ---------------------------------------------------------------------------


def run_external_evaluations(run_id: str, settings: Settings) -> Dict[str, Any]:
    records = ext.evaluations_for_run(ext.load_external_evaluations(Path(settings.platform_dir)), run_id)
    return {"run_id": run_id, "count": len(records), "external_evaluations": records}


def run_external_traces(run_id: str, settings: Settings) -> Dict[str, Any]:
    records = ext.traces_for_run(ext.load_external_traces(Path(settings.platform_dir)), run_id)
    return {"run_id": run_id, "count": len(records), "external_traces": records}


def agent_external_evaluations(agent_id: str, settings: Settings) -> Dict[str, Any]:
    platform_dir = Path(settings.platform_dir)
    evaluations = ext.evaluations_for_agent(ext.load_external_evaluations(platform_dir), agent_id)
    traces = ext.traces_for_agent(ext.load_external_traces(platform_dir), agent_id)
    return {
        "agent_id": agent_id,
        "count": len(evaluations),
        "external_evaluations": evaluations,
        "external_summary": ext.external_summary(evaluations, traces),
    }


def list_imports(settings: Settings) -> Dict[str, Any]:
    reports = ext.list_external_import_reports(Path(settings.platform_dir))
    summaries = [
        {
            "import_id": report.get("import_id"),
            "source_system": report.get("source_system"),
            "kind": report.get("kind"),
            "status": report.get("status"),
            "total_records": report.get("total_records"),
            "imported_records": report.get("imported_records"),
            "duplicate_records": report.get("duplicate_records"),
            "unlinked_records": report.get("unlinked_records"),
            "invalid_records": report.get("invalid_records"),
            "imported_at": report.get("imported_at"),
        }
        for report in reports
    ]
    return {"imports": summaries, "count": len(summaries)}


def get_import(import_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    return ext.load_external_import_report(Path(settings.platform_dir), import_id)


def export_promptfoo_case(case_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    from backend.app import platform_store

    case = platform_store.get_test_case(case_id, settings)
    if case is None:
        return None
    return promptfoo_adapter.export_case(case)


def external_evidence_for_runs(run_ids: List[str], settings: Settings) -> Dict[str, Any]:
    """Bounded external evidence block for a set of runs (SuiteJob detail)."""
    platform_dir = Path(settings.platform_dir)
    all_evaluations = ext.load_external_evaluations(platform_dir)
    all_traces = ext.load_external_traces(platform_dir)
    evaluations: List[Dict[str, Any]] = []
    traces: List[Dict[str, Any]] = []
    run_set = {str(item) for item in run_ids}
    for record in all_evaluations:
        if str(record.get("run_id") or "") in run_set:
            evaluations.append(record)
    for record in all_traces:
        linked = (record.get("link") or {}).get("run_ids") or []
        if str(record.get("run_id") or "") in run_set or run_set & {str(item) for item in linked}:
            traces.append(record)
    return {
        "summary": ext.external_summary(evaluations, traces),
        "external_evaluations": evaluations[:100],
        "external_traces": traces[:100],
        "note": "外部证据仅用于展示，不参与 deterministic audit、judge overall status 或 score cap",
    }
