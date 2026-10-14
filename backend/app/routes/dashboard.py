from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse, urlunparse

from fastapi import APIRouter, HTTPException

from backend.app.config import get_settings


router = APIRouter(prefix="/api")


@router.get("/dashboard")
def get_dashboard() -> dict[str, Any]:
    """Return the dashboard URL the frontend dev server registered, or an env-based hint."""
    return {"url": _read_registered_url() or _env_hint_url()}


@router.post("/dashboard/register")
def register_dashboard(payload: dict[str, Any]) -> dict[str, Any]:
    """Let the frontend dev server report its actual listen URL (called by vite.config)."""
    url = _normalize_url(payload.get("url"))
    if not url:
        raise HTTPException(status_code=400, detail="url must be an http(s) URL with a host and port")
    _write_registered_url(url)
    return {"url": url}


def _registry_path():
    return get_settings().runs_dir / ".dashboard.json"


def _read_registered_url() -> str:
    path = _registry_path()
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("url") or "")


def _write_registered_url(url: str) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"url": url}, ensure_ascii=False), encoding="utf-8")


def _env_hint_url() -> str:
    settings = get_settings()
    return "http://127.0.0.1:%s" % settings.frontend_port


def _normalize_url(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw if "://" in raw else "http://" + raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = parsed.hostname or ""
    if host in {"0.0.0.0", "::", "[::]", ""}:
        host = "127.0.0.1"
    if not parsed.port:
        return ""
    netloc = "%s:%s" % (host, parsed.port)
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))
