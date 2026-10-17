"""Phase 3 versioned price table API: lifecycle, history, local admin gate."""

import json

from fastapi.testclient import TestClient

from auditrail import costs as cost_mod
from auditrail import prices as price_mod
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
    return Settings(**base)


def _client(monkeypatch, tmp_path, **overrides):
    settings = _settings(tmp_path, **overrides)
    monkeypatch.setattr("backend.app.routes.prices.get_settings", lambda: settings)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return TestClient(app), settings


def _version_payload(price_version, *, rate_in=0.14, rate_out=0.28, model="deepseek-chat", effective_at=None):
    entry = {"input_cost_per_1m_tokens": rate_in, "output_cost_per_1m_tokens": rate_out}
    if effective_at:
        entry["effective_at"] = effective_at
    return {"price_version": price_version, "entries": {model: entry}}


# ---------------------------------------------------------------------------
# create / validate
# ---------------------------------------------------------------------------


def test_create_draft_version(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    response = client.post("/api/prices/versions", json=_version_payload("v1"))
    assert response.status_code == 201, response.text
    body = response.json()
    record = body["price_version_record"]
    assert record["price_version"] == "v1"
    assert record["status"] == "draft"
    assert record["currency"] == "USD"
    assert body["authority"] == "local"
    # draft activation has not happened: no derived table yet
    assert not (settings.platform_dir / "price_table.json").exists()


def test_validate_rejects_non_usd_currency(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    payload = _version_payload("v-eur")
    payload["currency"] = "EUR"
    response = client.post("/api/prices/validate", json=payload)
    assert response.status_code == 200
    body = response.json()
    assert body["valid"] is False
    assert any("EUR" in error for error in body["errors"])
    # entry-level currencies are also USD-only
    payload = _version_payload("v-eur-2")
    payload["entries"]["deepseek-chat"]["currency"] = "EUR"
    assert client.post("/api/prices/validate", json=payload).json()["valid"] is False
    # a valid payload passes
    assert client.post("/api/prices/validate", json=_version_payload("v-ok")).json()["valid"] is True


def test_create_invalid_version_400(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    response = client.post("/api/prices/versions", json={"entries": {}})
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_price_version"


# ---------------------------------------------------------------------------
# activation lifecycle
# ---------------------------------------------------------------------------


def test_activate_rewrites_derived_price_table(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post("/api/prices/versions", json=_version_payload("v1"))
    response = client.post("/api/prices/versions/v1/activate")
    assert response.status_code == 200, response.text
    assert response.json()["price_version_record"]["status"] == "active"

    overview = client.get("/api/prices").json()
    assert overview["active_version"] == "v1"
    table = json.loads((settings.platform_dir / "price_table.json").read_text(encoding="utf-8"))
    assert table["price_version"] == "v1"
    assert table["models"]["deepseek-chat"]["input_cost_per_1m_tokens"] == 0.14


def test_single_active_version_previous_retired(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    client.post("/api/prices/versions", json=_version_payload("v1"))
    client.post("/api/prices/versions/v1/activate")
    client.post("/api/prices/versions", json=_version_payload("v2", rate_in=0.5, rate_out=1.0))
    client.post("/api/prices/versions/v2/activate")

    overview = client.get("/api/prices").json()
    assert overview["active_version"] == "v2"
    statuses = {item["price_version"]: item["status"] for item in overview["versions"]}
    assert statuses == {"v1": "retired", "v2": "active"}


def test_activate_unknown_version_404(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    response = client.post("/api/prices/versions/nope/activate")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# history: estimated records keep their price_version
# ---------------------------------------------------------------------------


def test_historical_estimated_records_keep_price_version(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post(
        "/api/prices/versions",
        json=_version_payload("v1", rate_in=0.14, rate_out=0.28, effective_at="2026-01-01T00:00:00Z"),
    )
    client.post("/api/prices/versions/v1/activate")

    # import a cost log through the estimation pipeline (tokens only, no cost)
    proxy_log = settings.runs_dir / "proxy.jsonl"
    proxy_log.write_text(
        json.dumps(
            {
                "callId": "proxy-1",
                "model": "deepseek-chat",
                "tokenUsage": {"inputTokens": 1000, "outputTokens": 500, "totalTokens": 1500},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    report = cost_mod.import_cost_file(settings.platform_dir, proxy_log, source_system="metrics_proxy")
    assert report["imported_records"] == 1
    stored = cost_mod.load_cost_records(settings.platform_dir)
    assert stored[0]["source"] == "estimated"
    old_price_version = stored[0]["price_version"]
    assert old_price_version == "2026-01-01T00:00:00Z"
    old_total = stored[0]["total_cost"]

    # activate a more expensive table: history is never rewritten
    client.post(
        "/api/prices/versions",
        json=_version_payload("v2", rate_in=1.4, rate_out=2.8, effective_at="2026-02-01T00:00:00Z"),
    )
    client.post("/api/prices/versions/v2/activate")
    stored_after = cost_mod.load_cost_records(settings.platform_dir)
    assert stored_after[0]["price_version"] == old_price_version
    assert stored_after[0]["total_cost"] == old_total

    # new estimations use the new table
    table = cost_mod.load_price_table(settings.platform_dir)
    estimate = cost_mod.estimate_cost(table, "deepseek-chat", 1000, 500)
    assert estimate is not None
    assert estimate["price_version"] == "2026-02-01T00:00:00Z"
    assert estimate["total_cost"] > old_total


# ---------------------------------------------------------------------------
# delete rules
# ---------------------------------------------------------------------------


def test_delete_active_version_rejected(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    client.post("/api/prices/versions", json=_version_payload("v1"))
    client.post("/api/prices/versions/v1/activate")
    response = client.delete("/api/prices/versions/v1")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "price_version_not_deletable"


def test_delete_referenced_version_rejected(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post("/api/prices/versions", json=_version_payload("v1"))
    client.post("/api/prices/versions/v1/activate")
    client.post("/api/prices/versions", json=_version_payload("v2"))
    client.post("/api/prices/versions/v2/activate")
    # v1 is retired but referenced by a stored cost record
    records = [
        {
            "record_id": "cost-x",
            "source": "estimated",
            "source_system": "metrics_proxy",
            "source_id": "s-1",
            "total_tokens": 5,
            "total_cost": 0.001,
            "currency": "USD",
            "price_version": "v1",
        }
    ]
    cost_mod.save_cost_records(settings.platform_dir, records)
    assert "v1" in price_mod.referenced_price_versions(settings.platform_dir)
    response = client.delete("/api/prices/versions/v1")
    assert response.status_code == 409


def test_delete_unreferenced_draft_ok(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    client.post("/api/prices/versions", json=_version_payload("v-draft"))
    response = client.delete("/api/prices/versions/v-draft")
    assert response.status_code == 200
    assert response.json()["deleted"] == "v-draft"
    assert price_mod.get_price_version(settings.platform_dir, "v-draft") is None


# ---------------------------------------------------------------------------
# local admin gate
# ---------------------------------------------------------------------------


def test_local_admin_disabled_blocks_mutations(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path, local_admin_enabled=False)
    response = client.post("/api/prices/versions", json=_version_payload("v1"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "admin_disabled"
    response = client.post("/api/prices/versions/v1/activate")
    assert response.status_code == 403
    response = client.delete("/api/prices/versions/v1")
    assert response.status_code == 403
    # reads stay available
    assert client.get("/api/prices").status_code == 200
    assert client.post("/api/prices/validate", json=_version_payload("v1")).status_code == 200


# ---------------------------------------------------------------------------
# missing prices stay missing
# ---------------------------------------------------------------------------


def test_null_rates_estimate_to_none_not_invented(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    payload = _version_payload("v-null")
    payload["entries"]["deepseek-chat"] = {
        "input_cost_per_1m_tokens": None,
        "output_cost_per_1m_tokens": None,
    }
    assert client.post("/api/prices/validate", json=payload).json()["valid"] is True
    client.post("/api/prices/versions", json=payload)
    client.post("/api/prices/versions/v-null/activate")
    table = cost_mod.load_price_table(settings.platform_dir)
    assert cost_mod.estimate_cost(table, "deepseek-chat", 1000, 500) is None
    # unknown models are missing too
    assert cost_mod.estimate_cost(table, "unknown-model", 1000, 500) is None


def test_get_single_version_and_referenced_flag(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    client.post("/api/prices/versions", json=_version_payload("v1"))
    body = client.get("/api/prices/versions/v1").json()
    assert body["price_version_record"]["price_version"] == "v1"
    assert body["referenced"] is False
    assert client.get("/api/prices/versions/nope").status_code == 404
