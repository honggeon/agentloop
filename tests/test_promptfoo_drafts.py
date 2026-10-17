"""Phase 4 promptfoo draft review: list/get/approve/reject/archive, audit,
idempotent promotion, executable-content gating. No provider/CLI execution."""

import json

from fastapi.testclient import TestClient

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
    monkeypatch.setattr("backend.app.routes.integrations.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.routes.admin.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.routes.platform.get_settings", lambda: settings)
    return TestClient(app)


def _import_config(client, settings, config, name="promptfoo.json"):
    path = settings.platform_dir / "imports" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    response = client.post("/api/integrations/promptfoo/import", json={"source_file": "imports/%s" % name})
    assert response.status_code == 200, response.text
    return response.json()


def _safe_config():
    return {
        "providers": [{"id": "openai:gpt-4o-mini", "label": "declared only"}],
        "tests": [
            {
                "description": "问候场景",
                "vars": {"user_id": "user-1", "message": "你好"},
                "assert": [{"type": "icontains", "value": "你好"}],
            }
        ],
    }


def _executable_config():
    return {
        "providers": [{"id": "exec:provider", "command": "run-something"}],
        "tests": [{"description": "danger", "vars": {"user_id": "u", "message": "hi"}, "assert": []}],
    }


# ---------------------------------------------------------------------------
# list / get
# ---------------------------------------------------------------------------


def test_drafts_list_and_get(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _import_config(client, settings, _safe_config())
    listing = client.get("/api/integrations/promptfoo/drafts")
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    draft = body["drafts"][0]
    assert draft["status"] == "draft"
    assert draft["safe_to_execute"] is True
    assert draft["declared_executable"] is False
    assert draft["provider_declaration"]["executable"] is False

    detail = client.get("/api/integrations/promptfoo/drafts/%s" % draft["draft_id"])
    assert detail.status_code == 200
    assert detail.json()["draft"]["draft_id"] == draft["draft_id"]

    missing = client.get("/api/integrations/promptfoo/drafts/draft-ghost")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "draft_not_found"


# ---------------------------------------------------------------------------
# approve
# ---------------------------------------------------------------------------


def test_approve_creates_stable_test_case_with_source_ref(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _import_config(client, settings, _safe_config())
    draft = client.get("/api/integrations/promptfoo/drafts").json()["drafts"][0]

    approved = client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft["draft_id"], json={})
    assert approved.status_code == 200, approved.text
    body = approved.json()
    case = body["test_case"]
    assert case["case_id"] == draft["case_id"]
    assert case["version"] == 1
    source_ref = case["source_ref"]
    assert source_ref["source_system"] == "promptfoo"
    assert source_ref["source_id"] == draft["draft_id"]
    assert source_ref["approved_by"]
    assert source_ref["approved_at"]
    assert body["draft"]["status"] == "approved"
    assert body["draft"]["promoted_case_id"] == case["case_id"]

    # repeat approve is idempotent: same TestCase, no duplicate
    again = client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft["draft_id"], json={})
    assert again.status_code == 200
    assert again.json()["idempotent_replay"] is True
    assert again.json()["test_case"]["case_id"] == case["case_id"]

    fetched = client.get("/api/test-cases/%s" % case["case_id"])
    assert fetched.status_code == 200
    assert fetched.json()["test_case"]["source_ref"]["source_system"] == "promptfoo"


