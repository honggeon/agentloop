"""Phase 3 routes: external evaluation/observability integrations.

These endpoints expose the Langfuse/promptfoo adapters and the unified
external evidence store. Invariants enforced here and in the core adapters:

- imports are data-only: nothing in an import file is executed, no provider
  or Agent is called, and formal run artifacts are never modified;
- external results never overwrite deterministic checks, judge
  ``overall_status`` or score caps — they are display evidence only;
- import paths are confined to the allowed roots (``resolve_import_path``);
- ``Idempotency-Key``: same key + same fingerprint replays the original
  import, same key + different fingerprint is a 409;
- errors are redacted (exception type and safe message only — never internal
  paths, tokens or raw exception text).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException

from backend.app import integration_service
from backend.app.config import get_settings, resolve_run_path

router = APIRouter(prefix="/api")


def _validate_source(source: str) -> str:
    name = str(source or "").strip().lower()
    if name not in integration_service.IMPORT_SOURCES:
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_integration", "message": "unknown integration source %r" % source},
        )
    return name


def _run_import(source: str, payload: dict[str, Any], settings) -> dict[str, Any]:
    if source == "langfuse":
        return integration_service.run_langfuse_import(payload, settings)
    return integration_service.run_promptfoo_import(payload, settings)


@router.post("/integrations/{source}/import")
def import_integration(
    source: str,
    payload: dict[str, Any],
    idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    source = _validate_source(source)
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail={"code": "invalid_import", "message": "import payload must be an object"}
        )
    settings = get_settings()
    fingerprint = integration_service.import_fingerprint(payload)
    if idempotency_key:
        existing = integration_service.lookup_import_idempotency(idempotency_key, settings)
        if existing is not None:
            if existing.get("fingerprint") != fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "idempotency_conflict",
                        "message": "Idempotency-Key was already used with a different import payload",
                    },
                )
            report = integration_service.get_import(str(existing.get("import_id") or ""), settings)
            if report is not None:
                report = dict(report)
                report["idempotent_replay"] = True
                return report
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "idempotency_conflict",
                    "message": "Idempotency-Key references an import that no longer exists",
                },
            )
    try:
        report = _run_import(source, payload, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_import", "message": str(exc)[:300]}) from exc
    if idempotency_key and not report.get("dry_run"):
        integration_service.store_import_idempotency(
            idempotency_key, fingerprint, str(report.get("import_id") or ""), settings
        )
    return report


@router.post("/integrations/{source}/validate")
def validate_integration(source: str, payload: dict[str, Any]) -> dict[str, Any]:
    source = _validate_source(source)
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=400, detail={"code": "invalid_import", "message": "validate payload must be an object"}
        )
    try:
        return integration_service.validate_source(source, payload, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_import", "message": str(exc)[:300]}) from exc


@router.get("/integrations/imports")
def list_integration_imports() -> dict[str, Any]:
    return integration_service.list_imports(get_settings())


@router.get("/integrations/imports/{import_id}")
def get_integration_import(import_id: str) -> dict[str, Any]:
    report = integration_service.get_import(import_id, get_settings())
    if report is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "import_not_found", "message": "unknown integration import %s" % import_id},
        )
    return report


@router.get("/integrations/promptfoo/export/{case_id}")
def export_promptfoo_case(case_id: str) -> dict[str, Any]:
    """Safe static export of one TestCase as a promptfoo JSON config."""
    if not case_id or "/" in case_id or "\\" in case_id:
        raise HTTPException(status_code=400, detail={"code": "invalid_case_id", "message": "invalid case_id"})
    settings = get_settings()
    try:
        payload = integration_service.export_promptfoo_case(case_id, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_case", "message": str(exc)[:300]}) from exc
    if payload is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "case_not_found", "message": "unknown test case %s" % case_id},
        )
    return payload


# ---------------------------------------------------------- external evidence


@router.get("/runs/{run_id}/external-evaluations")
def get_run_external_evaluations(run_id: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        resolve_run_path(run_id, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_run_id", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": str(exc)}) from exc
    return integration_service.run_external_evaluations(run_id, settings)


@router.get("/runs/{run_id}/external-traces")
def get_run_external_traces(run_id: str) -> dict[str, Any]:
    settings = get_settings()
    try:
        resolve_run_path(run_id, settings)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_run_id", "message": str(exc)}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": str(exc)}) from exc
    return integration_service.run_external_traces(run_id, settings)


@router.get("/agents/{agent_id}/external-evaluations")
def get_agent_external_evaluations(agent_id: str) -> dict[str, Any]:
    if not agent_id or "/" in agent_id or "\\" in agent_id:
        raise HTTPException(status_code=400, detail={"code": "invalid_agent_id", "message": "invalid agent_id"})
    return integration_service.agent_external_evaluations(agent_id, get_settings())
