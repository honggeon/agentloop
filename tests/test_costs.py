"""Phase 2 cost records: model, import pipeline, price table, aggregation."""

import json

import pytest
from fastapi.testclient import TestClient

from auditrail import costs as cost_mod
from backend.app.config import Settings
from backend.app.main import app


def _settings(tmp_path):
    return Settings(
        runs_dir=tmp_path / "runs",
        platform_dir=tmp_path / "platform",
        agent_allowed_hosts=("agent.internal",),
        agent_allowed_ports=(80, 443),
    )


def _client(monkeypatch, tmp_path):
    settings = _settings(tmp_path)
    for module in ("backend.app.routes.platform", "backend.app.routes.costs"):
        monkeypatch.setattr("%s.get_settings" % module, lambda: settings)
    return TestClient(app), settings


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _make_run_with_observed_cost(runs_dir, run_id, cost=0.0042):
    run_dir = runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write(
        run_dir / "run.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": "completed",
            "status_reason": None,
            "started_at": "2026-01-05T10:00:00Z",
            "finished_at": "2026-01-05T10:00:02Z",
            "agent_ref": {"id": "agent-001"},
            "test_case_ref": None,
            "suite_ref": None,
            "artifact_manifest": ["llm_calls.json"],
        },
    )
    _write(
        run_dir / "llm_calls.json",
        {
            "llm_calls": [
                {
                    "callId": "call-1",
                    "model": "deepseek-chat",
                    "provider": "deepseek",
                    "tokenUsage": {"inputTokens": 100, "outputTokens": 50, "totalTokens": 150},
                    "cost": cost,
                }
            ]
        },
    )
    return run_dir


def _proxy_lines(run_id=None):
    rows = [
        {
            "callId": "proxy-1",
            "provider": "proxy",
            "model": "deepseek-chat",
            "startTime": "2026-01-05T10:00:00Z",
            "tokenUsage": {"inputTokens": 1000, "outputTokens": 500, "totalTokens": 1500},
            "total_cost": 0.01,
            "currency": "USD",
        },
        {
            "callId": "proxy-2",
            "provider": "proxy",
            "model": "deepseek-chat",
            "tokenUsage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        },
    ]
    if run_id:
        for row in rows:
            row["run_id"] = run_id
    return rows


# ---------------------------------------------------------------------------
# record model
# ---------------------------------------------------------------------------


def test_normalize_record_rejects_empty():
    with pytest.raises(ValueError):
        cost_mod.normalize_cost_record({}, source="imported")
    with pytest.raises(ValueError):
        cost_mod.normalize_cost_record({"total_tokens": 1}, source="bogus")


def test_metadata_redaction_drops_secrets_and_prompts():
    record = cost_mod.normalize_cost_record(
        {
            "total_tokens": 5,
            "metadata": {
                "authorization": "Bearer secret",
                "api_key": "sk-123",
                "cookie": "session=abc",
                "prompt": "full private prompt",
                "note": "safe",
            },
        },
        source="imported",
    )
    assert record["metadata"] == {"note": "safe"}


# ---------------------------------------------------------------------------
# import pipeline (API)
# ---------------------------------------------------------------------------


def test_import_observed_proxy_log_as_imported(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _write_jsonl(settings.runs_dir / "proxy.jsonl", _proxy_lines(run_id="run-1"))
    response = client.post(
        "/api/costs/import",
        json={"source_system": "metrics_proxy", "source_file": "proxy.jsonl"},
    )
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["status"] == "completed"
    assert report["total_records"] == 2
    assert report["imported_records"] == 2
    assert report["duplicate_records"] == 0
    assert report["invalid_records"] == 0

    stored = cost_mod.load_cost_records(settings.platform_dir)
    assert len(stored) == 2
    assert all(record["source_system"] == "metrics_proxy" for record in stored)
    assert all(record["imported_at"] for record in stored)
    by_id = {record["source_id"]: record for record in stored}
    assert by_id["proxy-1"]["source"] == "imported"
    assert by_id["proxy-1"]["total_cost"] == 0.01
    assert by_id["proxy-1"]["currency"] == "USD"
    # no price table and no explicit cost -> tokens are never treated as dollars
    assert by_id["proxy-2"]["total_cost"] is None
    assert by_id["proxy-2"]["source"] == "imported"

    # the report is retrievable
    fetched = client.get("/api/costs/imports/%s" % report["import_id"])
    assert fetched.status_code == 200
    assert fetched.json()["import_id"] == report["import_id"]


def test_import_is_idempotent_per_source_id(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _write_jsonl(settings.runs_dir / "proxy.jsonl", _proxy_lines(run_id="run-1"))
    first = client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "proxy.jsonl"}).json()
    second = client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "proxy.jsonl"}).json()
    assert second["imported_records"] == 0
    assert second["duplicate_records"] == 2
    assert len(cost_mod.load_cost_records(settings.platform_dir)) == 2
    assert first["import_id"] != second["import_id"]


def test_import_invalid_lines_recorded_not_fatal(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    path = settings.runs_dir / "mixed.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"callId": "ok-1", "tokenUsage": {"totalTokens": 5}}\n'
        "{not json\n"
        '"just a string"\n'
        '{"callId": "ok-2", "total_cost": 0.5, "currency": "USD"}\n',
        encoding="utf-8",
    )
    report = client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "mixed.jsonl"}).json()
    assert report["status"] == "completed_with_errors"
    assert report["imported_records"] == 2
    assert report["invalid_records"] == 2
    assert len(report["errors"]) == 2
    assert not any("secret" in str(error).lower() for error in report["errors"])