def test_approve_requires_derivable_input(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    config = _safe_config()
    config["tests"][0]["vars"] = {"name": "ada"}  # no user_id/message
    _import_config(client, settings, config)
    draft = client.get("/api/integrations/promptfoo/drafts").json()["drafts"][0]
    refused = client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft["draft_id"], json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "draft_invalid_state"
    # explicit operator override satisfies the schema re-validation
    ok = client.post(
        "/api/integrations/promptfoo/drafts/%s/approve" % draft["draft_id"],
        json={"input_override": {"user_id": "user-1", "message": "你好"}},
    )
    assert ok.status_code == 200, ok.text


def test_unmapped_assertions_stay_unmapped_after_approve(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    config = _safe_config()
    config["tests"][0]["assert"] = [
        {"type": "icontains", "value": "你好"},
        {"type": "llm-rubric", "value": "is polite"},  # unmapped: external grader
    ]
    _import_config(client, settings, config)
    draft = client.get("/api/integrations/promptfoo/drafts").json()["drafts"][0]
    assert draft["unmapped_count"] == 1
    approved = client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft["draft_id"], json={})
    assert approved.status_code == 200
    expectations = approved.json()["test_case"]["expectations"]
    unmapped = [item for item in expectations if item["type"] == "unmapped_promptfoo_assertion"]
    assert len(unmapped) == 1
    assert unmapped[0]["required"] is False  # never converted to executable


def test_executable_draft_can_never_be_approved(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _import_config(client, settings, _executable_config())
    listing = client.get("/api/integrations/promptfoo/drafts").json()
    # the executable provider was dropped at import; the test itself declares
    # no forbidden keys here, so import a draft that does:
    config = {
        "providers": ["openai:gpt-4o-mini"],
        "tests": [
            {
                "description": "with command",
                "vars": {"user_id": "u", "message": "hi"},
                "command": "rm -rf /",
                "assert": [],
            }
        ],
    }
    _import_config(client, settings, config, name="promptfoo2.json")
    drafts = client.get("/api/integrations/promptfoo/drafts").json()["drafts"]
    target = next(item for item in drafts if item["title"] == "with command")
    assert target["declared_executable"] is True
    refused = client.post("/api/integrations/promptfoo/drafts/%s/approve" % target["draft_id"], json={})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "draft_invalid_state"
    # no TestCase was created
    assert client.get("/api/test-cases/%s" % target["case_id"]).status_code == 404


# ---------------------------------------------------------------------------
# reject / archive
# ---------------------------------------------------------------------------


def test_reject_and_archive_transitions(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _import_config(client, settings, _safe_config())
    draft = client.get("/api/integrations/promptfoo/drafts").json()["drafts"][0]
    draft_id = draft["draft_id"]

    rejected = client.post("/api/integrations/promptfoo/drafts/%s/reject" % draft_id, json={"reason": "不适用"})
    assert rejected.status_code == 200
    assert rejected.json()["draft"]["status"] == "rejected"
    # rejected drafts cannot be approved
    approve = client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft_id, json={})
    assert approve.status_code == 409
    # repeat reject is idempotent
    again = client.post("/api/integrations/promptfoo/drafts/%s/reject" % draft_id, json={})
    assert again.json()["idempotent_replay"] is True
    # archive from rejected is allowed
    archived = client.post("/api/integrations/promptfoo/drafts/%s/archive" % draft_id)
    assert archived.status_code == 200
    assert archived.json()["draft"]["status"] == "archived"
    # archived cannot be approved either
    assert client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft_id, json={}).status_code == 409


def test_draft_content_is_sanitized(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    config = _safe_config()
    config["tests"][0]["vars"]["api_key"] = "sk-should-not-leak"
    config["tests"][0]["metadata"] = {"token": "sk-should-not-leak"}
    _import_config(client, settings, config)
    listing = client.get("/api/integrations/promptfoo/drafts")
    assert "sk-should-not-leak" not in listing.text


def test_approve_writes_audit_event(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    client = _client(monkeypatch, settings)
    _import_config(client, settings, _safe_config())
    draft = client.get("/api/integrations/promptfoo/drafts").json()["drafts"][0]
    client.post("/api/integrations/promptfoo/drafts/%s/approve" % draft["draft_id"], json={})
    events = client.get("/api/audit-events", params={"resource_type": "promptfoo_draft"})
    assert events.status_code == 200
    actions = [item["action"] for item in events.json()["audit_events"]]
    assert "promptfoo_draft_approve" in actions
    approve_event = next(item for item in events.json()["audit_events"] if item["action"] == "promptfoo_draft_approve")
    assert approve_event["resource_id"] == draft["draft_id"]
    assert approve_event["result"] == "success"
