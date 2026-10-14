"""DSH (DeepSeek Harness) session routes.

Lists locally recorded DSH sessions and imports them as run directories.
All conversion logic lives in ``auditrail.dsh_source``; this module only does
HTTP assembly, input validation and error mapping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from auditrail.cli import _run_dir, _write_run_outputs
from auditrail.dsh_source import (
    find_session_file,
    list_sessions,
    load_session_response,
    source_meta,
)
from backend.app.config import build_dsh_runtime_config, get_settings


router = APIRouter(prefix="/api/dsh")

MAX_WORKSPACE_CHOICES_PER_ROOT = 200


@router.get("/options")
def get_dsh_options() -> dict[str, Any]:
    """Expose the backend-owned provider manifest for the local workbench.

    This is read-only discovery: it reports whether local DSH execution is
    enabled and which profiles/workspaces the trusted policy admits, so the
    browser can offer approved choices instead of free-form input. The DSH
    binary path and any other launcher detail are never returned.
    """
    settings = get_settings()
    enabled = False
    disabled_reason: str | None = "local DSH runtime is disabled"
    profiles: tuple[str, ...] = ()
    roots: tuple[Path, ...] = ()
    if settings.dsh_runtime_enabled:
        try:
            config = build_dsh_runtime_config(settings)
        except ValueError as exc:
            disabled_reason = str(exc)
        else:
            enabled = True
            disabled_reason = None
            profiles = config.allowed_profiles
            roots = config.allowed_workspace_roots

    return {
        "providers": [
            {"id": "remote", "enabled": True, "disabled_reason": None},
            {"id": "dsh", "enabled": enabled, "disabled_reason": disabled_reason},
        ],
        "dsh": {
            "enabled": enabled,
            "disabled_reason": disabled_reason,
            "profiles": list(profiles),
            "workspace_roots": [str(root) for root in roots],
            "workspaces": _list_workspaces(roots),
            "default_timeout_seconds": settings.dsh_default_timeout_seconds,
            "max_timeout_seconds": settings.dsh_max_timeout_seconds,
            "max_concurrency": settings.dsh_max_concurrency,
        },
    }


def _list_workspaces(roots: tuple[Path, ...]) -> list[str]:
    """List approved candidate workspaces strictly beneath the allowed roots."""
    workspaces: list[str] = []
    seen: set[str] = set()
    for root in roots:
        try:
            children = sorted((child for child in root.iterdir() if child.is_dir()), key=lambda item: item.name)
        except OSError:
            continue
        for child in children[:MAX_WORKSPACE_CHOICES_PER_ROOT]:
            if child.name.startswith("."):
                continue
            try:
                resolved = child.resolve(strict=False)
            except OSError:
                continue
            if resolved == root or root not in resolved.parents:
                continue
            value = str(resolved)
            if value in seen:
                continue
            seen.add(value)
            workspaces.append(value)
    return workspaces


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