def test_import_dry_run_does_not_persist(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _write_jsonl(settings.runs_dir / "proxy.jsonl", _proxy_lines())
    report = client.post(
        "/api/costs/import",
        json={"source_system": "metrics_proxy", "source_file": "proxy.jsonl", "dry_run": True},
    ).json()
    assert report["status"] == "dry_run"
    assert report["imported_records"] == 2
    assert cost_mod.load_cost_records(settings.platform_dir) == []
    # dry-run reports are not part of the formal store
    assert client.get("/api/costs/imports/%s" % report["import_id"]).status_code == 404


def test_import_path_traversal_rejected(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    outside = tmp_path / "outside.jsonl"
    _write_jsonl(outside, _proxy_lines())
    for candidate in ("../outside.jsonl", str(outside), "/etc/passwd", "runs/../../outside.jsonl"):
        response = client.post(
            "/api/costs/import", json={"source_system": "metrics_proxy", "source_file": candidate}
        )
        assert response.status_code == 400, candidate
    assert "\x00" not in "x"  # sanity
    response = client.post(
        "/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "a\x00b"}
    )
    assert response.status_code == 400


def test_import_file_limits(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    big = settings.runs_dir / "big.jsonl"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_text('{"callId": "x", "totalTokens": 1}\n', encoding="utf-8")
    monkeypatch.setattr(cost_mod, "MAX_IMPORT_BYTES", 10)
    response = client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "big.jsonl"})
    assert response.status_code == 400


def test_import_never_executes_file_content(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    marker = tmp_path / "pwned"
    payload = '{"callId": "x", "totalTokens": 1, "metadata": {"cmd": "touch %s"}}\n' % marker
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    (settings.runs_dir / "evil.jsonl").write_text(payload, encoding="utf-8")
    client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "evil.jsonl"})
    assert not marker.exists()


# ---------------------------------------------------------------------------
# observed vs imported vs estimated
# ---------------------------------------------------------------------------


def test_run_costs_merge_observed_and_imported(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _make_run_with_observed_cost(settings.runs_dir, "run-1", cost=0.0042)
    _write_jsonl(settings.runs_dir / "proxy.jsonl", _proxy_lines(run_id="run-1"))
    client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "proxy.jsonl"})

    body = client.get("/api/runs/run-1/costs").json()
    sources = {record["source"] for record in body["records"]}
    assert "observed" in sources
    assert "imported" in sources
    # both sources kept; aggregate marks the mix
    assert body["summary"]["source"] == "mixed"
    assert body["summary"]["total"] is not None
    # observed record content is intact (not overwritten by the import)
    observed = [record for record in body["records"] if record["source"] == "observed"]
    assert observed[0]["total_cost"] == 0.0042


def test_run_costs_missing_without_data(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    run_dir = settings.runs_dir / "run-empty"
    run_dir.mkdir(parents=True)
    _write(
        run_dir / "run.json",
        {
            "schema_version": 1,
            "run_id": "run-empty",
            "status": "completed",
            "status_reason": None,
            "started_at": None,
            "finished_at": None,
            "agent_ref": None,
            "test_case_ref": None,
            "suite_ref": None,
            "artifact_manifest": [],
        },
    )
    body = client.get("/api/runs/run-empty/costs").json()
    assert body["summary"]["total"] is None  # never a fabricated 0
    assert body["summary"]["source"] == "missing"


def test_estimated_records_carry_price_version(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _write(
        settings.platform_dir / "price_table.json",
        {
            "schema_version": 1,
            "price_version": "2026-01",
            "models": {
                "deepseek-chat": {
                    "currency": "USD",
                    "input_cost_per_1m_tokens": 0.14,
                    "output_cost_per_1m_tokens": 0.28,
                    "effective_at": "2026-01-01T00:00:00Z",
                }
            },
        },
    )
    _write_jsonl(settings.runs_dir / "proxy.jsonl", _proxy_lines(run_id="run-1"))
    client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "proxy.jsonl"})
    stored = {record["source_id"]: record for record in cost_mod.load_cost_records(settings.platform_dir)}
    assert stored["proxy-2"]["source"] == "estimated"
    assert stored["proxy-2"]["price_version"] == "2026-01-01T00:00:00Z"
    assert stored["proxy-2"]["total_cost"] is not None
    assert stored["proxy-2"]["currency"] == "USD"
    # explicit imported cost is never replaced by an estimate
    assert stored["proxy-1"]["source"] == "imported"


def test_agent_costs_aggregates_runs(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    _make_run_with_observed_cost(settings.runs_dir, "run-1", cost=0.0042)
    body = client.get("/api/agents/agent-001/costs").json()
    assert body["summary"]["total"] == 0.0042
    assert body["summary"]["source"] == "observed"


def test_cost_responses_do_not_leak_secrets(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    rows = [
        {
            "callId": "proxy-1",
            "totalTokens": 5,
            "metadata": {"authorization": "Bearer topsecret", "note": "ok"},
        }
    ]
    _write_jsonl(settings.runs_dir / "proxy.jsonl", rows)
    client.post("/api/costs/import", json={"source_system": "metrics_proxy", "source_file": "proxy.jsonl"})
    body = client.get("/api/agents/agent-001/costs")
    assert "topsecret" not in body.text
    assert "Bearer" not in body.text
