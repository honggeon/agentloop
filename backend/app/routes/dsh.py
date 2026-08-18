"""DSH (DeepSeek Harness) session routes.

Lists locally recorded DSH sessions and imports them as run directories.
All conversion logic lives in ``agentloop.dsh_source``; this module only does
HTTP assembly, input validation and error mapping.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from agentloop.cli import _run_dir, _write_run_outputs
from agentloop.dsh_source import (
    find_session_file,
    list_sessions,
    load_session_response,
    source_meta,
)
from backend.app.config import get_settings


router = APIRouter(prefix="/api/dsh")


@router.get("/sessions")
def get_dsh_sessions(limit: int = 50) -> dict[str, Any]:
    settings = get_settings()
    sessions = list_sessions(settings.dsh_home, limit=limit)
    return {"dsh_home": str(settings.dsh_home), "sessions": sessions}


@router.post("/import")
def import_dsh_session(payload: dict[str, Any]) -> dict[str, Any]:
    session_id = str(payload.get("session_id") or "").strip()
    workspace = str(payload.get("workspace") or "").strip() or None
    if not session_id:
        raise HTTPException(status_code=400, detail="missing session_id")

    settings = get_settings()
    try:
        session_path = find_session_file(settings.dsh_home, session_id, workspace)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        session_response = load_session_response(session_path)
        run_dir = _run_dir(settings.runs_dir, "dsh-%s" % session_id.removeprefix("session-"))
        _write_run_outputs(run_dir, session_response, source=source_meta(session_response))
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "run_id": run_dir.name,
        "session_id": session_id,
        "source_type": "dsh",
    }
