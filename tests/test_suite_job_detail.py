"""Phase 3 SuiteJob detail endpoint: aggregate view, retries, external evidence."""

import json

from fastapi.testclient import TestClient

from auditrail import evaluation as evaluation_mod
from auditrail import testsuites as suite_mod
from auditrail.integrations import external_models as ext
from auditrail.integrations import langfuse
from backend.app import platform_store
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
    monkeypatch.setattr("backend.app.routes.platform.get_settings", lambda: settings)
    monkeypatch.setattr("backend.app.routes.integrations.get_settings", lambda: settings)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    settings.platform_dir.mkdir(parents=True, exist_ok=True)
    return TestClient(app), settings


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def _make_run(settings, run_id, job_id, *, evaluation=None):
    run_dir = settings.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_json(
        run_dir / "run.json",
        {
            "schema_version": 1,
            "run_id": run_id,
            "status": "completed",
            "status_reason": None,
            "started_at": "2026-01-05T10:00:00Z",
            "finished_at": "2026-01-05T10:00:02Z",
            "agent_ref": {"id": "agent-1"},
            "test_case_ref": None,
            "suite_ref": {"id": "suite-1", "job_id": job_id},
            "artifact_manifest": [],
        },
    )
    if evaluation is not None:
        envelope = evaluation_mod.new_envelope("completed")
        envelope["final_quality"] = {"status": evaluation, "score_cap": None, "source": "deterministic"}
        _write_json(run_dir / "evaluation.json", envelope)
    return run_dir


def _make_job(settings, specs, *, job_id="suitejob-0001", status="completed", options=None):
    """specs: list of attempt dicts: order/case_id/status/run_id/retry_index."""
    suite = {"suite_id": "suite-1", "version": 1, "title": "回归集", "case_ids": [], "enabled": True}
    agent = {
        "agent_id": "agent-1",
        "name": "Agent One",
        "environment": "test",
        "version": 1,
        "config_hash": "hash",
    }
    orders = sorted({spec["order"] for spec in specs})
    snapshots = [
        {"case_id": next(spec["case_id"] for spec in specs if spec["order"] == order), "title": "case", "version": 1}
        for order in orders
    ]
    record = suite_mod.new_suite_job(
        job_id,
        suite=suite,
        agent=agent,
        judge=False,
        case_snapshots=snapshots,
        options=options or {},
        limits={"global": 4, "agent": 1},
    )
    by_order = {attempt["order"]: attempt for attempt in record["attempts"]}
    for spec in specs:
        order = spec["order"]
        if int(spec.get("retry_index") or 0) == 0:
            attempt = by_order[order]
        else:
            base = by_order[order]
            attempt = dict(base)
            attempt["attempt_id"] = "%s-r%d" % (base["attempt_id"], spec["retry_index"])
            attempt["retry_index"] = spec["retry_index"]
            record["attempts"].append(attempt)
        attempt["status"] = spec["status"]
        attempt["run_id"] = spec.get("run_id")
        attempt["finished_at"] = "2026-01-05T10:00:02Z" if spec.get("run_id") else None
    record["status"] = status
    record["started_at"] = "2026-01-05T10:00:00Z"
    record["finished_at"] = "2026-01-05T10:05:00Z"
    platform_store.save_suite_job(record, settings)
    return record


# ---------------------------------------------------------------------------
# happy path: full detail block
# ---------------------------------------------------------------------------


