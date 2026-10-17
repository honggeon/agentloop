"""Versioned price table management (Phase 3).

The operator-maintained ``price_table.json`` gains a controlled, versioned
lifecycle::

    platform/
      price_table.json              # active table (derived; never hand-edited required)
      price_versions/<version>.json # one immutable-ish record per version

Rules enforced here:

- a version is ``draft -> active -> retired``; exactly one version is
  ``active`` at a time (activating one retires the previous active version);
- activation rewrites the derived ``price_table.json`` so the estimation
  pipeline keeps working unchanged;
- historical ``estimated`` cost records keep the ``price_version`` they were
  computed with — price changes never rewrite history;
- a version still referenced by stored cost records (or the active one) can
  never be deleted;
- only ``USD`` is supported: entries declaring another currency are rejected
  explicitly, and missing prices stay ``null``/``missing`` — no third-party
  price is ever invented.

This module is core logic: it never imports ``backend``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .costs import PRICE_TABLE_FILE, default_price_table, load_cost_records
from .run_contract import utc_now
from .trajectory import write_json

SCHEMA_VERSION = 1

VERSIONS_DIR = "price_versions"
PRICE_STATUSES = ("draft", "active", "retired")
SUPPORTED_CURRENCIES = ("USD",)

MAX_PRICE_ENTRIES = 500


def _versions_dir(platform_dir: Path) -> Path:
    return Path(platform_dir) / VERSIONS_DIR


def _version_path(platform_dir: Path, price_version: str) -> Path:
    if (
        not price_version
        or "/" in price_version
        or "\\" in price_version
        or price_version in {".", ".."}
        or "\x00" in price_version
    ):
        raise ValueError("invalid price_version")
    return _versions_dir(platform_dir) / ("%s.json" % price_version)


# ---------------------------------------------------------------------------
# validation / normalization
# ---------------------------------------------------------------------------


def validate_price_version_payload(raw: Any) -> List[str]:
    """Return validation errors for a price version payload (empty = valid)."""
    errors: List[str] = []
    if not isinstance(raw, dict):
        return ["price version must be an object"]
    price_version = raw.get("price_version")
    if not isinstance(price_version, str) or not price_version.strip():
        errors.append("price_version is required")
    elif any(ch in price_version for ch in "/\\\x00") or price_version in {".", ".."}:
        errors.append("price_version must be a plain identifier")
    currency = raw.get("currency", "USD")
    if currency not in SUPPORTED_CURRENCIES:
        errors.append("currency %r is not supported; only USD is accepted" % (currency,))
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        errors.append("entries must be an object mapping model -> price entry")
    else:
        if len(entries) > MAX_PRICE_ENTRIES:
            errors.append("entries exceed the %d entry limit" % MAX_PRICE_ENTRIES)
        for model, entry in entries.items():
            if not isinstance(model, str) or not model.strip():
                errors.append("entry model names must be non-empty strings")
                continue
            if not isinstance(entry, dict):
                errors.append("entry %s must be an object" % model)
                continue
            for key in ("input_cost_per_1m_tokens", "output_cost_per_1m_tokens"):
                value = entry.get(key)
                if value is None:
                    continue  # missing price stays missing, never invented
                if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                    errors.append("entry %s.%s must be null or a non-negative number" % (model, key))
            entry_currency = entry.get("currency", "USD")
            if entry_currency not in SUPPORTED_CURRENCIES:
                errors.append(
                    "entry %s declares unsupported currency %r; only USD is accepted" % (model, entry_currency)
                )
    status = raw.get("status", "draft")
    if status not in PRICE_STATUSES:
        errors.append("status must be one of %s" % (list(PRICE_STATUSES),))
    return errors


def normalize_price_version(raw: Any, *, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Normalize a price version payload. Raises ``ValueError``."""
    errors = validate_price_version_payload(raw)
    if errors:
        raise ValueError("; ".join(errors[:5]))
    now = utc_now()
    entries: Dict[str, Any] = {}
    for model, entry in (raw.get("entries") or {}).items():
        entries[str(model).strip()[:128]] = {
            "input_cost_per_1m_tokens": _nullable_rate(entry.get("input_cost_per_1m_tokens")),
            "output_cost_per_1m_tokens": _nullable_rate(entry.get("output_cost_per_1m_tokens")),
            "currency": "USD",
            "effective_at": entry.get("effective_at") if isinstance(entry.get("effective_at"), str) else None,
        }
    status = str(raw.get("status") or "draft")
    if existing is not None:
        # Status transitions go through activate/retire, not silent edits.
        status = str(existing.get("status") or "draft")
    return {
        "schema_version": SCHEMA_VERSION,
        "price_version": str(raw["price_version"]).strip()[:64],
        "currency": "USD",
        "effective_at": raw.get("effective_at") if isinstance(raw.get("effective_at"), str) else None,
        "entries": entries,
        "status": status,
        "created_at": str((existing or {}).get("created_at") or raw.get("created_at") or now),
        "updated_at": now,
    }


