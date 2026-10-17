"""Unified TestCase model with backward-compatible legacy mapping.

This is a normalization layer, not a second schema: the legacy
``auditrail.cases`` format (``binary``/``args``/``files``/``env``/``expect``/
``session``/``xfail``/``defect``) keeps its exact execution semantics in
``auditrail.cases``; :func:`normalize_test_case` only exposes both formats
through one shape:

```text
TestCase
├── case_id / title / version
├── adapter: command | http_session | openai_compatible
├── input
├── fixtures
├── expectations
├── timeout_seconds
└── adapter_config
```

Legacy cases map to ``adapter="command"`` with their argv/env/files/session
preserved under ``adapter_config``; HTTP agents use ``adapter="http_session"``
with ``input`` carrying ``user_id`` plus ``message`` or ``session_id``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

ADAPTERS = ("command", "http_session", "openai_compatible")

#: Expectation types the Phase 0 deterministic evaluator can compute for an
#: ``http_session`` run from ``audit.json``/``summary.json``. Unknown types
#: evaluate to ``unknown`` — never silently dropped, never fabricated.
HTTP_EXPECTATION_TYPES = (
    "final_answer_present",
    "no_tool_errors",
    "tool_error_is_observed",
    "no_missing_tool_results",
    "no_orphan_tool_results",
    "no_dangerous_tools",
    "artifact_exists",
)

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 3600


def is_legacy_case(raw: Any) -> bool:
    """A legacy ``cases.py`` case declares argv (``args``) instead of ``adapter``."""
    return isinstance(raw, dict) and "adapter" not in raw and isinstance(raw.get("args"), list)


def normalize_test_case(raw: Any) -> Dict[str, Any]:
    """Normalize a unified or legacy TestCase mapping. Raises ``ValueError``."""
    if not isinstance(raw, dict):
        raise ValueError("test case must be an object")
    if is_legacy_case(raw):
        return _normalize_legacy(raw)
    return _normalize_unified(raw)


def _normalize_unified(raw: Dict[str, Any]) -> Dict[str, Any]:
    case_id = str(raw.get("case_id") or raw.get("id") or "").strip()
    if not case_id:
        raise ValueError("test case is missing case_id")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("test case %s is missing title" % case_id)
    adapter = str(raw.get("adapter") or "").strip()
    if adapter not in ADAPTERS:
        raise ValueError("test case %s has unsupported adapter %r" % (case_id, adapter))
    version = _positive_int(raw.get("version", 1), "version", case_id)
    timeout = _timeout(raw.get("timeout_seconds"), case_id)

    input_spec = raw.get("input")
    if not isinstance(input_spec, dict):
        raise ValueError("test case %s needs an input object" % case_id)
    input_spec = dict(input_spec)

    expectations = raw.get("expectations") or []
    if not isinstance(expectations, list) or not all(isinstance(item, dict) for item in expectations):
        raise ValueError("test case %s expectations must be a list of objects" % case_id)
    expectations = [_normalize_expectation(item, case_id) for item in expectations]

    fixtures = raw.get("fixtures") or []
    if isinstance(fixtures, dict):
        fixtures = [{"path": key, "content": value} for key, value in fixtures.items()]
    if not isinstance(fixtures, list):
        raise ValueError("test case %s fixtures must be a list" % case_id)

    case = {
        "case_id": case_id,
        "version": version,
        "title": title,
        "adapter": adapter,
        "input": input_spec,
        "fixtures": fixtures,
        "expectations": expectations,
        "timeout_seconds": timeout,
        "adapter_config": dict(raw.get("adapter_config") or {}),
        "legacy": False,
    }
    # Phase 4: provenance of promoted external drafts (e.g. promptfoo). Purely
    # additive; cases without a source_ref read as None.
    source_ref = raw.get("source_ref")
    case["source_ref"] = dict(source_ref) if isinstance(source_ref, dict) else None
    if adapter == "http_session":
        _validate_http_input(case)
    return case


def _normalize_legacy(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map a legacy ``cases.py`` case to the unified view, semantics preserved."""
    case_id = str(raw.get("id") or "").strip()
    if not case_id:
        raise ValueError("legacy case is missing id")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise ValueError("legacy case %s is missing title" % case_id)
    return {
        "case_id": case_id,
        "version": _positive_int(raw.get("version", 1), "version", case_id),
        "title": title,
        "adapter": "command",
        "input": {
            "binary": str(raw.get("binary") or ""),
            "args": list(raw.get("args") or []),
            "env": dict(raw.get("env") or {}),
        },
        "fixtures": [
            {"path": key, "content": value} for key, value in (raw.get("files") or {}).items()
        ],
        "expectations": [
            {
                "id": "legacy-expect",
                "type": "legacy_assertions",
                "required": True,
                "assertions": dict(raw.get("expect") or {}),
            }
        ],
        "timeout_seconds": _timeout(raw.get("timeout_s"), case_id) if raw.get("timeout_s") else DEFAULT_TIMEOUT_SECONDS,
        "adapter_config": {
            "module": str(raw.get("module") or ""),
            "priority": str(raw.get("priority") or ""),
            "session": dict(raw.get("session") or {}) or None,
            "notes": str(raw.get("notes") or ""),
        },
        "xfail": bool(raw.get("xfail")),
        "defect": str(raw.get("defect") or ""),
        "legacy": True,
    }


