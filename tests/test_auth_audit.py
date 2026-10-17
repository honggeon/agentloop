"""Phase 4 local permission boundary + append-only audit log."""

import json

import pytest
from fastapi.testclient import TestClient

from backend.app import audit_service, auth
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


def _client(monkeypatch, settings):
    for module in (
        "backend.app.routes.integrations",
        "backend.app.routes.admin",
        "backend.app.routes.platform",
        "backend.app.routes.prices",
        "backend.app.routes.costs",
    ):
        monkeypatch.setattr("%s.get_settings" % module, lambda: settings)
    return TestClient(app)


def _agent_payload():
    return {
        "agent_id": "agent-1",
        "name": "Agent One",
        "endpoint": "http://agent.internal:80",
        "capabilities": ["session"],
    }


def _write_draft(settings, draft_id="promptfoo-case-x1"):
    from auditrail.integrations import promptfoo

    promptfoo._save_drafts(
        settings.platform_dir,
        [
            {
                "case_id": draft_id,
                "draft_id": draft_id,
                "status": "draft",
                "title": "示例",
                "draft": True,
                "safe_to_execute": True,
                "declared_executable": False,
                "forbidden_keys": [],
                "source_system": "promptfoo",
                "source_index": 1,
                "vars": {"user_id": "u", "message": "hi"},
                "expectations": [],
                "provider_declaration": None,
                "metadata": {},
                "unmapped_count": 0,
                "reviewed_by": None,
                "reviewed_at": None,
                "promoted_case_id": None,
            }
        ],
    )
    return draft_id


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


def test_viewer_cannot_write(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="viewer")
    client = _client(monkeypatch, settings)
    response = client.post("/api/agents", json=_agent_payload())
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert detail["code"] == "permission_denied"
    assert detail["retryable"] is False
    # permission errors never leak resource content
    assert "endpoint" not in json.dumps(detail)
    # reads stay open to viewers
    assert client.get("/api/agents").status_code == 200
    assert client.get("/api/audit-events").status_code == 200
    # viewer cannot touch imports / drafts either
    draft_id = _write_draft(settings)
    assert client.post("/api/integrations/promptfoo/drafts/%s/reject" % draft_id).status_code == 403


def test_operator_cannot_manage_prices_or_approve(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="operator")
    client = _client(monkeypatch, settings)
    # operator: imports allowed
    (settings.platform_dir / "imports").mkdir(parents=True, exist_ok=True)
    (settings.platform_dir / "imports" / "t.jsonl").write_text('{"id": "t-1"}\n', encoding="utf-8")
    assert client.post("/api/integrations/langfuse/import", json={"source_file": "imports/t.jsonl"}).status_code == 200
    # operator: draft review allowed, approve denied
    draft_id = _write_draft(settings)
    assert client.post("/api/integrations/promptfoo/drafts/%s/reject" % draft_id).status_code == 200
    draft_id2 = _write_draft(settings, "promptfoo-case-x2")
    approve = client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft_id2, json={})
    assert approve.status_code == 403
    # operator: price mutations denied
    price = client.post("/api/prices/versions", json={"price_version": "v1", "prices": []})
    assert price.status_code == 403
    # operator: agent config denied
    assert client.post("/api/agents", json=_agent_payload()).status_code == 403


def test_admin_can_manage(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="admin")
    client = _client(monkeypatch, settings)
    assert client.post("/api/agents", json=_agent_payload()).status_code == 201
    draft_id = _write_draft(settings)
    assert client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft_id, json={}).status_code == 200
    rebuild = client.post("/api/admin/run-index/rebuild")
    assert rebuild.status_code == 200


def test_auth_disabled_mode_is_explicit(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="disabled")
    client = _client(monkeypatch, settings)
    context = client.get("/api/auth/context")
    assert context.status_code == 200
    body = context.json()
    assert body["auth_mode"] == "disabled"
    assert body["production_ready"] is False
    assert "本地开发" in body["note"]
    # everything is allowed, and mutation responses state the mode
    created = client.post("/api/agents", json=_agent_payload())
    assert created.status_code == 201
    assert created.json()["auth_mode"] == "disabled"


def test_local_mode_context(monkeypatch, tmp_path):
    settings = _settings(tmp_path, auth_mode="local", auth_role="operator")
    client = _client(monkeypatch, settings)
    body = client.get("/api/auth/context").json()
    assert body["auth_mode"] == "local"
    assert body["role"] == "operator"
    assert body["permissions"]["import"] is True
    assert body["permissions"]["manage_prices"] is False
    assert body["production_ready"] is False


def test_misconfigured_auth_fails_closed(tmp_path):
    settings = _settings(tmp_path, auth_mode="bogus")
    with pytest.raises(Exception):
        auth.current_identity(settings)


# ---------------------------------------------------------------------------
# audit log
# ---------------------------------------------------------------------------


def test_audit_log_append_query_and_redaction(tmp_path):
    settings = _settings(tmp_path)
    event = audit_service.append_event(
        settings,
        actor="local-admin",
        role="admin",
        action="agent_create",
        resource_type="agent",
        resource_id="agent-1",
        result="success",
    )
    assert event["event_id"].startswith("audit-")
    assert event["timestamp"]

    # secrets never enter the log
    leaked = audit_service.append_event(
        settings,
        actor="local-admin",
        role="admin",
        action="agent_update",
        resource_type="agent",
        resource_id="agent-1",
        reason="failed with Bearer sk-secret-token",
    )
    assert leaked["reason"] == "[redacted]"

    result = audit_service.query_events(settings, resource_type="agent")
    assert result["total"] == 2
    assert result["has_more"] is False
    filtered = audit_service.query_events(settings, action="agent_update")
    assert filtered["total"] == 1
    # raw file is append-only JSONL
    lines = (settings.platform_dir / "audit" / "audit_events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["event_id"] for line in lines)


def test_audit_write_failure_is_explicit(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    settings.platform_dir.chmod(0o555)  # read-only: the append must fail loudly
    try:
        with pytest.raises(audit_service.AuditLogError):
            audit_service.append_event(
                settings,
                actor="local-admin",
                role="admin",
                action="agent_create",
                resource_type="agent",
                resource_id="agent-1",
            )
    finally:
        settings.platform_dir.chmod(0o755)


def test_unknown_audit_action_rejected(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(ValueError):
        audit_service.append_event(
            settings,
            actor="a",
            role="admin",
            action="drop_database",
            resource_type="agent",
        )


def test_audit_events_via_api_and_agent_actions_audited(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    client.post("/api/agents", json=_agent_payload())
    client.put("/api/agents/agent-1", json={"description": "更新"})
    client.delete("/api/agents/agent-1")
    events = client.get("/api/audit-events", params={"resource_type": "agent"})
    actions = [item["action"] for item in events.json()["audit_events"]]
    assert actions == ["agent_delete", "agent_update", "agent_create"]  # newest first
    # audit payloads never carry the endpoint URL or metadata
    for item in events.json()["audit_events"]:
        assert "endpoint" not in json.dumps(item)
