"""Run Contract v1: run.json lifecycle, legacy compatibility, error.json, atomicity."""

import json

import pytest

from auditrail import run_contract
from auditrail.run_contract import (
    collect_artifact_manifest,
    load_run_state,
    new_run_record,
    read_error,
    read_run_record,
    safe_status_subset,
    update_run_status,
    write_error,
    write_run_record,
)


def _record(run_id="run-1"):
    return new_run_record(
        run_id,
        agent_ref={"id": "agent-1", "version": "git:abc", "config_hash": "h"},
        test_case_ref={"id": "case-1", "version": 1},
    )


def test_new_record_shape_and_enums(tmp_path):
    record = _record()
    assert record["schema_version"] == 1
    assert record["status"] == "queued"
    assert record["status_reason"] is None
    assert record["suite_ref"] is None
    assert record["source_ref"] == {"system": "platform", "source_id": None}
    assert set(run_contract.RUN_STATUSES) == {
        "queued",
        "running",
        "completed",
        "failed",
        "timed_out",
        "cancelled",
        "skipped",
    }


def test_write_and_read_run_record_roundtrip(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    write_run_record(run_dir, _record())
    loaded = read_run_record(run_dir)
    assert loaded["run_id"] == "run-1"
    assert loaded["status"] == "queued"


def test_write_run_record_rejects_illegal_status(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    record = _record()
    record["status"] = "success"  # judge overall status is not a Run status
    with pytest.raises(ValueError):
        write_run_record(run_dir, record)


def test_status_transitions_set_timestamps_and_manifest(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    write_run_record(run_dir, _record())
    record = update_run_status(run_dir, "running")
    assert record["started_at"]
    assert record["finished_at"] is None
    (run_dir / "summary.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    (run_dir / ".summary.json.123.tmp").write_text("{partial", encoding="utf-8")
    record = update_run_status(run_dir, "completed")
    assert record["finished_at"]
    assert "summary.json" in record["artifact_manifest"]
    # staging files never enter the manifest
    assert not any(name.endswith(".tmp") for name in record["artifact_manifest"])


def test_manifest_excludes_unparseable_json(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text("{not json", encoding="utf-8")
    (run_dir / "audit.json").write_text(json.dumps({"findings": []}), encoding="utf-8")
    manifest = collect_artifact_manifest(run_dir)
    assert "audit.json" in manifest
    assert "summary.json" not in manifest


def test_legacy_run_without_run_json_infers_completed(tmp_path):
    run_dir = tmp_path / "20260101-000000-legacy"
    run_dir.mkdir()
    (run_dir / "summary.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    state = load_run_state(run_dir)
    assert state["status"] == "completed"
    assert state["legacy_inferred"] is True
    assert state["agent_ref"] is None
    assert state["test_case_ref"] is None
    # reading must not write run.json back into the legacy run
    assert not (run_dir / "run.json").exists()


def test_legacy_run_without_any_artifacts_infers_failed(tmp_path):
    run_dir = tmp_path / "empty-run"
    run_dir.mkdir()
    state = load_run_state(run_dir)
    assert state["status"] == "failed"
    assert state["legacy_inferred"] is True


def test_write_error_and_read_back(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    payload = write_error(
        run_dir,
        phase="collect",
        error_type="agent_timeout",
        retryable=True,
        safe_message="agent call timed out",
        external_status=None,
    )
    loaded = read_error(run_dir)
    assert loaded == payload
    assert loaded["phase"] == "collect"
    assert loaded["error_type"] == "agent_timeout"
    assert loaded["retryable"] is True
    assert loaded["timestamp"]


def test_safe_status_subset_projects_only_public_fields(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    write_run_record(run_dir, _record())
    subset = safe_status_subset(load_run_state(run_dir), stale=True)
    assert subset["status"] == "queued"
    assert subset["stale"] is True
    assert "schema_version" in subset or subset["run_id"] == "run-1"


def test_run_json_write_is_atomic_and_complete(tmp_path):
    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    write_run_record(run_dir, _record())
    # no staging residue after a successful write
    assert not list(run_dir.glob(".*.tmp"))
    # the file is always complete JSON
    json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
