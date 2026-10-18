"""Phase 5 user registry (database-backed, single-tenant).

Users are provisioned just-in-time on first successful OIDC/JWT
verification. Role resolution policy:

- a stored user record's roles are server-managed and win over token
  claims (an operator can revoke a role without waiting for the IdP);
- a first-seen user keeps the token's recognized roles;
- unknown role names are never stored nor granted.

The registry lives in the database (``users`` table). In ``file`` storage
mode there is no user registry: the API reports that honestly instead of
pretending multi-user management exists.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from auditrail.db import Database, dumps_json, loads_json
from auditrail.run_contract import utc_now

from backend.app.auth import ROLES
from backend.app.config import Settings


class UserStoreError(RuntimeError):
    """User registry unavailable or the operation is not allowed."""


def _db(settings: Settings) -> Database:
    from backend.app import job_store as job_store_mod

    return job_store_mod.database_for_settings(settings)


def _tenant(settings: Settings) -> str:
    return str(getattr(settings, "tenant_id", "default") or "default")


def available(settings: Settings) -> bool:
    return str(getattr(settings, "storage_mode", "file") or "file") != "file"


def _row_to_user(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "user_id": row.get("user_id"),
        "issuer": row.get("issuer"),
        "subject": row.get("subject"),
        "email": row.get("email"),
        "display_name": row.get("display_name"),
        "roles": loads_json(row.get("roles_json"), default=[]) or [],
        "status": row.get("status"),
        "first_seen_at": row.get("first_seen_at"),
        "last_seen_at": row.get("last_seen_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def provision_from_claims(
    settings: Settings,
    *,
    issuer: str,
    subject: str,
    email: Optional[str],
    display_name: Optional[str],
    token_roles: List[str],
) -> Optional[Dict[str, Any]]:
    """JIT upsert on successful verification; returns the stored user.

    Server-managed roles win: an existing record keeps its assigned roles.
    """
    if not available(settings):
        return None
    db = _db(settings)
    tenant = _tenant(settings)
    now = utc_now()
    row = db.query_one(
        "SELECT * FROM users WHERE tenant_id = %s AND subject = %s"
        " AND (issuer = %s OR (issuer IS NULL AND %s IS NULL))",
        (tenant, subject, issuer or None, issuer or None),
    )
    recognized = [role for role in token_roles if role in ROLES]
    if row is None:
        user_id = "user-%s" % uuid.uuid4().hex[:12]
        db.execute(
            "INSERT INTO users (tenant_id, user_id, issuer, subject, email, display_name, roles_json, status,"
            " first_seen_at, last_seen_at, created_at, updated_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                tenant,
                user_id,
                issuer or None,
                subject,
                (str(email)[:128] if email else None),
                (str(display_name)[:128] if display_name else None),
                dumps_json(recognized),
                "active",
                now,
                now,
                now,
                now,
            ),
        )
    else:
        user_id = str(row["user_id"])
        db.execute(
            "UPDATE users SET last_seen_at = %s, email = COALESCE(%s, email),"
            " display_name = COALESCE(%s, display_name), updated_at = %s"
            " WHERE tenant_id = %s AND user_id = %s",
            (now, (str(email)[:128] if email else None), (str(display_name)[:128] if display_name else None), now, tenant, user_id),
        )
    return get_user(user_id, settings)


def get_user(user_id: str, settings: Settings) -> Optional[Dict[str, Any]]:
    if not available(settings):
        return None
    row = _db(settings).query_one(
        "SELECT * FROM users WHERE tenant_id = %s AND user_id = %s",
        (_tenant(settings), str(user_id)),
    )
    return _row_to_user(row) if row else None


def list_users(settings: Settings) -> List[Dict[str, Any]]:
    if not available(settings):
        return []
    rows = _db(settings).query(
        "SELECT * FROM users WHERE tenant_id = %s ORDER BY created_at DESC",
        (_tenant(settings),),
    )
    return [_row_to_user(row) for row in rows]


def update_user_roles(user_id: str, roles: List[str], settings: Settings) -> Dict[str, Any]:
    """Assign server-managed roles. Unknown roles are rejected (never
    stored, never granted)."""
    if not available(settings):
        raise UserStoreError("user registry requires database storage mode")
    cleaned: List[str] = []
    for role in roles or []:
        name = str(role).strip().lower()
        if name not in ROLES:
            raise UserStoreError("unknown role %r" % role)
        if name not in cleaned:
            cleaned.append(name)
    if not cleaned:
        raise UserStoreError("roles must contain at least one recognized role")
    db = _db(settings)
    updated = db.execute(
        "UPDATE users SET roles_json = %s, updated_at = %s WHERE tenant_id = %s AND user_id = %s",
        (dumps_json(cleaned), utc_now(), _tenant(settings), str(user_id)),
    )
    if updated == 0:
        raise KeyError(user_id)
    user = get_user(user_id, settings)
    if user is None:
        raise KeyError(user_id)
    return user
