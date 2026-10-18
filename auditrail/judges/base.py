"""Judge engine abstraction shared by the DeepAgent and DSH judges.

This module is deterministic and credential-free: it only defines the engine
boundary, the engine registry, and structured failures. Concrete engines live
in ``deepagent_engine`` and ``dsh_engine``; both return an untrusted report
proposal that is normalized and capped by the existing judge pipeline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol

DEFAULT_JUDGE_ENGINE = "deepagent"
DEEPAGENT_ENGINE = "deepagent"
DSH_ENGINE = "dsh"
_ENGINE_IDS = (DEEPAGENT_ENGINE, DSH_ENGINE)


class JudgeEngineError(RuntimeError):
    """Base failure raised by the judge engine boundary."""


class JudgeUnavailableError(JudgeEngineError):
    """The selected engine cannot run in this deployment (maps to HTTP 503)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class JudgeExecutionError(JudgeEngineError):
    """The selected engine ran but did not produce a usable proposal (HTTP 500)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class JudgeRequest:
    """One judge attempt for an already-selected run directory.

    ``run_dir`` must come from ``backend.app.config.resolve_run_path`` (or an
    equivalent validation) before it reaches an engine. ``thread_id`` is an
    optional caller-chosen conversation id (CLI ``--thread-id``); engines fall
    back to their own default when it is empty.
    """

    run_dir: Path
    run_id: str = ""
    thread_id: str = ""


@dataclass(frozen=True)
class JudgeResult:
    """A raw, untrusted judge proposal plus engine provenance."""

    engine: str
    proposal: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)


class JudgeEngine(Protocol):
    """One selectable judge backend."""

    id: str
    label: str

    def available(self) -> Optional[str]:
        """Return ``None`` when ready, else a safe human-readable reason."""

    def run(self, request: JudgeRequest) -> JudgeResult:
        """Execute one judgment and return an untrusted proposal."""


# Engine overrides exist for tests and operator debugging only. They are never
# read from an HTTP request, so a browser cannot select an arbitrary engine.
_ENGINE_ENV_VAR = "AUDITRAIL_JUDGE_ENGINE"


def default_judge_engine_id() -> str:
    """Operator-selected default engine.

    The override is server-side only (an environment variable), and an unknown
    value falls back to the built-in default instead of breaking the UI.
    """
    value = str(os.getenv(_ENGINE_ENV_VAR) or "").strip().lower()
    return value if value in _ENGINE_IDS else DEFAULT_JUDGE_ENGINE


def list_judge_engines(settings: Any = None) -> list[dict[str, Any]]:
    """Describe every registered engine and its availability for the UI."""
    items: list[dict[str, Any]] = []
    for engine in _engines(settings):
        reason = engine.available()
        items.append(
            {
                "id": engine.id,
                "label": engine.label,
                "available": reason is None,
                "disabled_reason": reason,
            }
        )
    return items


def judge_engine_ids(settings: Any = None) -> list[str]:
    return [engine.id for engine in _engines(settings)]


def resolve_judge_engine(engine_id: Optional[str], settings: Any = None) -> JudgeEngine:
    """Resolve an engine id, failing explicitly on unknown ids."""
    value = str(engine_id or "").strip().lower() or default_judge_engine_id()
    for engine in _engines(settings):
        if engine.id == value:
            return engine
    raise JudgeUnavailableError(
        "unknown_judge_engine",
        "judge engine %r is not registered" % value,
    )


def describe_engines(engine_id: Optional[str], settings: Any = None) -> dict[str, Any]:
    """Resolve one engine and report it together with the full catalog."""
    selected = resolve_judge_engine(engine_id, settings)
    return {
        "default_engine": default_judge_engine_id(),
        "engines": list_judge_engines(settings),
        "selected": selected.id,
    }


def _engines(settings: Any = None) -> Iterable[JudgeEngine]:
    # Imported lazily so the deterministic agents layer never pulls the DSH
    # adapter or the optional DeepAgent dependencies unless a judge is selected.
    from .deepagent_engine import DeepAgentJudgeEngine
    from .dsh_engine import DshJudgeEngine

    return (DeepAgentJudgeEngine(), DshJudgeEngine(settings))
