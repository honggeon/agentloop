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
from auditrail.integrations import import_jobs
from auditrail.integrations import langfuse as langfuse_adapter
from auditrail.integrations import promptfoo as promptfoo_adapter
from auditrail.trajectory import write_json

from backend.app.config import Settings

_LOCK = threading.RLock()

IMPORT_SOURCES = ("langfuse", "promptfoo")
_IDEMPOTENCY_FILE = "idempotency.json"

#: Payloads (source_file/config/association) are stored in a sidecar the job
#: API never returns verbatim; configs carry the env var *name* only.
_PAYLOADS_DIR = "payloads"
#: File imports larger than this default to the async ImportJob path.
ASYNC_FILE_THRESHOLD_BYTES = 4 * 1024 * 1024


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


def export_promptfoo_case(case_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    from backend.app import platform_store

    case = platform_store.get_test_case(case_id, settings)
    if case is None:
        return None
    return promptfoo_adapter.export_case(case)


# ---------------------------------------------------------------------------
# Phase 4 ImportJob orchestration
# ---------------------------------------------------------------------------


def resolve_import_source(source_file: Any, settings: Settings) -> Path:
    """Resolve an operator-supplied source path inside the allowed roots."""
    from auditrail.costs import resolve_import_path

    return resolve_import_path(source_file, allowed_import_roots(settings))


def _payload_path(settings: Settings, import_id: str) -> Path:
    return Path(settings.platform_dir) / ext.EXTERNAL_DIR / ext.IMPORTS_DIR / _PAYLOADS_DIR / ("%s.json" % import_id)


def save_import_payload(import_id: str, payload: Dict[str, Any], settings: Settings) -> None:
    """Persist the execution payload sidecar (source_file, kind, config,
    association, dry_run). Configs store the token env var *name* only; the
    token value never enters this file."""
    path = _payload_path(settings, import_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, payload)


def load_import_payload(import_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    path = _payload_path(settings, import_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def job_fingerprint(source: str, payload: Dict[str, Any]) -> str:
    """Idempotency fingerprint: source system + request content + options."""
    return hashlib.sha256(("%s|%s" % (source, import_fingerprint(payload))).encode("utf-8")).hexdigest()


def should_run_async(source: str, payload: Dict[str, Any], settings: Settings) -> bool:
    """Large or HTTP imports default to async; small dry-runs stay sync."""
    if bool(payload.get("dry_run")):
        return False
    if payload.get("async") is True or str(payload.get("mode") or "") == "async":
        return True
    if payload.get("sync") is True or str(payload.get("mode") or "") == "sync":
        return False
    if source == "langfuse":
        config = payload.get("config") if isinstance(payload.get("config"), dict) else {}
        if config.get("enabled") and not payload.get("source_file"):
            return True  # HTTP imports page over the network: async by default
    source_file = payload.get("source_file")
    if source_file:
        try:
            path = resolve_import_source(source_file, settings)
            return path.stat().st_size > ASYNC_FILE_THRESHOLD_BYTES
        except (ValueError, OSError):
            return False  # validation reports the error synchronously
    return False


def create_import_job(
    source: str,
    payload: Dict[str, Any],
    settings: Settings,
    *,
    actor: Optional[str] = None,
) -> Dict[str, Any]:
    """Create and persist a queued ImportJob plus its payload sidecar.

    Validation (kind, source_file presence, path safety) happens here so a
    malformed request fails synchronously with 400 instead of queuing a job
    that can never run.
    """
    kind = str(payload.get("kind") or ("promptfoo_config" if source == "promptfoo" else "traces")).strip()
    source_file = payload.get("source_file")
    config = payload.get("config") if isinstance(payload.get("config"), dict) else None
    if source == "langfuse":
        if kind not in ("traces", "scores"):
            raise ValueError("kind must be one of traces|scores")
        if source_file:
            resolve_import_source(source_file, settings)  # path safety now, not later
            source_type = "file"
        elif config and config.get("enabled"):
            langfuse_adapter.validate_config(config)  # SSRF/config validation upfront
            source_type = "http"
        else:
            raise ValueError("source_file is required (or an explicitly enabled HTTP config)")
    elif source == "promptfoo":
        if not source_file:
            raise ValueError("source_file is required")
        path = resolve_import_source(source_file, settings)
        source_type = "yaml" if str(path.name).lower().endswith((".yaml", ".yml")) else "file"
    else:
        raise ValueError("unsupported integration source %r" % source)

    dry_run = bool(payload.get("dry_run"))
    mode = "async" if should_run_async(source, payload, settings) and not dry_run else "sync"
    job = import_jobs.new_import_job(
        source_system=source,
        source_type=source_type,
        kind=kind,
        source_fingerprint=job_fingerprint(source, payload),
        mode=mode,
        dry_run=dry_run,
        options=payload.get("options") if isinstance(payload.get("options"), dict) else {},
        actor=actor,
    )
    with _LOCK:
        import_jobs.save_import_job(Path(settings.platform_dir), job)
        save_import_payload(
            str(job["import_id"]),
            {
                "source_file": source_file,
                "kind": kind,
                "dry_run": dry_run,
                "association": payload.get("association") if isinstance(payload.get("association"), dict) else None,
                "config": config,
            },
            settings,
        )
    return job


def import_report_response(job: Dict[str, Any]) -> Dict[str, Any]:
    """The Phase 3-compatible sync response: the embedded import report plus
    additive job fields (``mode``/``job_status``)."""
    report = job.get("report") if isinstance(job.get("report"), dict) else None
    if report is None:
        view = import_jobs.job_view(job)
        return {
            "import_id": view.get("import_id"),
            "source_system": view.get("source_system"),
            "kind": view.get("kind"),
            "status": view.get("status"),
            "mode": view.get("mode"),
            "job_status": view.get("status"),
        }
    body = dict(report)
    body["mode"] = job.get("mode")
    body["job_status"] = job.get("status")
    return body


def list_import_jobs(settings: Settings, *, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    records = import_jobs.list_import_jobs(Path(settings.platform_dir))
    total = len(records)
    limit = min(max(int(limit), 1), 200)
    offset = max(int(offset), 0)
    page = records[offset : offset + limit]
    return {
        "imports": [import_jobs.job_summary(record) for record in page],
        "count": total,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


def get_import_job(import_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    platform_dir = Path(settings.platform_dir)
    import_jobs.refresh_stale_jobs(platform_dir)
    record = import_jobs.load_import_job(platform_dir, import_id)
    if record is None:
        # Legacy Phase 3 report files stay readable.
        legacy = ext.load_external_import_report(platform_dir, import_id)
        if legacy is None:
            return None
        return import_jobs.job_view(legacy)
    return import_jobs.job_view(record)


# ---------------------------------------------------------------------------
# Phase 4 promptfoo draft review / promotion
# ---------------------------------------------------------------------------


def list_promptfoo_drafts(settings: Settings, *, limit: int = 50, offset: int = 0) -> Dict[str, Any]:
    drafts = [promptfoo_adapter.public_draft(item) for item in promptfoo_adapter.load_drafts(Path(settings.platform_dir))]
    for draft in drafts:
        draft.setdefault("draft_id", draft.get("case_id"))
        draft.setdefault("status", "draft")
        draft.setdefault("declared_executable", bool(draft.get("forbidden_keys")))
    total = len(drafts)
    limit = min(max(int(limit), 1), 200)
    offset = max(int(offset), 0)
    return {
        "drafts": drafts[offset : offset + limit],
        "total": total,
        "count": total,
        "limit": limit,
        "offset": offset,
        "has_more": offset + limit < total,
    }


def get_promptfoo_draft(draft_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    draft = promptfoo_adapter.get_draft(Path(settings.platform_dir), draft_id)
    return promptfoo_adapter.public_draft(draft) if draft is not None else None


class DraftReviewError(ValueError):
    """Structured draft review failure (mapped to 400/409 by the route)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(str(message)[:300])
        self.code = str(code)


def _draft_or_error(draft_id: str, settings: Settings) -> Dict[str, Any]:
    draft = promptfoo_adapter.get_draft(Path(settings.platform_dir), draft_id)
    if draft is None:
        raise KeyError(draft_id)
    return draft


def approve_promptfoo_draft(
    draft_id: str,
    settings: Settings,
    *,
    actor: Optional[str] = None,
    input_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Promote one draft to a formal TestCase (idempotent).

    - re-validates schema and safety fields at approve time;
    - drafts declaring executable content (command/plugin/script/provider
      execution) can never be approved: the platform has no safe execution
      capability for them, and no confirmation flag changes that;
    - unmapped assertions are preserved as declarative, non-executable
      expectations;
    - repeat approval returns the originally created TestCase.
    """
    from backend.app import platform_store

    draft = _draft_or_error(draft_id, settings)
    if str(draft.get("status") or "draft") == "approved":
        # Idempotent replay: the draft already produced its TestCase.
        case_id = str(draft.get("promoted_case_id") or draft.get("case_id") or "")
        case = platform_store.get_test_case(case_id, settings)
        return {"draft": promptfoo_adapter.public_draft(draft), "test_case": case, "idempotent_replay": True}
    if str(draft.get("status") or "draft") != "draft":
        raise DraftReviewError(
            "draft_invalid_state", "draft 当前状态为 %s，不能 approve" % draft.get("status")
        )
    if draft.get("declared_executable"):
        raise DraftReviewError(
            "draft_invalid_state",
            "draft 声明了 command/plugin/script 等可执行内容；平台没有安全执行能力，不能 approve",
        )
    existing = platform_store.get_test_case(str(draft.get("case_id") or ""), settings)
    existing_version = None
    if existing is not None:
        source_ref = existing.get("source_ref") if isinstance(existing.get("source_ref"), dict) else {}
        if source_ref.get("source_id") == str(draft.get("draft_id")):
            # Already promoted by an earlier approve: replay, never duplicate.
            return {"draft": promptfoo_adapter.public_draft(draft), "test_case": existing, "idempotent_replay": True}
        existing_version = existing.get("version") if isinstance(existing.get("version"), int) else None
    try:
        payload = promptfoo_adapter.build_test_case_payload(
            draft,
            actor=actor,
            input_override=input_override,
            existing_version=existing_version,
        )
    except ValueError as exc:
        raise DraftReviewError("draft_invalid_state", str(exc)) from exc
    # Re-validate the exact payload through the platform schema before storing.
    case = platform_store.create_test_case(payload, settings)
    updated, _changed = promptfoo_adapter.update_draft_status(
        Path(settings.platform_dir),
        str(draft.get("draft_id")),
        "approved",
        actor=actor,
        promoted_case_id=case["case_id"],
    )
    return {"draft": promptfoo_adapter.public_draft(updated), "test_case": case, "idempotent_replay": False}


def review_promptfoo_draft(
    draft_id: str,
    action: str,
    settings: Settings,
    *,
    actor: Optional[str] = None,
    reason: Optional[str] = None,
) -> Dict[str, Any]:
    """Reject or archive one draft (idempotent on repeat)."""
    if action not in ("rejected", "archived"):
        raise DraftReviewError("draft_invalid_state", "unsupported draft action %r" % action)
    draft = _draft_or_error(draft_id, settings)
    if str(draft.get("status") or "draft") == action:
        return {"draft": promptfoo_adapter.public_draft(draft), "idempotent_replay": True}
    try:
        updated, _changed = promptfoo_adapter.update_draft_status(
            Path(settings.platform_dir), str(draft.get("draft_id")), action, actor=actor
        )
    except ValueError as exc:
        raise DraftReviewError("draft_invalid_state", str(exc)) from exc
    return {"draft": promptfoo_adapter.public_draft(updated), "idempotent_replay": False}


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
