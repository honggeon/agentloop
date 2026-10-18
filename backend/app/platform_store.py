"""Phase 0/1/2 platform config store: Agents, TestCases, TestSuites and SuiteJobs.

No database, no queue: ``platform/agents.json`` and ``platform/test_cases.json``
hold the Agent and unified TestCase configs; ``platform/test_suites.json``
holds TestSuite configs; ``platform/suite_jobs/<job_id>.json`` holds one
SuiteJob record per batch launch; ``platform/idempotency.json`` and
``platform/suite_idempotency.json`` hold POST /api/runs and SuiteJob launch
idempotency keys for this single deployment. All writes
are atomic via ``auditrail.trajectory.write_json``. Credentials never enter
these files — Agent configs reference endpoints only.

Phase 2 adds formal Agent metadata: ``description``, ``environment``,
``tags``, ``enabled``, ``created_at``/``updated_at`` and a display-only
``source_host`` derived from the endpoint. Old configs missing these fields
stay valid: ``created_at`` reads as ``None`` (the file mtime is never used to
fake a business timestamp), ``environment`` stays ``None`` and is never
normalized to ``production``. Updating an Agent never rewrites historical
``run.json`` ``agent_ref`` values, and deleting an Agent never deletes runs —
the agent simply shows up as derived/missing-configuration afterwards.

Test cases come from two sources: the unified store (``adapter`` format) and
legacy ``cases/*.json`` suites, which are exposed read-only through the
``auditrail.testcases`` compatibility mapping (``adapter="command"``).
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from auditrail import testsuites as suite_mod
from auditrail.run_contract import utc_now
from auditrail.testcases import normalize_test_case
from auditrail.trajectory import write_json

from backend.app.config import PROJECT_ROOT, Settings, get_settings

_LOCK = threading.RLock()

_AGENTS_FILE = "agents.json"
_TEST_CASES_FILE = "test_cases.json"
_TEST_SUITES_FILE = "test_suites.json"
_SUITE_JOBS_DIR = "suite_jobs"
_IDEMPOTENCY_FILE = "idempotency.json"


def _platform_dir(settings: Optional[Settings] = None) -> Path:
    current = settings or get_settings()
    return Path(current.platform_dir)


def _read_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [item for item in data["items"] if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def _write_list(path: Path, items: List[Dict[str, Any]]) -> None:
    write_json(path, {"items": items})


# -------------------------------------------------------------------- agents

#: Recognized environments. Custom values are preserved as-is (and returned
#: verbatim by the API) rather than silently normalized — ``None`` means the
#: operator never declared one, which is never reported as ``production``.
KNOWN_ENVIRONMENTS = ("local", "development", "staging", "production", "unknown")

_MAX_DESCRIPTION = 2000
_MAX_TAGS = 32
_MAX_TAG_LENGTH = 64
_MAX_ENVIRONMENT_LENGTH = 64
_MAX_METADATA_ENTRIES = 32

#: metadata keys that may carry credentials or private prompts; rejected on
#: intake instead of being stored.
_SECRET_METADATA_FRAGMENTS = (
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "api-key",
    "token",
    "secret",
    "password",
    "credential",
    "prompt",
)


def list_agents(settings: Optional[Settings] = None) -> List[Dict[str, Any]]:
    return _read_list(_platform_dir(settings) / _AGENTS_FILE)


def get_agent(agent_id: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    for agent in list_agents(settings):
        if str(agent.get("agent_id") or "") == agent_id:
            return agent
    return None


def _validate_environment(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("environment must be a string or null")
    text = value.strip()
    if not text:
        return None
    if len(text) > _MAX_ENVIRONMENT_LENGTH:
        raise ValueError("environment must be at most %d characters" % _MAX_ENVIRONMENT_LENGTH)
    return text


def _validate_tags(value: Any) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("tags must be a list of strings")
    if len(value) > _MAX_TAGS:
        raise ValueError("tags must contain at most %d entries" % _MAX_TAGS)
    tags = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("tags must be a list of strings")
        text = item.strip()
        if not text or len(text) > _MAX_TAG_LENGTH:
            raise ValueError("each tag must be 1-%d characters" % _MAX_TAG_LENGTH)
        if text not in tags:
            tags.append(text)
    return tags


def _validate_metadata(metadata: Any) -> Dict[str, Any]:
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    if len(metadata) > _MAX_METADATA_ENTRIES:
        raise ValueError("metadata must contain at most %d entries" % _MAX_METADATA_ENTRIES)
    for key, value in metadata.items():
        lowered = str(key).lower()
        if any(fragment in lowered for fragment in _SECRET_METADATA_FRAGMENTS):
            raise ValueError("credentials and private prompts must not be stored in agent metadata")
        if isinstance(value, str) and _looks_like_secret(value):
            raise ValueError("credentials must not be stored in agent metadata; use environment variables")
    return dict(metadata)


def _validate_endpoint_credentials(endpoint: str) -> None:
    """Reject endpoints that embed plaintext credentials (user:pass@host)."""
    from urllib.parse import urlparse

    parsed = urlparse(endpoint)
    if parsed.username or parsed.password:
        raise ValueError("endpoint must not embed credentials; use environment variables")


def _source_host_for(endpoint: str) -> Optional[str]:
    """Display/attribution host derived from the endpoint — never a name."""
    from urllib.parse import urlparse

    host = (urlparse(endpoint).hostname or "").lower()
    return host or None


def create_agent(payload: Dict[str, Any], settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Validate and store one Agent config. Raises ``ValueError`` on problems."""
    agent_id = str(payload.get("agent_id") or "").strip()
    if not agent_id or "/" in agent_id or "\\" in agent_id or agent_id in {".", ".."}:
        raise ValueError("agent_id is required and must be a plain identifier")
    name = str(payload.get("name") or "").strip() or agent_id
    adapter = str(payload.get("adapter") or "http_session").strip()
    if adapter != "http_session":
        raise ValueError("Phase 0 supports adapter=http_session only")
    endpoint = str(payload.get("endpoint") or "").strip()
    if not endpoint:
        raise ValueError("endpoint is required")
    _validate_endpoint_credentials(endpoint)
    capabilities = payload.get("capabilities") or ["session"]
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise ValueError("capabilities must be a list of strings")
    metadata = _validate_metadata(payload.get("metadata"))
    description = str(payload.get("description") or "")[:_MAX_DESCRIPTION]
    environment = _validate_environment(payload.get("environment"))
    tags = _validate_tags(payload.get("tags"))
    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean")
    max_concurrency = _validate_max_concurrency(payload.get("max_concurrency"))

    now = utc_now()
    agent = {
        "agent_id": agent_id,
        "name": name,
        "version": str(payload.get("version") or ""),
        "adapter": adapter,
        "endpoint": endpoint,
        "capabilities": capabilities,
        "metadata": metadata,
        "max_concurrency": max_concurrency,
        # Phase 2 formal metadata. ``created_at`` is written only here, on
        # creation; it is never derived from file mtimes.
        "description": description,
        "environment": environment,
        "tags": tags,
        "enabled": enabled,
        "source_host": _source_host_for(endpoint),
        "created_at": now,
        "updated_at": now,
        "config_hash": _config_hash(agent_id, endpoint, capabilities),
    }
    with _LOCK:
        agents = [item for item in list_agents(settings) if item.get("agent_id") != agent_id]
        agents.append(agent)
        _write_list(_platform_dir(settings) / _AGENTS_FILE, agents)
    return agent


