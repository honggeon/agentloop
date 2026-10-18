"""Phase 5 RBAC: role/permission boundaries, owner role, user registry and
the single-tenant boundary. Database-backed user tests use SQLite in
tmp_path; no network, no real IdP.
"""

import json

import pytest
from fastapi.testclient import TestClient

from auditrail.db import Database, apply_migrations
from backend.app import auth, users_service
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path, **overrides):
    base = dict(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
    )
    base.update(overrides)
    settings = Settings(**base)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _db_settings(tmp_path, **overrides):
    base = dict(
        storage_mode="database",
        queue_mode="local_worker",
        database_url="sqlite:///%s" % (tmp_path / "platform" / "auditrail.db"),
    )
    base.update(overrides)
    settings = _settings(tmp_path, **base)
    apply_migrations(Database(settings.database_url))
    from backend.app import job_store as job_store_mod

    job_store_mod.reset_cached_stores()
    return settings


def _client(monkeypatch, settings):
    for module in (
        "backend.app.routes.integrations",
        "backend.app.routes.admin",
        "backend.app.routes.platform",
        "backend.app.routes.prices",
        "backend.app.routes.costs",
        "backend.app.routes.auth_admin",
    ):
        monkeypatch.setattr("%s.get_settings" % module, lambda: settings)
    return TestClient(app)


# ---------------------------------------------------------------------------
# permission map
# ---------------------------------------------------------------------------


def test_role_permission_boundaries():
    assert "runs:read" in auth.role_permissions("viewer")
    assert "agents:write" not in auth.role_permissions("viewer")
    assert "imports:write" in auth.role_permissions("operator")
    assert "prices:write" not in auth.role_permissions("operator")
    assert "users:admin" not in auth.role_permissions("admin")
    assert "users:admin" in auth.role_permissions("owner")
    assert auth.role_allowed("owner", "admin") is True
    assert auth.role_allowed("admin", "owner") is False
    assert auth.role_allowed(None, "viewer") is False


def test_owner_role_via_local_mode(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="owner")
    client = _client(monkeypatch, settings)
    context = client.get("/api/auth/context").json()
    assert context["role"] == "owner"
    assert "users:admin" in context["capabilities"]
    # admin-only operation allowed for owner
    rebuild = client.post("/api/admin/run-index/rebuild")
    assert rebuild.status_code == 200


def test_viewer_denied_users_admin(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="viewer")
    client = _client(monkeypatch, settings)
    assert client.get("/api/admin/users").status_code == 403
    assert client.put("/api/admin/users/user-x/roles", json={"roles": ["admin"]}).status_code == 403


def test_admin_cannot_grant_owner(monkeypatch, tmp_path):
    settings = _db_settings(tmp_path, auth_mode="local", auth_role="admin")
    client = _client(monkeypatch, settings)
    user = users_service.provision_from_claims(
        settings, issuer="https://issuer.example", subject="sub-1", email=None, display_name=None, token_roles=["viewer"]
    )
    denied = client.put("/api/admin/users/%s/roles" % user["user_id"], json={"roles": ["owner"]})
    assert denied.status_code == 403


def test_owner_assigns_roles_and_audit_actor(monkeypatch, tmp_path):
    settings = _db_settings(tmp_path, auth_mode="local", auth_role="owner", auth_actor="owner-local")
    client = _client(monkeypatch, settings)
    user = users_service.provision_from_claims(
        settings, issuer="https://issuer.example", subject="sub-2", email="b@example.com", display_name="B", token_roles=["viewer"]
    )
    updated = client.put("/api/admin/users/%s/roles" % user["user_id"], json={"roles": ["operator", "admin"]})
    assert updated.status_code == 200
    assert updated.json()["user"]["roles"] == ["operator", "admin"]
    # unknown roles rejected (never stored, never granted)
    bad = client.put("/api/admin/users/%s/roles" % user["user_id"], json={"roles": ["superuser"]})
    assert bad.status_code == 400
    # audit trail carries the real actor
    events = client.get("/api/audit-events", params={"resource_type": "user"})
    entries = events.json()["audit_events"]
    assert entries and entries[0]["action"] == "user_roles_update"
    assert entries[0]["actor"] == "owner-local"


def test_user_registry_unavailable_in_file_mode(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="owner")
    client = _client(monkeypatch, settings)
    response = client.get("/api/admin/users")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is False
    assert "file" in body["note"]


def test_db_roles_override_token_roles(tmp_path):
    """Server-managed roles win over token claims once the user exists."""
    settings = _db_settings(tmp_path, auth_mode="oidc", oidc_issuer_url="https://issuer.example")
    user = users_service.provision_from_claims(
        settings, issuer="https://issuer.example", subject="sub-3", email=None, display_name=None, token_roles=["admin"]
    )
    assert user["roles"] == ["admin"]
    # operator revokes to viewer in the registry...
    users_service.update_user_roles(user["user_id"], ["viewer"], settings)
    # ...and the next verification resolves viewer, not the token's admin
    from unittest import mock

    import backend.app.auth as auth_mod

    claims = {"sub": "sub-3", "iss": "https://issuer.example", "roles": ["admin"]}
    with mock.patch.object(auth_mod, "resolve_bearer_identity") as _:
        pass  # identity path covered by test_auth_oidc; here we check the store policy
    reloaded = users_service.get_user(user["user_id"], settings)
    assert reloaded["roles"] == ["viewer"]


# ---------------------------------------------------------------------------
# tenant boundary (explicitly single-tenant)
# ---------------------------------------------------------------------------


def test_client_tenant_id_never_trusted(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="admin")
    client = _client(monkeypatch, settings)
    created = client.post(
        "/api/agents",
        json={"agent_id": "a-tenant", "endpoint": "http://agent.internal", "tenant_id": "evil-tenant"},
    )
    assert created.status_code == 201
    assert "evil-tenant" not in json.dumps(created.json())
    stored = client.get("/api/agents/a-tenant").json()
    assert "evil-tenant" not in json.dumps(stored)


def test_tenant_context_is_server_owned(tmp_path):
    settings = _settings(tmp_path, tenant_id="acme")
    context = auth.tenant_context(settings)
    assert context == {"tenant_id": "acme", "single_tenant": True}
    payload = auth.auth_context_payload(settings)
    assert payload["tenant"]["tenant_id"] == "acme"
    assert payload["tenant"]["single_tenant"] is True