def _nullable_rate(value: Any) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 8)


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------


def create_price_version(platform_dir: Path, raw: Any) -> Dict[str, Any]:
    """Create a new ``draft`` price version. Raises ``ValueError``."""
    payload = dict(raw) if isinstance(raw, dict) else raw
    if isinstance(payload, dict):
        payload["status"] = "draft"
    version = normalize_price_version(payload)
    path = _version_path(platform_dir, version["price_version"])
    if path.exists():
        raise ValueError("price version %s already exists" % version["price_version"])
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, version)
    return version


def list_price_versions(platform_dir: Path) -> List[Dict[str, Any]]:
    directory = _versions_dir(platform_dir)
    if not directory.is_dir():
        return []
    versions: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("price_version"):
            versions.append(data)
    versions.sort(key=lambda item: str(item.get("created_at") or ""))
    return versions


def get_price_version(platform_dir: Path, price_version: str) -> Optional[Dict[str, Any]]:
    try:
        path = _version_path(platform_dir, price_version)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def active_price_version(platform_dir: Path) -> Optional[Dict[str, Any]]:
    for version in list_price_versions(platform_dir):
        if version.get("status") == "active":
            return version
    return None


def activate_price_version(platform_dir: Path, price_version: str) -> Dict[str, Any]:
    """Activate one version; the previous active version becomes ``retired``
    and the derived ``price_table.json`` is rewritten atomically."""
    target = get_price_version(platform_dir, price_version)
    if target is None:
        raise ValueError("price version %s was not found" % price_version)
    for version in list_price_versions(platform_dir):
        if version.get("status") == "active" and version.get("price_version") != price_version:
            version["status"] = "retired"
            version["updated_at"] = utc_now()
            write_json(_version_path(platform_dir, str(version["price_version"])), version)
    target["status"] = "active"
    target["updated_at"] = utc_now()
    write_json(_version_path(platform_dir, price_version), target)
    _write_active_table(platform_dir, target)
    return target


def _write_active_table(platform_dir: Path, version: Dict[str, Any]) -> None:
    """Derive the estimation-facing ``price_table.json`` from a version."""
    models: Dict[str, Any] = {}
    for model, entry in (version.get("entries") or {}).items():
        models[model] = {
            "currency": "USD",
            "input_cost_per_1m_tokens": entry.get("input_cost_per_1m_tokens"),
            "output_cost_per_1m_tokens": entry.get("output_cost_per_1m_tokens"),
            "effective_at": entry.get("effective_at") or version.get("effective_at"),
        }
    table = {
        "schema_version": SCHEMA_VERSION,
        "price_version": version.get("price_version"),
        "models": models,
    }
    write_json(Path(platform_dir) / PRICE_TABLE_FILE, table)


def referenced_price_versions(platform_dir: Path) -> List[str]:
    """Price versions referenced by stored estimated/imported cost records."""
    referenced = set()
    for record in load_cost_records(platform_dir):
        value = record.get("price_version")
        if isinstance(value, str) and value.strip():
            referenced.add(value.strip())
    return sorted(referenced)


def delete_price_version(platform_dir: Path, price_version: str) -> None:
    """Delete a version that is neither active nor referenced by history."""
    target = get_price_version(platform_dir, price_version)
    if target is None:
        raise ValueError("price version %s was not found" % price_version)
    if target.get("status") == "active":
        raise ValueError("the active price version cannot be deleted")
    if price_version in set(referenced_price_versions(platform_dir)):
        raise ValueError("price version %s is referenced by historical cost records" % price_version)
    path = _version_path(platform_dir, price_version)
    try:
        path.unlink()
    except OSError as exc:
        raise ValueError("price version could not be deleted: %s" % type(exc).__name__) from exc


def prices_overview(platform_dir: Path) -> Dict[str, Any]:
    """Combined view for ``GET /api/prices``."""
    versions = list_price_versions(platform_dir)
    active = next((item for item in versions if item.get("status") == "active"), None)
    table_path = Path(platform_dir) / PRICE_TABLE_FILE
    has_table = table_path.is_file()
    legacy_table: Optional[Dict[str, Any]] = None
    if has_table and active is None:
        try:
            data = json.loads(table_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict) and data.get("price_version"):
            legacy_table = {"price_version": data.get("price_version"), "source": "price_table.json"}
    return {
        "schema_version": SCHEMA_VERSION,
        "active_version": active.get("price_version") if active else None,
        "versions": versions,
        "referenced_versions": referenced_price_versions(platform_dir),
        "legacy_table": legacy_table or ({"price_version": None, "source": "price_table.json"} if has_table else None),
        "default_table": default_price_table() if not has_table and not versions else None,
    }
