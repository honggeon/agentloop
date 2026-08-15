from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from agentloop.agents.schemas import resolve_judge_report
from agentloop.agents.tools import write_judge_report
from agentloop.runtime import build_agent_judge_prompt
from backend.app.config import get_settings, resolve_run_path


router = APIRouter(prefix="/api")


@router.post("/runs/{run_id}/judge")
def run_judge(run_id: str) -> dict[str, Any]:
    path = _run_path_or_404(run_id)
    from agentloop.agents.agent import invoke_agent

    messages = [
        {
            "role": "user",
            "content": build_agent_judge_prompt(path),
        }
    ]
    config = {"configurable": {"thread_id": "agentloop-judge-%s" % run_id}}
    try:
        raw_report = asyncio.run(invoke_agent(messages, config=config))
        report = resolve_judge_report(raw_report, path)
        paths = write_judge_report(str(path), report)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail="%s: %s" % (type(exc).__name__, exc)) from exc
    return {"status": "ok", "paths": paths, "report": report}


def _run_path_or_404(run_id: str):
    try:
        return resolve_run_path(run_id, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
