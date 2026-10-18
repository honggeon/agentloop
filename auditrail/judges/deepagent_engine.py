"""DeepAgent judge engine adapter.

This wraps the existing DeepAgent invocation without changing behavior: the
same prompt builder, the same thread-id shape, and the same
``resolve_judge_report`` normalization performed by ``write_judge_report``.

Structural completeness is enforced at this boundary: the returned proposal is
the most complete report candidate found in the agent response, and a proposal
missing dimensions gets exactly one bounded repair attempt (same run evidence,
same schema) before the run fails with a diagnosable error.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from ..agents.schemas import extract_json_object, missing_judge_dimensions
from ..runtime import build_agent_judge_prompt
from .base import (
    DEEPAGENT_ENGINE,
    JudgeExecutionError,
    JudgeRequest,
    JudgeResult,
    JudgeUnavailableError,
)
from .prompting import build_dimension_repair_note

_THREAD_ID_PREFIX = "auditrail-judge"

# The DeepAgent judge uses model credentials; record a non-secret label only.
_MODEL_PROVIDER_ENV = "MODEL_PROVIDER"
_MODEL_NAME_ENV = "MODEL_NAME"


class DeepAgentJudgeEngine:
    """Run the existing local DeepAgent analyst graph as a judge engine."""

    id = DEEPAGENT_ENGINE
    label = "DeepAgent 裁判"

    def __init__(self, invoke: Any = None, *, thread_id_prefix: str = _THREAD_ID_PREFIX):
        self._invoke = invoke
        self._thread_id_prefix = thread_id_prefix

    def available(self) -> Optional[str]:
        # Preserved behavior: availability is decided when the graph loads, so
        # this matches the previous route's 503 without importing the optional
        # dependency here.
        return None

    def run(self, request: JudgeRequest) -> JudgeResult:
        run_dir = Path(request.run_dir)
        prompt = build_agent_judge_prompt(run_dir)
        run_id = request.run_id or run_dir.name
        thread_id = request.thread_id or "%s-%s" % (self._thread_id_prefix, run_id)
        config = {"configurable": {"thread_id": thread_id}}

        raw_report = self._invoke_or_raise([{"role": "user", "content": prompt}], config)
        if not isinstance(raw_report, Mapping):
            raise JudgeExecutionError(
                "invalid_proposal",
                "DeepAgent judge returned %s instead of a report object" % type(raw_report).__name__,
            )
        proposal = self._best_candidate(raw_report)
        missing = missing_judge_dimensions(proposal) if proposal is not None else []
        if proposal is not None and missing:
            # One bounded repair attempt on the same run evidence and schema;
            # the note names the omitted dimension keys explicitly.
            repair_prompt = prompt + build_dimension_repair_note(missing)
            repaired_raw = self._invoke_or_raise([{"role": "user", "content": repair_prompt}], config)
            repaired = self._best_candidate(repaired_raw) if isinstance(repaired_raw, Mapping) else None
            remaining = missing_judge_dimensions(repaired) if repaired is not None else list(missing)
            if repaired is None or remaining:
                raise JudgeExecutionError(
                    "incomplete_proposal",
                    "judge proposal is still incomplete after one repair attempt; "
                    "missing dimensions: %s" % ", ".join(remaining),
                )
            proposal = repaired
        return JudgeResult(
            engine=self.id,
            # No report-shaped JSON at all keeps the legacy pass-through: the
            # caller's resolve_judge_report applies its disk fallback or fails.
            proposal=proposal if proposal is not None else dict(raw_report),
            metadata=self._metadata(),
        )

    @staticmethod
    def _best_candidate(raw_report: Mapping) -> Optional[dict[str, Any]]:
        """Extract the most complete report candidate, or None if none exists."""
        try:
            return extract_json_object(raw_report)
        except ValueError:
            return None

    def _invoke_or_raise(self, messages: list[dict], config: dict) -> Any:
        try:
            return asyncio.run(self._invoke_agent(messages, config=config))
        except RuntimeError as exc:
            raise JudgeUnavailableError("deepagent_unavailable", str(exc)) from exc
        except Exception as exc:
            raise JudgeExecutionError(
                "deepagent_failed",
                "%s: %s" % (type(exc).__name__, exc),
            ) from exc

    def _invoke_agent(self, messages: list[dict], config: dict) -> Any:
        if self._invoke is not None:
            return self._invoke(messages, config=config)
        from ..agents.agent import invoke_agent

        return invoke_agent(messages, config=config)

    def _metadata(self) -> dict[str, Any]:
        return {
            "runtime": "deepagent",
            "provider": os.getenv(_MODEL_PROVIDER_ENV, ""),
            "model": os.getenv(_MODEL_NAME_ENV, ""),
        }
