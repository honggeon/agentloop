"""Phase 3 routes: versioned price table management.

The version lifecycle (draft -> active -> retired, single active version,
reference-guarded delete, USD-only) lives in ``auditrail.prices``; this module
only assembles HTTP. Mutations are a *local* management capability: there is
no production-grade auth in this deployment, so they are gated by the
server-owned ``local_admin_enabled`` setting (``AUDITRAIL_LOCAL_ADMIN``) and
every response states its ``authority: "local"``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from auditrail import prices as price_mod

from backend.app.config import get_settings

router = APIRouter(prefix="/api/prices")

_LOCAL_NOTE = "本地管理能力：无生产级权限控制"


def _require_local_admin(settings) -> None:
    if not getattr(settings, "local_admin_enabled", False):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "admin_disabled",
                "message": "price management endpoints are disabled (AUDITRAIL_LOCAL_ADMIN)",
            },
        )


def _with_authority(payload: dict[str, Any]) -> dict[str, Any]:
    payload["authority"] = "local"
    payload["note"] = _LOCAL_NOTE
    return payload


@router.get("")
def get_prices() -> dict[str, Any]:
    """Active version, all versions and the referenced-version list."""
    settings = get_settings()
    return price_mod.prices_overview(Path(settings.platform_dir))


@router.post("/versions", status_code=201)
def create_price_version(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    _require_local_admin(settings)
    try:
        version = price_mod.create_price_version(Path(settings.platform_dir), payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_price_version", "message": str(exc)[:300]}) from exc
    return _with_authority({"price_version_record": version})


@router.get("/versions/{price_version}")
def get_price_version(price_version: str) -> dict[str, Any]:
    settings = get_settings()
    version = price_mod.get_price_version(Path(settings.platform_dir), price_version)
    if version is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "price_version_not_found", "message": "unknown price version %s" % price_version},
        )
    referenced = version.get("price_version") in set(price_mod.referenced_price_versions(Path(settings.platform_dir)))
    return {"price_version_record": version, "referenced": referenced}


@router.post("/versions/{price_version}/activate")
def activate_price_version(price_version: str) -> dict[str, Any]:
    """Activate one version; the previous active version is retired and the
    derived ``price_table.json`` is rewritten. History is never rewritten."""
    settings = get_settings()
    _require_local_admin(settings)
    try:
        version = price_mod.activate_price_version(Path(settings.platform_dir), price_version)
    except ValueError as exc:
        message = str(exc)[:300]
        status_code = 404 if "not found" in message else 400
        raise HTTPException(status_code=status_code, detail={"code": "invalid_price_version", "message": message}) from exc
    return _with_authority({"price_version_record": version})


@router.delete("/versions/{price_version}")
def delete_price_version(price_version: str) -> dict[str, Any]:
    """Delete a draft/retired version. Active or history-referenced versions
    are rejected explicitly."""
    settings = get_settings()
    _require_local_admin(settings)
    try:
        price_mod.delete_price_version(Path(settings.platform_dir), price_version)
    except ValueError as exc:
        message = str(exc)[:300]
        status_code = 404 if "not found" in message else 409
        raise HTTPException(status_code=status_code, detail={"code": "price_version_not_deletable", "message": message}) from exc
    return _with_authority({"deleted": price_version})


@router.post("/validate")
def validate_price_version(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a price version payload without storing anything."""
    errors = price_mod.validate_price_version_payload(payload)
    return {"valid": not errors, "errors": errors}
