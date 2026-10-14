"""DeepAgent judge engine adapter.

This wraps the existing DeepAgent invocation without changing behavior: the
same prompt builder, the same thread-id shape, and the same
``resolve_judge_report`` normalization performed by ``write_judge_report``.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any, Mapping, Optional

from ..runtime import build_agent_judge_prompt
from .base import (
    DEEPAGENT_ENGINE,
    JudgeExecutionError,
    JudgeRequest,
    JudgeResult,
    JudgeUnavailableError,
)

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
        messages = [{"role": "user", "content": build_agent_judge_prompt(run_dir)}]
        run_id = request.run_id or run_dir.name
        config = {"configurable": {"thread_id": "%s-%s" % (self._thread_id_prefix, run_id)}}
        try:
            raw_report = asyncio.run(self._invoke_agent(messages, config=config))
        except RuntimeError as exc:
            raise JudgeUnavailableError("deepagent_unavailable", str(exc)) from exc
        except Exception as exc:
            raise JudgeExecutionError(
                "deepagent_failed",
                "%s: %s" % (type(exc).__name__, exc),
            ) from exc
        if not isinstance(raw_report, Mapping):
            raise JudgeExecutionError(
                "invalid_proposal",
                "DeepAgent judge returned %s instead of a report object" % type(raw_report).__name__,
            )
        return JudgeResult(
            engine=self.id,
            proposal=dict(raw_report),
            metadata=self._metadata(),
        )

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
