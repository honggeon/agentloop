"""Phase 0 platform config store: Agents and TestCases as JSON files.

No database, no queue: ``platform/agents.json`` and ``platform/test_cases.json``
hold the Agent and unified TestCase configs; ``platform/idempotency.json``
holds POST /api/runs idempotency keys for this single deployment. All writes
are atomic via ``auditrail.trajectory.write_json``. Credentials never enter
these files — Agent configs reference endpoints only.

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

from auditrail.testcases import normalize_test_case
from auditrail.trajectory import write_json

from backend.app.config import PROJECT_ROOT, Settings, get_settings

_LOCK = threading.Lock()

_AGENTS_FILE = "agents.json"
_TEST_CASES_FILE = "test_cases.json"
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


def list_agents(settings: Optional[Settings] = None) -> List[Dict[str, Any]]:
    return _read_list(_platform_dir(settings) / _AGENTS_FILE)


def get_agent(agent_id: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    for agent in list_agents(settings):
        if str(agent.get("agent_id") or "") == agent_id:
            return agent
    return None


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
    capabilities = payload.get("capabilities") or ["session"]
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise ValueError("capabilities must be a list of strings")
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")
    for value in metadata.values():
        if isinstance(value, str) and _looks_like_secret(value):
            raise ValueError("credentials must not be stored in agent metadata; use environment variables")

    agent = {
        "agent_id": agent_id,
        "name": name,
        "version": str(payload.get("version") or ""),
        "adapter": adapter,
        "endpoint": endpoint,
        "capabilities": capabilities,
        "metadata": metadata,
        "config_hash": _config_hash(agent_id, endpoint, capabilities),
    }
    with _LOCK:
        agents = [item for item in list_agents(settings) if item.get("agent_id") != agent_id]
        agents.append(agent)
        _write_list(_platform_dir(settings) / _AGENTS_FILE, agents)
    return agent


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


# -------------------------------------------------------------- idempotency


def lookup_idempotency(key: str, settings: Optional[Settings] = None) -> Optional[Dict[str, Any]]:
    with _LOCK:
        data = _read_idempotency(settings)
        entry = data.get(key)
        return dict(entry) if isinstance(entry, dict) else None


def store_idempotency(key: str, fingerprint: str, run_id: str, settings: Optional[Settings] = None) -> None:
    with _LOCK:
        data = _read_idempotency(settings)
        data[key] = {"fingerprint": fingerprint, "run_id": run_id}
        _write_idempotency(data, settings)


def run_fingerprint(agent_id: str, test_case_id: str, judge: bool) -> str:
    text = json.dumps(
        {"agent_id": agent_id, "test_case_id": test_case_id, "judge": bool(judge)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_idempotency(settings: Optional[Settings]) -> Dict[str, Any]:
    path = _platform_dir(settings) / _IDEMPOTENCY_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_idempotency(data: Dict[str, Any], settings: Optional[Settings]) -> None:
    write_json(_platform_dir(settings) / _IDEMPOTENCY_FILE, data)
