from __future__ import annotations

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
#: env 兜底默认模型。实际生效模型在 ``get_agent()`` 运行时解析（配置文件
#: profile / JUDGE_MODEL_PROFILE / 本默认值，见 agents/model_config.py）。
MODEL_SPEC = f"{MODEL_PROVIDER}:{MODEL_NAME}"


async def get_agent(model_profile: str | None = None) -> Any:
    """Build a fresh DeepAgent graph bound to the current event loop.

    The compiled graph and its async model client bind to the running loop.
    Callers (CLI judge-run, backend judge route) wrap each invocation in
    ``asyncio.run``, which closes the loop on exit; a module-level cached
    graph would then be reused on a closed loop and every judge after the
    first failed with "Event loop is closed". Construction is cheap compared
    with the model calls, so each call builds its own graph.

    ``model_profile`` 指定 config/judge_models.yaml 中的 profile 名；
    为空时按 JUDGE_MODEL_PROFILE → 配置文件 default → env 兜底解析。
    """
    _sanitize_no_proxy_env()
    create_deep_agent, init_chat_model, memory_saver = _load_deepagent_runtime()
    model = _build_chat_model(init_chat_model, model_profile)
    return create_deep_agent(
        model=model,
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
        subagents=build_subagents(model),
        checkpointer=memory_saver(),
        name="Auditrail Analyst",
    )


async def graph() -> Any:
    return await get_agent()


async def invoke_agent(messages: list[dict], config: dict | None = None, model_profile: str | None = None) -> dict:
    agent = await get_agent(model_profile)
    return await agent.ainvoke({"messages": messages}, config=config or {})


async def stream_agent(messages: list[dict], config: dict | None = None, model_profile: str | None = None) -> AsyncIterator[dict]:
    agent = await get_agent(model_profile)
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


def _sanitize_no_proxy_env() -> None:
    """Unbracket bracketed IPv6 entries in NO_PROXY/no_proxy.

    Some shells export ``NO_PROXY=127.0.0.1,localhost,::1,[::1]``. The httpx2
    client used by the OpenAI SDK (and therefore by langchain-deepseek) builds
    proxy-mount patterns from NO_PROXY and misparses an already-bracketed
    ``[::1]`` entry, failing model initialisation with
    ``InvalidURL: Invalid port: ':1]'``. The unbracketed form ``::1`` is
    handled correctly, so rewrite bracketed entries in place.
    """
    for name in ("no_proxy", "NO_PROXY"):
        value = os.environ.get(name)
        if not value or "[" not in value:
            continue
        entries = [
            entry[1:-1] if entry.startswith("[") and entry.endswith("]") else entry
            for entry in (part.strip() for part in value.split(","))
        ]
        os.environ[name] = ",".join(entries)


def _build_chat_model(init_chat_model: Any, model_profile: str | None = None) -> Any:
    """按 profile 配置构建 chat model；解析或初始化失败给出明确错误。"""
    from .model_config import chat_model_kwargs, resolve_judge_model

    spec = resolve_judge_model(model_profile)
    try:
        return init_chat_model(spec.chat_model_spec, **chat_model_kwargs(spec))
    except Exception as exc:
        raise RuntimeError(
            "judge model %r (profile %r) could not be initialised. Cause: %s: %s"
            % (spec.chat_model_spec, spec.name or "env", type(exc).__name__, exc)
        ) from exc