def update_agent(agent_id: str, payload: Dict[str, Any], settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    """Update editable Agent metadata; ``None`` when the agent is unknown.

    ``agent_id`` and ``created_at`` are immutable; ``updated_at`` is refreshed
    on every update. Historical runs are never touched — their ``run.json``
    ``agent_ref`` keeps the identity snapshot from execution time.
    """
    with _LOCK:
        agents = list_agents(settings)
        existing = next((item for item in agents if item.get("agent_id") == agent_id), None)
        if existing is None:
            return None
        merged = {**existing, **payload, "agent_id": agent_id}
        name = str(merged.get("name") or "").strip() or agent_id
        adapter = str(merged.get("adapter") or "http_session").strip()
        if adapter != "http_session":
            raise ValueError("Phase 0 supports adapter=http_session only")
        endpoint = str(merged.get("endpoint") or "").strip()
        if not endpoint:
            raise ValueError("endpoint is required")
        _validate_endpoint_credentials(endpoint)
        capabilities = merged.get("capabilities") or ["session"]
        if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
            raise ValueError("capabilities must be a list of strings")
        metadata = _validate_metadata(merged.get("metadata"))
        description = str(merged.get("description") or "")[:_MAX_DESCRIPTION]
        environment = _validate_environment(merged.get("environment"))
        tags = _validate_tags(merged.get("tags"))
        enabled = merged.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be a boolean")
        max_concurrency = _validate_max_concurrency(merged.get("max_concurrency"))

        agent = {
            **existing,
            "name": name,
            "version": str(merged.get("version") or ""),
            "adapter": adapter,
            "endpoint": endpoint,
            "capabilities": capabilities,
            "metadata": metadata,
            "max_concurrency": max_concurrency,
            "description": description,
            "environment": environment,
            "tags": tags,
            "enabled": enabled,
            "source_host": _source_host_for(endpoint),
            "created_at": existing.get("created_at"),  # None for legacy configs
            "updated_at": utc_now(),
            "config_hash": _config_hash(agent_id, endpoint, capabilities),
        }
        agents = [agent if item.get("agent_id") == agent_id else item for item in agents]
        _write_list(_platform_dir(settings) / _AGENTS_FILE, agents)
    return agent


def delete_agent(agent_id: str, settings: Optional[Settings] = None) -> bool:
    """Remove an Agent config; historical runs are always preserved.

    Runs that reference the deleted agent keep their ``run.json``
    ``agent_ref`` and simply show up as a derived/missing-configuration agent
    in the workspace afterwards.
    """
    with _LOCK:
        agents = list_agents(settings)
        remaining = [item for item in agents if item.get("agent_id") != agent_id]
        if len(remaining) == len(agents):
            return False
        _write_list(_platform_dir(settings) / _AGENTS_FILE, remaining)
    return True


def _validate_max_concurrency(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("max_concurrency must be a positive integer")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("max_concurrency must be a positive integer")
    if not 1 <= value <= 64:
        raise ValueError("max_concurrency must be between 1 and 64")
    return value


def _config_hash(agent_id: str, endpoint: str, capabilities: List[str]) -> str:
    text = json.dumps(
        {"agent_id": agent_id, "endpoint": endpoint, "capabilities": sorted(capabilities)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _looks_like_secret(value: str) -> bool:
    lowered = value.lower()
    return lowered.startswith("sk-") or "bearer " in lowered or "api_key" in lowered


# --------------------------------------------------------------- test cases


def list_test_cases(settings: Optional[Settings] = None) -> List[Dict[str, Any]]:
    """Unified store cases plus legacy ``cases/*.json`` cases (read-only)."""
    cases, _errors = list_test_cases_with_errors(settings)
    return cases


def list_test_cases_with_errors(
    settings: Optional[Settings] = None,
) -> "tuple[List[Dict[str, Any]], List[Dict[str, str]]]":
    """Return (valid cases, per-case errors); malformed entries are surfaced, not dropped."""
    cases: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for raw in _read_list(_platform_dir(settings) / _TEST_CASES_FILE):
        try:
            cases.append(normalize_test_case(raw))
        except ValueError as exc:
            errors.append({"case_id": str(raw.get("case_id") or raw.get("id") or "?"), "error": str(exc)})
    for case in _legacy_cases():
        if not any(item["case_id"] == case["case_id"] for item in cases):
            cases.append(case)
    return cases, errors


def get_test_case(case_id: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    for case in list_test_cases(settings):
        if case.get("case_id") == case_id:
            return case
    return None


def create_test_case(payload: Dict[str, Any], settings: Optional[Settings] = None) -> Dict[str, Any]:
    case = normalize_test_case(payload)
    with _LOCK:
        cases = [item for item in _read_list(_platform_dir(settings) / _TEST_CASES_FILE) if str(item.get("case_id") or item.get("id")) != case["case_id"]]
        cases.append(dict(payload, case_id=case["case_id"]))
        _write_list(_platform_dir(settings) / _TEST_CASES_FILE, cases)
    return case


def _legacy_cases() -> List[Dict[str, Any]]:
    cases_dir = PROJECT_ROOT / "cases"
    if not cases_dir.is_dir():
        return []
    normalized: List[Dict[str, Any]] = []
    for path in sorted(cases_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for raw in payload.get("cases") or []:
            try:
                normalized.append(normalize_test_case(raw))
            except ValueError:
                continue
    return normalized


# -------------------------------------------------------------- test suites


def list_test_suites(settings: Optional[Settings] = None) -> List[Dict[str, Any]]:
    return _read_list(_platform_dir(settings) / _TEST_SUITES_FILE)


def get_test_suite(suite_id: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    for suite in list_test_suites(settings):
        if str(suite.get("suite_id") or "") == suite_id:
            return suite
    return None


def create_test_suite(payload: Dict[str, Any], settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Validate and store a new TestSuite. Raises ``ValueError`` on problems."""
    suite = suite_mod.normalize_test_suite(payload)
    _validate_suite_case_refs(suite, settings)
    with _LOCK:
        suites = list_test_suites(settings)
        if any(item.get("suite_id") == suite["suite_id"] for item in suites):
            raise ValueError("test suite %s already exists" % suite["suite_id"])
        suites.append(suite)
        _write_list(_platform_dir(settings) / _TEST_SUITES_FILE, suites)
    return suite


def update_test_suite(
    suite_id: str, payload: Dict[str, Any], settings: Optional[Settings] = None
) -> Optional[Dict[str, Any]]:
    """Replace a TestSuite's editable fields; ``None`` when the suite is unknown.

    ``suite_id`` is immutable and ``version`` bumps on every update.
    """
    with _LOCK:
        suites = list_test_suites(settings)
        existing = next((item for item in suites if item.get("suite_id") == suite_id), None)
        if existing is None:
            return None
        suite = suite_mod.normalize_test_suite({**payload, "suite_id": suite_id}, existing=existing)
        _validate_suite_case_refs(suite, settings)
        suites = [suite if item.get("suite_id") == suite_id else item for item in suites]
        _write_list(_platform_dir(settings) / _TEST_SUITES_FILE, suites)
    return suite


def delete_test_suite(suite_id: str, settings: Optional[Settings] = None) -> bool:
    with _LOCK:
        suites = list_test_suites(settings)
        remaining = [item for item in suites if item.get("suite_id") != suite_id]
        if len(remaining) == len(suites):
            return False
        _write_list(_platform_dir(settings) / _TEST_SUITES_FILE, remaining)
    return True


def _validate_suite_case_refs(suite: Dict[str, Any], settings: Optional[Settings]) -> None:
    unknown = [case_id for case_id in suite["case_ids"] if get_test_case(case_id, settings) is None]
    if unknown:
        raise ValueError("test suite %s references unknown test cases: %s" % (suite["suite_id"], ", ".join(unknown)))


# --------------------------------------------------------------- suite jobs


def _suite_job_path(job_id: str, settings: Optional[Settings] = None) -> Path:
    if not job_id or "/" in job_id or "\\" in job_id or job_id in {".", ".."}:
        raise ValueError("invalid suite job id")
    return _platform_dir(settings) / _SUITE_JOBS_DIR / ("%s.json" % job_id)


def save_suite_job(record: Dict[str, Any], settings: Optional[Settings] = None) -> Dict[str, Any]:
    """Create or replace one SuiteJob record atomically."""
    job_id = str(record.get("job_id") or "")
    path = _suite_job_path(job_id, settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        suite_mod.write_suite_job(path, record)
    _record_suite_job_metric(record)
    return record


_SUITE_JOB_METRIC_RECORDED: set = set()


def _record_suite_job_metric(record: Dict[str, Any]) -> None:
    """Terminal SuiteJob metric (once per job; bounded status label only)."""
    try:
        status = str(record.get("status") or "")
        if not record.get("finished_at") or suite_mod.is_active_job_status(status):
            return
        job_id = str(record.get("job_id") or "")
        if not job_id or job_id in _SUITE_JOB_METRIC_RECORDED:
            return
        _SUITE_JOB_METRIC_RECORDED.add(job_id)
        duration = None
        started, finished = record.get("created_at"), record.get("finished_at")
        if started and finished:
            from datetime import datetime

            try:
                duration = max(
                    (
                        datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
                        - datetime.fromisoformat(str(started).replace("Z", "+00:00"))
                    ).total_seconds(),
                    0.0,
                )
            except ValueError:
                duration = None
        from backend.app import metrics

        metrics.record_suite_job(status, duration)
    except Exception:
        pass  # metrics must never fail a suite job write


def get_suite_job(job_id: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    try:
        path = _suite_job_path(job_id, settings)
    except ValueError:
        return None
    return suite_mod.read_suite_job(path)


def list_suite_jobs(settings: Optional[Settings] = None) -> List[Dict[str, Any]]:
    jobs_dir = _platform_dir(settings) / _SUITE_JOBS_DIR
    if not jobs_dir.is_dir():
        return []
    jobs = []
    for path in sorted(jobs_dir.glob("*.json")):
        record = suite_mod.read_suite_job(path)
        if record is not None:
            jobs.append(record)
    jobs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return jobs


def request_suite_job_cancel(job_id: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    """Set ``cancel_requested`` on a queued/running job; ``None`` when unknown."""
    with _LOCK:
        record = get_suite_job(job_id, settings)
        if record is None:
            return None
        if suite_mod.is_active_job_status(record.get("status")):
            record["cancel_requested"] = True
            save_suite_job(record, settings)
    return record


# -------------------------------------------------------------- idempotency

def lookup_idempotency(key: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _read_idempotency(_IDEMPOTENCY_FILE, settings)
        entry = data.get(key)
        return dict(entry) if isinstance(entry, dict) else None


def store_idempotency(key: str, fingerprint: str, run_id: str, settings: Optional[Settings] = None) -> None:
    with _LOCK:
        data = _read_idempotency(_IDEMPOTENCY_FILE, settings)
        data[key] = {"fingerprint": fingerprint, "run_id": run_id}
        _write_idempotency(_IDEMPOTENCY_FILE, data, settings)


def run_fingerprint(agent_id: str, test_case_id: str, judge: bool) -> str:
    text = json.dumps(
        {"agent_id": agent_id, "test_case_id": test_case_id, "judge": bool(judge)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------- suite job idempotency

_SUITE_IDEMPOTENCY_FILE = "suite_idempotency.json"


def lookup_suite_idempotency(key: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _read_idempotency(_SUITE_IDEMPOTENCY_FILE, settings)
        entry = data.get(key)
        return dict(entry) if isinstance(entry, dict) else None


def store_suite_idempotency(key: str, fingerprint: str, job_id: str, settings: Optional[Settings] = None) -> None:
    with _LOCK:
        data = _read_idempotency(_SUITE_IDEMPOTENCY_FILE, settings)
        data[key] = {"fingerprint": fingerprint, "job_id": job_id}
        _write_idempotency(_SUITE_IDEMPOTENCY_FILE, data, settings)


def suite_fingerprint(
    suite_id: str,
    suite_version: Any,
    agent_id: str,
    judge: bool,
    options: Dict[str, Any],
) -> str:
    """Fingerprint of one SuiteJob launch request for Idempotency-Key replay."""
    text = json.dumps(
        {
            "suite_id": suite_id,
            "suite_version": suite_version,
            "agent_id": agent_id,
            "judge": bool(judge),
            "options": options,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_idempotency(filename: str, settings: Optional[Settings]) -> Dict[str, Any]:
    path = _platform_dir(settings) / filename
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_idempotency(filename: str, data: Dict[str, Any], settings: Optional[Settings]) -> None:
    write_json(_platform_dir(settings) / filename, data)