def _normalize_expectation(item: Dict[str, Any], case_id: str) -> Dict[str, Any]:
    expectation = dict(item)
    expectation_id = str(expectation.get("id") or "").strip()
    if not expectation_id:
        raise ValueError("test case %s has an expectation without id" % case_id)
    expectation["id"] = expectation_id
    expectation["type"] = str(expectation.get("type") or "").strip()
    if not expectation["type"]:
        raise ValueError("test case %s expectation %s is missing type" % (case_id, expectation_id))
    expectation["required"] = bool(expectation.get("required", True))
    return expectation


def _validate_http_input(case: Dict[str, Any]) -> None:
    input_spec = case["input"]
    user_id = str(input_spec.get("user_id") or "").strip()
    message = str(input_spec.get("message") or "").strip()
    session_id = str(input_spec.get("session_id") or "").strip()
    if not user_id:
        raise ValueError("test case %s http_session input needs user_id" % case["case_id"])
    if not message and not session_id:
        raise ValueError("test case %s http_session input needs message or session_id" % case["case_id"])
    if session_id and ("/" in session_id or "\\" in session_id):
        raise ValueError("test case %s session_id must not contain path separators" % case["case_id"])
    input_spec["user_id"] = user_id
    if message:
        input_spec["message"] = message
    if session_id:
        input_spec["session_id"] = session_id


def _positive_int(value: Any, field: str, case_id: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("test case %s %s must be a positive integer" % (case_id, field))
    if number <= 0:
        raise ValueError("test case %s %s must be a positive integer" % (case_id, field))
    return number


def _timeout(value: Any, case_id: str) -> int:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    timeout = _positive_int(value, "timeout_seconds", case_id)
    return min(timeout, MAX_TIMEOUT_SECONDS)


def validate_case_relpath(relpath: Any) -> str:
    """Validate one run-relative path for fixtures, deliverables and expectations.

    Rejects absolute paths, ``..`` segments, NUL bytes, Windows-style
    backslash separators and empty values. Resolution-escape and symlink
    checks need a concrete root; use :func:`resolve_within` for those.
    """
    text = str(relpath or "")
    if not text:
        raise ValueError("path must be a non-empty relative path")
    if "\x00" in text:
        raise ValueError("path must not contain NUL bytes: %r" % text)
    if "\\" in text:
        raise ValueError("path must use forward slashes only: %r" % text)
    path = Path(text)
    if path.is_absolute():
        raise ValueError("path must be relative: %r" % text)
    if ".." in path.parts:
        raise ValueError("path must be relative and stay inside the workspace (no '..'): %r" % text)
    if text.startswith("/") or text.endswith("/"):
        raise ValueError("path must be a plain relative file path: %r" % text)
    return text


def resolve_within(root: Path, relpath: str) -> Path:
    """Resolve ``relpath`` inside ``root``, rejecting escapes and symlink hops."""
    root = Path(root).resolve()
    target = (root / validate_case_relpath(relpath)).resolve()
    if target != root and root not in target.parents:
        raise ValueError("path escapes the workspace: %r" % relpath)
    # Reject symlink components that exist already (a resolved escape above
    # only catches symlinks that resolve outside; a symlink pointing inside is
    # still rejected to keep fixture semantics predictable).
    current = root
    for part in Path(relpath).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("path must not traverse symlinks: %r" % relpath)
    return target
