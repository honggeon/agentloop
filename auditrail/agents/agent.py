from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator

from .prompts import MAIN_SYSTEM_PROMPT
from .subagents import build_subagents
from .tools import (
    build_chart_data,
    get_tool_calls,
    list_runs,
    load_audit,
    load_run_summary,
    load_trajectory_slice,
    search_trajectory,
)


logger = logging.getLogger(__name__)

MODEL_PROVIDER = os.getenv("MODEL_PROVIDER", "openai")
MODEL_NAME = os.getenv("MODEL_NAME", "gpt-4.1")
MODEL_SPEC = f"{MODEL_PROVIDER}:{MODEL_NAME}"

_agent_graph: Any = None
_agent_lock = asyncio.Lock()


async def get_agent() -> Any:
    global _agent_graph
    if _agent_graph is not None:
        return _agent_graph
    async with _agent_lock:
        if _agent_graph is not None:
            return _agent_graph
        create_deep_agent, init_chat_model, memory_saver = _load_deepagent_runtime()
        _validate_model_spec(init_chat_model, MODEL_SPEC)
        _agent_graph = create_deep_agent(
            model=MODEL_SPEC,
            tools=[
                list_runs,
                load_run_summary,
                load_audit,
                load_trajectory_slice,
                search_trajectory,
                get_tool_calls,
                build_chart_data,
            ],
            system_prompt=MAIN_SYSTEM_PROMPT,
            subagents=build_subagents(MODEL_SPEC),
            checkpointer=memory_saver(),
            name="Auditrail Analyst",
        )
        return _agent_graph


async def graph() -> Any:
    return await get_agent()


async def invoke_agent(messages: list[dict], config: dict | None = None) -> dict:
    agent = await get_agent()
    return await agent.ainvoke({"messages": messages}, config=config or {})


async def stream_agent(messages: list[dict], config: dict | None = None) -> AsyncIterator[dict]:
    agent = await get_agent()
    async for chunk in agent.astream({"messages": messages}, config=config or {}):
        yield chunk


def _load_deepagent_runtime() -> tuple[Any, Any, Any]:
    try:
        from deepagents import create_deep_agent
        from langchain.chat_models import init_chat_model
        from langgraph.checkpoint.memory import MemorySaver
    except ImportError as exc:
        raise RuntimeError(
            "DeepAgent analysis requires installing the optional agent dependencies: "
            "pip install -e '.[agent]'"
        ) from exc
    return create_deep_agent, init_chat_model, MemorySaver


def _validate_model_spec(init_chat_model: Any, model_spec: str) -> None:
    try:
        init_chat_model(model_spec)
    except Exception as exc:
        raise RuntimeError(
            "MODEL_SPEC %r could not be initialised. Cause: %s: %s"
            % (model_spec, type(exc).__name__, exc)
        ) from exc