def test_detail_full_block(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    job_id = "suitejob-0001"
    _make_run(settings, "run-a", job_id, evaluation="success")
    _make_run(settings, "run-b", job_id)
    _make_run(settings, "run-c", job_id)
    _make_job(
        settings,
        [
            {"order": 0, "case_id": "case-1", "status": "completed", "run_id": "run-a"},
            {"order": 1, "case_id": "case-2", "status": "failed", "run_id": "run-b"},
            {"order": 2, "case_id": "case-3", "status": "skipped", "run_id": None},
            {"order": 3, "case_id": "case-4", "status": "cancelled", "run_id": "run-c"},
        ],
        job_id=job_id,
        options={"timeout_seconds": 3600, "budget": {"max_attempts": 10, "max_total_tokens": None}},
    )
    response = client.get("/api/suite-jobs/%s/detail" % job_id)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["suite_job"]["job_id"] == job_id
    assert body["suite_job"]["status"] == "completed"

    executions = body["case_executions"]
    assert len(executions) == 4
    by_case = {item["case_id"]: item for item in executions}
    assert by_case["case-1"]["final_status"] == "completed"
    assert by_case["case-2"]["final_status"] == "failed"
    assert by_case["case-3"]["final_status"] == "skipped"
    assert by_case["case-4"]["final_status"] == "cancelled"
    assert all(item["attempt_count"] == 1 for item in executions)

    summary = body["summary"]
    assert summary["total"] == 4
    assert summary["completed"] == 1
    assert summary["failed"] == 1
    assert summary["skipped"] == 1
    assert summary["cancelled"] == 1
    assert summary["unknown"] == 0
    assert summary["attempts_total"] == 4
    assert summary["retries"] == 0
    assert summary["evaluated"] == 1
    assert summary["passed"] == 1
    assert summary["pass_rate"] == 1.0

    budget = body["budget"]
    assert budget["deadline_at"] == "2026-01-05T11:00:00Z"
    assert budget["budget_exceeded"] is False
    assert budget["budget_exceeded_reason"] is None
    assert budget["budget"]["max_attempts"] == 10
    assert budget["max_retries"] == 0

    evidence = body["external_evidence"]
    assert evidence["summary"]["count"] == 0
    assert "note" in evidence


# ---------------------------------------------------------------------------
# retries
# ---------------------------------------------------------------------------


def test_detail_with_retry_history(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    job_id = "suitejob-retry"
    _make_run(settings, "run-fail", job_id)
    _make_run(settings, "run-ok", job_id, evaluation="success")
    _make_job(
        settings,
        [
            {"order": 0, "case_id": "case-1", "status": "failed", "run_id": "run-fail", "retry_index": 0},
            {"order": 0, "case_id": "case-1", "status": "completed", "run_id": "run-ok", "retry_index": 1},
            {"order": 1, "case_id": "case-2", "status": "completed", "run_id": "run-x", "retry_index": 0},
        ],
        job_id=job_id,
        options={"max_retries": 1},
    )
    body = client.get("/api/suite-jobs/%s/detail" % job_id).json()

    summary = body["summary"]
    # the retrying first attempt is not counted as a failed case
    assert summary["total"] == 2
    assert summary["failed"] == 0
    assert summary["completed"] == 2
    assert summary["attempts_total"] == 3
    assert summary["retries"] == 1

    executions = body["case_executions"]
    retry_exec = next(item for item in executions if item["case_id"] == "case-1")
    assert retry_exec["attempt_count"] == 2
    assert retry_exec["final_status"] == "completed"
    # the final attempt is the one with the higher retry_index
    attempts = {attempt["attempt_id"]: attempt for attempt in body["suite_job"]["attempts"]}
    final = next(attempt for attempt in attempts.values() if attempt.get("is_final_attempt") and attempt["order"] == 0)
    assert final["retry_index"] == 1
    assert final["run_id"] == "run-ok"
    first = next(attempt for attempt in attempts.values() if attempt["order"] == 0 and attempt["retry_index"] == 0)
    assert first["is_final_attempt"] is False
    assert first["attempt_count"] == 2


# ---------------------------------------------------------------------------
# budget exhaustion
# ---------------------------------------------------------------------------


def test_detail_budget_exceeded(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    job_id = "suitejob-budget"
    record = _make_job(
        settings,
        [{"order": 0, "case_id": "case-1", "status": "completed", "run_id": "run-a"}],
        job_id=job_id,
        options={"max_retries": 1, "budget": {"max_attempts": 2, "max_total_tokens": None}},
    )
    record["budget_used"] = {"attempts": 2, "total_tokens": 0}
    platform_store.save_suite_job(record, settings)
    body = client.get("/api/suite-jobs/%s/detail" % job_id).json()
    assert body["budget"]["budget_exceeded"] is True
    assert body["budget"]["budget_exceeded_reason"] == "attempt budget exhausted"


# ---------------------------------------------------------------------------
# external evidence stays a display-only section
# ---------------------------------------------------------------------------


def test_detail_external_evidence_partition(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    job_id = "suitejob-ext"
    _make_run(settings, "run-a", job_id, evaluation="success")
    _make_job(
        settings,
        [{"order": 0, "case_id": "case-1", "status": "completed", "run_id": "run-a"}],
        job_id=job_id,
    )
    before = client.get("/api/suite-jobs/%s/detail" % job_id).json()
    assert before["external_evidence"]["summary"]["linked_count"] == 0

    # import an external trace linked to the job's run (adapter call, no HTTP)
    source = settings.platform_dir / "imports" / "traces.jsonl"
    _write_jsonl(source, [{"id": "t-1", "metadata": {"run_id": "run-a"}}])
    context = langfuse.new_run_context()
    context["run_ids"] = {"run-a"}
    report = langfuse.import_traces(
        settings.platform_dir,
        {"kind": "file", "path": str(source)},
        allowed_roots=[settings.platform_dir],
        run_context=context,
    )
    assert report["imported_records"] == 1

    after = client.get("/api/suite-jobs/%s/detail" % job_id).json()
    evidence = after["external_evidence"]
    assert evidence["summary"]["linked_count"] >= 1
    assert evidence["summary"]["trace_count"] == 1
    assert evidence["external_traces"][0]["source_id"] == "t-1"
    assert "note" in evidence
    # external evidence never feeds the job stats or the run quality section
    assert after["suite_job"]["stats"] == before["suite_job"]["stats"]
    assert after["summary"] == before["summary"]
    assert (settings.runs_dir / "run-a" / "evaluation.json").read_bytes() is not None
    # the run directory gained no external files
    assert not (settings.runs_dir / "run-a" / "external").exists()


# ---------------------------------------------------------------------------
# errors and legacy compatibility
# ---------------------------------------------------------------------------


def test_detail_unknown_job_404(monkeypatch, tmp_path):
    client, _settings_obj = _client(monkeypatch, tmp_path)
    response = client.get("/api/suite-jobs/suitejob-ghost/detail")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "suite_job_not_found"


def test_legacy_suite_job_endpoint_still_works(monkeypatch, tmp_path):
    client, settings = _client(monkeypatch, tmp_path)
    job_id = "suitejob-legacy"
    _make_job(
        settings,
        [{"order": 0, "case_id": "case-1", "status": "completed", "run_id": "run-a"}],
        job_id=job_id,
    )
    response = client.get("/api/suite-jobs/%s" % job_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suite_job"]["job_id"] == job_id
    assert body["suite_job"]["stats"]["total"] == 1
    assert "external_evidence" not in body  # the legacy payload shape is unchanged
