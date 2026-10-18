import json
import threading
from pathlib import Path

import pytest

from auditrail.agents.schemas import normalize_judge_report
from auditrail.agents.tools import write_judge_report
from auditrail.judges import reports
from tests.conftest import full_judge_dimensions


def _write_run(base: Path) -> Path:
    run = base / "runs" / "run-1"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps(
            {
                "tool_calls": [],
                "findings": [],
                "phases": [],
                "summary": {"requested_deliverables": []},
            }
        ),
        encoding="utf-8",
    )
    (run / "trajectory.jsonl").write_text("", encoding="utf-8")
    (run / "session.json").write_text(json.dumps({"session_id": "s1"}), encoding="utf-8")
    return run


def _publish(run: Path, engine: str, status: str, score: int, *, published_at: str, findings=None) -> None:
    write_judge_report(
        str(run),
        {
            "overall_status": status,
            "completion_score": score,
            "confidence": "high",
            "summary": "%s summary" % engine,
            "findings": findings or [],
            "dimensions": full_judge_dimensions(),
        },
        judge_metadata={"engine": engine, "runtime": engine},
        judge_engine=engine,
        published_at=published_at,
    )


def _complete_ppt_run(base: Path) -> Path:
    run = base / "runs" / "ppt-run"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"title": "生成本次演示文稿", "session_id": "s1"}), encoding="utf-8")
    (run / "audit.json").write_text(json.dumps({"tool_calls": [], "findings": [], "phases": []}), encoding="utf-8")
    (run / "trajectory.jsonl").write_text(
        json.dumps({"index": 0, "role": "user", "content": "请生成 .pptx 演示文稿"}, ensure_ascii=False) + "\n"
        + json.dumps({"index": 1, "role": "assistant", "content": "已完成"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return run


# -- storage layout ----------------------------------------------------


def test_write_judge_report_archives_one_report_per_engine(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 95, published_at="2026-10-13T00:00:00+00:00")
    _publish(run, "dsh", "failed", 30, published_at="2026-10-13T00:01:00+00:00")

    for engine in ("deepagent", "dsh"):
        directory = run / "judges" / engine
        assert (directory / "agent_judge.json").is_file()
        assert (directory / "agent_judge.md").is_file()
        assert (directory / "chart_data.json").is_file()

    manifest = json.loads((run / "judges" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["active_engine"] == "dsh"
    assert [entry["engine"] for entry in manifest["reports"]] == ["deepagent", "dsh"]
    # Root files stay the active report and remain readable by old clients.
    root = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    assert root["judge"]["engine"] == "dsh"
    assert root["overall_status"] == "failed"


def test_archived_report_is_the_same_normalized_document(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "partial", 60, published_at="2026-10-13T00:00:00+00:00")

    root = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    archived = json.loads((run / "judges" / "deepagent" / "agent_judge.json").read_text(encoding="utf-8"))
    assert archived == root
    assert normalize_judge_report(archived)["dimensions"] == root["dimensions"]
    markdown = (run / "judges" / "deepagent" / "agent_judge.md").read_text(encoding="utf-8")
    assert "### Evaluation Dimensions" in markdown
    charts = json.loads((run / "judges" / "deepagent" / "chart_data.json").read_text(encoding="utf-8"))
    assert charts["dimension_scores"] == root["charts"]["dimension_scores"]


def test_list_judge_reports_reports_active_engine_and_availability(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")
    _publish(run, "dsh", "partial", 55, published_at="2026-10-13T00:01:00+00:00")

    manifest = reports.list_judge_reports(run)
    assert manifest["version"] == 1
    assert manifest["active_engine"] == "dsh"
    assert reports.available_judge_engines(run) == ["deepagent", "dsh"]
    assert all(entry["available"] is True for entry in manifest["reports"])


def test_read_judge_report_selects_one_engine_and_reports_missing(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")
    _publish(run, "dsh", "partial", 55, published_at="2026-10-13T00:01:00+00:00")

    deepagent = reports.read_judge_report(run, "deepagent")
    dsh = reports.read_judge_report(run, "dsh")
    assert deepagent["overall_status"] == "success"
    assert deepagent["judge_engine"] == "deepagent"
    assert dsh["overall_status"] == "partial"
    assert reports.read_judge_report(run, "other") == {"status": "missing", "engine": "other"}


def test_old_run_without_manifest_still_reads_root_report(tmp_path):
    run = _write_run(tmp_path)
    (run / "agent_judge.json").write_text(
        json.dumps({"overall_status": "partial", "completion_score": 40}), encoding="utf-8"
    )

    manifest = reports.list_judge_reports(run)
    assert manifest["active_engine"] == "deepagent"
    assert [entry["engine"] for entry in manifest["reports"]] == ["deepagent"]
    report = reports.read_judge_report(run, "deepagent")
    assert report["overall_status"] == "partial"
    assert report["judge_engine"] == "deepagent"
    # No archive is fabricated for a legacy run.
    assert not (run / "judges").exists()


def test_run_without_any_report_lists_nothing(tmp_path):
    run = _write_run(tmp_path)
    assert reports.list_judge_reports(run) == {"version": 1, "active_engine": "", "reports": []}
    assert reports.compare_judge_reports(run)["engines"] == []


def test_set_active_judge_report_restores_archived_root_files(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")
    _publish(run, "dsh", "failed", 25, published_at="2026-10-13T00:01:00+00:00")

    manifest = reports.set_active_judge_report(run, "deepagent", published_at="2026-10-13T00:02:00+00:00")

    assert manifest["active_engine"] == "deepagent"
    root = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    assert root["judge"]["engine"] == "deepagent"
    assert root["overall_status"] == "success"
    assert "success" in (run / "agent_judge.md").read_text(encoding="utf-8")
    assert json.loads((run / "chart_data.json").read_text(encoding="utf-8"))["dimension_scores"]
    # Both archives survive the switch.
    assert reports.available_judge_engines(run) == ["deepagent", "dsh"]


def test_set_active_judge_report_rejects_unknown_or_incomplete_engine(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")

    with pytest.raises(ValueError):
        reports.set_active_judge_report(run, "..")
    with pytest.raises(FileNotFoundError):
        reports.set_active_judge_report(run, "dsh")

    # A manifest entry whose files vanished must not silently truncate the root report.
    (run / "judges" / "deepagent" / "agent_judge.md").unlink()
    with pytest.raises(FileNotFoundError):
        reports.set_active_judge_report(run, "deepagent")
    assert json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))["judge"]["engine"] == "deepagent"


# -- engine id safety --------------------------------------------------


@pytest.mark.parametrize("value", ["", "..", "a/b", "a\\b", ".hidden", "x" * 80, "deep agent", "日文"])
def test_normalize_engine_id_rejects_unsafe_ids(value):
    with pytest.raises(ValueError):
        reports.normalize_engine_id(value)


@pytest.mark.parametrize("value", ["DeepAgent", " DSH ", "dsh"])
def test_normalize_engine_id_accepts_registered_ids(value):
    assert reports.normalize_engine_id(value) in {"deepagent", "dsh"}


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/etc/passwd",
        "../x/agent_judge.json",
        "judges/../agent_judge.json",
        "judges/a/b/agent_judge.json",
        "judges/deepagent/evil.txt",
        "judges/deepagent/../../agent_judge.json",
        "agents\\judges\\deepagent\\agent_judge.json",
    ],
)
def test_resolve_report_path_rejects_traversal(tmp_path, value):
    with pytest.raises(ValueError):
        reports.resolve_report_path(tmp_path, value)


def test_resolve_report_path_accepts_root_and_archive(tmp_path):
    assert reports.resolve_report_path(tmp_path, "agent_judge.json") == tmp_path / "agent_judge.json"
    assert (
        reports.resolve_report_path(tmp_path, "judges/dsh/chart_data.json")
        == tmp_path / "judges" / "dsh" / "chart_data.json"
    )


# -- comparison --------------------------------------------------------


def test_compare_distinguishes_unknown_from_fail(tmp_path):
    run = _write_run(tmp_path)
    _publish(
        run,
        "deepagent",
        "success",
        90,
        published_at="2026-10-13T00:00:00+00:00",
        findings=[
            {
                "severity": "high",
                "category": "tool",
                "title": "unverified claim",
                "type": "fact",
                "evidence": [],
                "recommendation": "",
            }
        ],
    )
    _publish(run, "dsh", "partial", 50, published_at="2026-10-13T00:01:00+00:00")

    comparison = reports.compare_judge_reports(run)

    assert comparison["engines"] == ["deepagent", "dsh"]
    assert comparison["overall_status"]["by_engine"] == {"deepagent": "success", "dsh": "partial"}
    assert comparison["overall_status"]["disagreement"] is True
    assert comparison["completion_score"]["spread"] == 40
    # The unsupported fact was downgraded by evidence validation, never kept as fact.
    assert comparison["deterministic_findings"]["deepagent"]["count"] == 0
    assert comparison["deterministic_findings"]["deepagent"]["findings"] == []
    unknown = [row for row in comparison["dimensions"].values() if "deepagent" in row["unknown_engines"]]
    fail = [row for row in comparison["dimensions"].values() if row["status"]["deepagent"] == "fail"]
    assert unknown, "dimensions without evidence stay unknown"
    assert all(row["status"]["deepagent"] != "fail" for row in unknown)


def test_compare_keeps_deterministic_findings_visible_for_passing_judges(tmp_path):
    # Both semantic judges claim success; the requested .pptx is absent from the
    # run bundle. Inferred-missing is unverifiable rather than a failure, so the
    # reports keep their scores — but the deterministic delivery finding must
    # stay visible in the comparison instead of being hidden by the pass.
    run = _complete_ppt_run(tmp_path)
    _publish(run, "deepagent", "success", 95, published_at="2026-10-13T00:00:00+00:00")
    _publish(run, "dsh", "success", 92, published_at="2026-10-13T00:01:00+00:00")

    comparison = reports.compare_judge_reports(run)

    assert comparison["overall_status"]["by_engine"] == {"deepagent": "success", "dsh": "success"}
    assert comparison["completion_score"]["by_engine"] == {"deepagent": 95, "dsh": 92}
    for engine in ("deepagent", "dsh"):
        findings = comparison["deterministic_findings"][engine]["findings"]
        assert any(finding["category"] == "delivery" for finding in findings)


def test_compare_marks_dimension_disagreement_without_choosing_a_winner(tmp_path):
    run = _write_run(tmp_path)
    evidence = [{"source": "summary.json", "quote": "s1"}]
    _publish(
        run,
        "deepagent",
        "success",
        90,
        published_at="2026-10-13T00:00:00+00:00",
        findings=[
            {
                "severity": "low",
                "category": "tool",
                "title": "intent ok",
                "type": "fact",
                "evidence": evidence,
                "recommendation": "",
            }
        ],
    )
    _publish(run, "dsh", "failed", 20, published_at="2026-10-13T00:01:00+00:00")

    comparison = reports.compare_judge_reports(run)
    row = comparison["dimensions"]["evidence_citation"]

    assert row["status"]["deepagent"] != row["status"]["dsh"]
    assert row["disagreement"] is True
    assert comparison["active_engine"] == "dsh"
    assert "winner" not in json.dumps(comparison)


def test_compare_reliability_and_fact_counts_are_per_engine(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "partial", 60, published_at="2026-10-13T00:00:00+00:00")
    _publish(run, "dsh", "partial", 58, published_at="2026-10-13T00:01:00+00:00")

    comparison = reports.compare_judge_reports(run)

    assert set(comparison["reliability"]) == {"deepagent", "dsh"}
    # Deterministic dimension chains contribute validated evidence references
    # even when the judge proposal itself carried none.
    assert comparison["reliability"]["deepagent"]["total_evidence_count"] > 0
    assert comparison["judge_finding_counts"]["deepagent"]["total"] == 0
    assert set(comparison["fact_inference_mismatch"]) == {"deepagent", "dsh"}


# -- concurrency and atomic publish ------------------------------------


def test_concurrent_publishes_keep_both_reports_and_one_manifest(tmp_path):
    run = _write_run(tmp_path)
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(engine: str, offset: str) -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(5):
                _publish(
                    run,
                    engine,
                    "partial",
                    50 + index,
                    published_at="2026-10-13T00:0%s:%02d+00:00" % (offset, index),
                )
        except BaseException as exc:  # noqa: BLE001 - surfaced through the assert below
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("deepagent", "0")),
        threading.Thread(target=worker, args=("dsh", "1")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert reports.available_judge_engines(run) == ["deepagent", "dsh"]
    manifest = json.loads((run / "judges" / "manifest.json").read_text(encoding="utf-8"))
    assert [entry["engine"] for entry in manifest["reports"]] == ["deepagent", "dsh"]
    assert manifest["active_engine"] in {"deepagent", "dsh"}
    for engine in ("deepagent", "dsh"):
        data = json.loads((run / "judges" / engine / "agent_judge.json").read_text(encoding="utf-8"))
        assert normalize_judge_report(data)["dimensions"]
    # The lock leaves no stale files behind.
    assert sorted(path.name for path in (run / "judges").glob(".*")) == []


def test_compare_skips_a_listed_report_whose_file_vanished(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")
    _publish(run, "dsh", "partial", 50, published_at="2026-10-13T00:01:00+00:00")
    (run / "judges" / "dsh" / "agent_judge.json").unlink()

    comparison = reports.compare_judge_reports(run)

    assert comparison["engines"] == ["deepagent"]
    assert reports.read_judge_report(run, "dsh") == {"status": "missing", "engine": "dsh"}


def test_manifest_timestamp_never_goes_backwards(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "dsh", "partial", 50, published_at="2026-10-13T05:00:00+00:00")
    # An out-of-order writer must not make the older archive look newer.
    _publish(run, "deepagent", "partial", 50, published_at="2026-10-13T01:00:00+00:00")

    manifest = reports.read_judge_manifest(run)
    by_engine = {entry["engine"]: entry["published_at"] for entry in manifest["reports"]}
    assert by_engine["deepagent"] == "2026-10-13T05:00:00+00:00"
    assert reports.read_judge_report(run, "deepagent")["overall_status"] == "partial"


def test_unsettled_manifest_is_retried_then_reported(tmp_path, monkeypatch):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")

    # Simulate a peer that wrote its report files but has not published the manifest yet.
    manifest_file = run / "judges" / "manifest.json"
    monkeypatch.setattr(reports.time, "sleep", lambda _seconds: None)
    import os

    os.utime(manifest_file, (1_700_000_000, 1_700_000_000))
    for path in (run / "judges" / "deepagent").iterdir():
        os.utime(path, (1_700_000_060, 1_700_000_060))

    with pytest.raises(reports.JudgeReportSnapshotUnstable):
        reports.read_judge_manifest(run)
    with pytest.raises(reports.JudgeReportSnapshotUnstable):
        reports.compare_judge_reports(run)
    # Once the slow peer has published, the snapshot settles again.
    os.utime(manifest_file, (1_700_000_120, 1_700_000_120))
    assert reports.read_judge_manifest(run)["active_engine"] == "deepagent"


def test_corrupted_manifest_degrades_to_legacy_root_report(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")
    (run / "judges" / "manifest.json").write_text("{not json", encoding="utf-8")

    manifest = reports.list_judge_reports(run)
    assert [entry["engine"] for entry in manifest["reports"]] == ["deepagent"]
    assert reports.read_judge_report(run, "deepagent")["overall_status"] == "success"


def test_manifest_active_engine_falls_back_to_a_listed_report(tmp_path):
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")
    manifest = json.loads((run / "judges" / "manifest.json").read_text(encoding="utf-8"))
    manifest["active_engine"] = "removed-engine"
    (run / "judges" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    assert reports.read_judge_manifest(run)["active_engine"] == "deepagent"


# -- backward compatibility --------------------------------------------


def test_write_judge_report_without_engine_keeps_single_report_layout(tmp_path):
    run = _write_run(tmp_path)
    paths = write_judge_report(
        str(run),
        {
            "overall_status": "partial",
            "completion_score": 50,
            "confidence": "medium",
            "summary": "legacy",
            "dimensions": full_judge_dimensions(),
        },
    )

    assert Path(paths["json_path"]) == run / "agent_judge.json"
    assert not (run / "judges").exists()
    assert reports.read_judge_report(run, "deepagent")["overall_status"] == "partial"


def test_publish_accepts_a_report_written_by_the_shared_pipeline(tmp_path):
    run = _write_run(tmp_path)
    paths = reports.publish_judge_report(
        run,
        normalize_judge_report({"overall_status": "success", "completion_score": 70, "summary": "direct"}),
        engine="dsh",
        markdown="# Auditrail Agent Judge\n",
        charts={"dimension_scores": []},
        published_at="2026-10-13T00:00:00+00:00",
    )

    assert Path(paths["json_path"]).exists()
    assert Path(paths["archive_json_path"]) == run / "judges" / "dsh" / "agent_judge.json"
    assert reports.read_judge_report(run, "dsh")["overall_status"] == "success"


def test_judge_archive_does_not_count_as_a_produced_deliverable(tmp_path):
    # A run that asks for a .md deliverable must not "produce" one just because
    # a judge archive happens to contain agent_judge.md.
    run = tmp_path / "runs" / "md-run"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(
        json.dumps({"session_id": "s1", "title": "请输出 markdown 报告"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "audit.json").write_text(json.dumps({"tool_calls": [], "findings": [], "phases": []}), encoding="utf-8")
    (run / "trajectory.jsonl").write_text(
        json.dumps({"index": 0, "role": "user", "content": "请输出 markdown 报告"}, ensure_ascii=False) + "\n"
        + json.dumps({"index": 1, "role": "assistant", "content": "完成"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    _publish(run, "deepagent", "success", 95, published_at="2026-10-13T00:00:00+00:00")

    archived = json.loads((run / "judges" / "deepagent" / "agent_judge.json").read_text(encoding="utf-8"))
    assert archived["metrics"]["produced_deliverables"] == []
    assert archived["metrics"]["missing_deliverables"] == [".md"]
    # inferred-missing is unverifiable, not a failure: no cap, no forced failed
    assert archived["completion_score"] == 95
    assert archived["overall_status"] == "success"


def test_compare_preserves_null_completion_scores(tmp_path):
    # A missing completion_score is null ("no data"), never 0 ("did poorly");
    # spread/disagreement are computed only over engines that actually scored.
    run = _write_run(tmp_path)
    _publish(run, "deepagent", "success", 90, published_at="2026-10-13T00:00:00+00:00")
    _publish(run, "dsh", "unknown", None, published_at="2026-10-13T00:01:00+00:00")

    comparison = reports.compare_judge_reports(run)

    assert comparison["completion_score"]["by_engine"] == {"deepagent": 90, "dsh": None}
    assert comparison["completion_score"]["spread"] == 0
    assert comparison["completion_score"]["disagreement"] is False
