import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auditrail.agents.schemas import DIMENSION_DEFINITIONS, missing_judge_dimensions
from auditrail.agents.tools import write_judge_report
from auditrail.dsh_headless import DshProcessOutput
from auditrail.judges.base import (
    JudgeExecutionError,
    JudgeRequest,
    JudgeUnavailableError,
    describe_engines,
    resolve_judge_engine,
)
from auditrail.judges.deepagent_engine import DeepAgentJudgeEngine
from auditrail.judges.dsh_engine import DshJudgeEngine
from auditrail.judges.prompting import build_dsh_judge_prompt
from auditrail.judges.proposals import JudgeProposalError, parse_judge_proposal
from backend.app.config import Settings, build_dsh_judge_settings
from backend.app.main import app
from tests.conftest import full_judge_dimensions


JUDGE_PROFILE = "agentloop-judge"
EVALUAND_PROFILE = "headless"
SESSION_ID = "session-12345678-1234-4234-8234-123456789abc"


@pytest.fixture
def base(tmp_path):
    # macOS tmp paths are symlinked (/var -> /private/var); resolve once so the
    # configured roots match DshRuntimeConfig's resolved workspace comparison.
    return Path(tmp_path).resolve()


def _proposal(**overrides):
    proposal = {
        "overall_status": "partial",
        "completion_score": 70,
        "confidence": "high",
        "summary": "语义分析结论",
        "findings": [
            {
                "severity": "medium",
                "category": "tool",
                "title": "工具步骤可更早验证",
                "type": "inference",
                "evidence": [],
                "recommendation": "在写文件前先列出交付物。",
            }
        ],
        "metrics": {"message_count": 3, "tool_call_count": 1, "tool_error_count": 0},
        "charts": {},
        # 新契约：proposal 必须提交全部 13 个维度（证据不足的用 unknown/judge）。
        "dimensions": full_judge_dimensions(),
        "judge_reliability": {},
    }
    proposal.update(overrides)
    return proposal


def _judge_stdout(proposal, session_id=SESSION_ID, workspace=None):
    # DSH emits the turn_end status before the lossless final event. A dict
    # proposal is serialized; a string is used verbatim as the raw final text.
    final_text = proposal if isinstance(proposal, str) else json.dumps(proposal, ensure_ascii=False)
    return [
        json.dumps({"type": "session", "sessionId": session_id, "cwd": str(workspace)}),
        json.dumps({"type": "status", "phase": "turn_start", "turn": 1}),
        json.dumps({"type": "text", "text": "judging"}),
        json.dumps({"type": "status", "phase": "turn_end", "turn": 1, "reason": "completed"}),
        json.dumps({"type": "final", "text": final_text}),
    ]


def _fake_runner_factory(proposal, captured, session_id=SESSION_ID):
    def runner(argv, *, cwd, timeout_seconds, cancel_event, max_stdout_bytes, max_stderr_bytes, on_stdout_line):
        captured.append({"argv": list(argv), "cwd": str(cwd), "timeout": timeout_seconds})
        lines = _judge_stdout(proposal, session_id=session_id, workspace=cwd)
        # Return the lines so the real DshHeadlessClient normalizes them; the
        # streaming callback path is covered by tests/test_dsh_headless.py.
        return DshProcessOutput(tuple(line.encode("utf-8") + b"\n" for line in lines), b"", 0)

    return runner


def _write_run(runs_dir: Path, name="run-1") -> Path:
    run = Path(runs_dir) / name
    run.mkdir(parents=True, exist_ok=True)
    (run / "session.json").write_text(
        json.dumps({"session": {"session_id": "evaluand-1", "messages": []}}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "summary.json").write_text(
        json.dumps(
            {
                "session_id": "evaluand-1",
                "title": "生成报告",
                "model": "deepseek-v4-flash",
                "server_host": "xuanjian-agent",
                "message_count": 3,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (run / "expectations.json").write_text(
        json.dumps({"acceptance_criteria": "必须产出 report.md"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (run / "source.json").write_text(json.dumps({"source_type": "dsh"}), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps(
            {
                "tool_calls": [
                    {"index": 1, "tool": "write_file", "status": "ok", "args_preview": "report.md"}
                ],
                "findings": [],
                "phases": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    rows = [
        {"index": 0, "role": "user", "content": "生成 report.md"},
        {
            "index": 1,
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1", "name": "write_file", "arguments": {"path": "report.md"}}],
        },
        {"index": 2, "role": "tool", "tool_call_id": "call-1", "tool_name": "write_file", "content": "ok"},
        {"index": 3, "role": "assistant", "content": "已生成 report.md"},
    ]
    (run / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )
    (run / "report.md").write_text("# report\n", encoding="utf-8")
    return run


def _settings(tmp_path: Path, **overrides) -> Settings:
    base_dir = Path(tmp_path)
    root = base_dir / "allowed"
    judge_workspace = root / "judge-workspace"
    root.mkdir(parents=True, exist_ok=True)
    judge_workspace.mkdir(parents=True, exist_ok=True)
    (root / "project").mkdir(parents=True, exist_ok=True)
    values = dict(
        runs_dir=base_dir / "runs",
        dsh_home=base_dir / "dsh",
        dsh_runtime_enabled=True,
        dsh_binary=base_dir / "bin" / "dsh",
        dsh_allowed_profiles=(EVALUAND_PROFILE,),
        dsh_allowed_workspace_roots=(root,),
        dsh_default_timeout_seconds=30,
        dsh_max_timeout_seconds=60,
        dsh_max_concurrency=1,
        dsh_judge_profile=JUDGE_PROFILE,
        dsh_judge_workspace=judge_workspace,
    )
    values.update(overrides)
    return Settings(**values)


# -- strict proposal parsing -------------------------------------------


def test_parse_judge_proposal_accepts_raw_json_object():
    proposal = _proposal()
    assert parse_judge_proposal(json.dumps(proposal)) == proposal
    assert parse_judge_proposal(proposal) == proposal


@pytest.mark.parametrize(
    "raw, message",
    [
        ("", "no proposal"),
        ("not json", "not valid JSON"),
        ("[1, 2]", "must be a JSON object"),
        ('```json\n{"overall_status": "success"}\n```', "Markdown code fence"),
        (json.dumps({"summary": "x", "findings": []}), "overall_status"),
        (json.dumps({"overall_status": "success"}), "not report-shaped"),
    ],
)
def test_parse_judge_proposal_rejects_malformed_or_degraded_bodies(raw, message):
    with pytest.raises(JudgeProposalError, match=message):
        parse_judge_proposal(raw)


def test_parse_judge_proposal_rejects_oversized_body():
    raw = json.dumps(_proposal(summary="x" * 4096))
    with pytest.raises(JudgeProposalError, match="exceeded"):
        parse_judge_proposal(raw, max_bytes=1024)


# -- bounded judge evidence --------------------------------------------


def test_dsh_judge_prompt_embeds_bounded_run_evidence(base):
    run = _write_run(base / "runs")
    prompt = build_dsh_judge_prompt(run)

    assert "必须产出 report.md" in prompt
    assert "evaluand-1" in prompt
    assert "write_file" in prompt
    assert "Do not wrap the JSON in a Markdown code fence" in prompt


def test_dsh_judge_prompt_caps_trajectory_rows(base):
    run = _write_run(base / "runs")
    rows = [{"index": index, "role": "user", "content": "row-%s" % index} for index in range(500)]
    (run / "trajectory.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n",
        encoding="utf-8",
    )
    prompt = build_dsh_judge_prompt(run)

    assert "row-399" in prompt
    assert "row-400" not in prompt
    assert '"truncated": true' in prompt


# -- engine registry ---------------------------------------------------


def test_registry_lists_deepagent_and_dsh_and_defaults_to_deepagent(monkeypatch):
    monkeypatch.delenv("AUDITRAIL_JUDGE_ENGINE", raising=False)
    catalog = describe_engines(None)

    assert catalog["default_engine"] == "deepagent"
    assert catalog["selected"] == "deepagent"
    assert {item["id"] for item in catalog["engines"]} == {"deepagent", "dsh"}
    assert resolve_judge_engine(None).id == "deepagent"
    assert resolve_judge_engine("dsh").id == "dsh"


def test_registry_rejects_unknown_engine():
    with pytest.raises(JudgeUnavailableError):
        resolve_judge_engine("browser-supplied")


def test_deepagent_engine_preserves_prompt_thread_id_and_proposal(base):
    run = _write_run(base / "runs")
    seen = {}

    async def fake_invoke(messages, config=None):
        seen["messages"] = messages
        seen["config"] = config
        return {
            "overall_status": "success",
            "completion_score": 88,
            "confidence": "high",
            "summary": "ok",
            "dimensions": full_judge_dimensions(),
        }

    result = DeepAgentJudgeEngine(fake_invoke).run(JudgeRequest(run_dir=run, run_id="run-1"))

    assert result.engine == "deepagent"
    assert result.proposal["completion_score"] == 88
    assert seen["config"]["configurable"]["thread_id"] == "auditrail-judge-run-1"
    assert "run_path: %s" % run in seen["messages"][0]["content"]
    assert result.metadata["runtime"] == "deepagent"


def test_deepagent_engine_maps_missing_dependency_to_unavailable(base):
    run = _write_run(base / "runs")

    async def fake_invoke(messages, config=None):
        raise RuntimeError("DeepAgent analysis requires installing the optional agent dependencies")

    with pytest.raises(JudgeUnavailableError):
        DeepAgentJudgeEngine(fake_invoke).run(JudgeRequest(run_dir=run, run_id="run-1"))


def test_deepagent_engine_rejects_non_report_response(tmp_path):
    from auditrail.judges.base import JudgeExecutionError

    run = _write_run(tmp_path / "runs")

    async def fake_invoke(messages, config=None):
        return "not a report object"

    with pytest.raises(JudgeExecutionError, match="instead of a report object"):
        DeepAgentJudgeEngine(fake_invoke).run(JudgeRequest(run_dir=run, run_id="run-1"))


def test_normalize_judge_report_ignores_malformed_metadata(tmp_path):
    from auditrail.agents.schemas import normalize_judge_report

    for value in ("dsh", 7, None, [1, 2], {"nested": {"a": 1}}):
        normalized = normalize_judge_report(
            {"overall_status": "success", "completion_score": 90, "summary": "x", "judge": value}
        )
        assert "judge" not in normalized


# -- DSH engine isolation ----------------------------------------------


def test_dsh_engine_available_only_with_dedicated_profile_and_workspace(base):
    run = _write_run(base / "runs")

    assert "disabled" in DshJudgeEngine(Settings()).available()
    assert "DSH_JUDGE_PROFILE" in DshJudgeEngine(_settings(base, dsh_judge_profile="")).available()
    assert "DSH_JUDGE_WORKSPACE" in DshJudgeEngine(_settings(base, dsh_judge_workspace=None)).available()
    assert DshJudgeEngine(_settings(base)).available() is None

    build_dsh_judge_settings(_settings(base))
    with pytest.raises(ValueError, match="differ"):
        build_dsh_judge_settings(_settings(base, dsh_judge_profile=EVALUAND_PROFILE))


def test_dsh_engine_rejects_judge_profile_shared_with_evaluand(base):
    run = _write_run(base / "runs")
    engine = DshJudgeEngine(_settings(base, dsh_judge_profile=EVALUAND_PROFILE))

    assert "differ" in engine.available()
    with pytest.raises(JudgeUnavailableError, match="differ"):
        engine.run(JudgeRequest(run_dir=run, run_id="run-1"))


def test_dsh_engine_rejects_workspace_outside_allowed_roots(base):
    run = _write_run(base / "runs")
    outside = base / "outside"
    outside.mkdir()
    engine = DshJudgeEngine(_settings(base, dsh_judge_workspace=outside))

    assert "outside the allowed roots" in engine.available()
    with pytest.raises(ValueError, match="outside the allowed roots"):
        build_dsh_judge_settings(_settings(base, dsh_judge_workspace=outside))


def test_dsh_engine_rejects_reused_judge_workspace(base):
    run = _write_run(base / "runs")
    engine = DshJudgeEngine(_settings(base, dsh_judge_workspace=run))

    with pytest.raises(JudgeUnavailableError, match="differ from the evaluated workspace"):
        engine.run(JudgeRequest(run_dir=run, run_id="run-1"))


def test_dsh_engine_uses_dedicated_profile_and_workspace(base):
    run = _write_run(base / "runs")
    settings = _settings(base)
    captured = []
    engine = DshJudgeEngine(settings, runner=_fake_runner_factory(_proposal(), captured))

    result = engine.run(JudgeRequest(run_dir=run, run_id="run-1"))

    assert result.engine == "dsh"
    assert captured[0]["argv"][:3] == [str(settings.dsh_binary), "--profile", JUDGE_PROFILE]
    assert captured[0]["cwd"] == str(settings.dsh_judge_workspace)
    assert result.metadata["profile"] == JUDGE_PROFILE
    assert result.metadata["workspace"] == str(settings.dsh_judge_workspace)
    assert result.metadata["turn_end_reason"] == "completed"


def test_dsh_engine_records_recording_model_and_version(base):
    from tests.test_backend_dsh_chat import _write_durable_session

    run = _write_run(base / "runs")
    settings = _settings(base)
    workspace = settings.dsh_judge_workspace
    _write_durable_session(settings, workspace, session_id=SESSION_ID)
    _append_request_header(settings, workspace, SESSION_ID, model="dsh-judge-model")

    captured = []
    engine = DshJudgeEngine(settings, runner=_fake_runner_factory(_proposal(), captured, session_id=SESSION_ID))
    result = engine.run(JudgeRequest(run_dir=run, run_id="run-1"))

    assert result.metadata["model"] == "dsh-judge-model"
    assert result.metadata["session_id"] == SESSION_ID


def _append_request_header(settings, workspace, session_id, model):
    import zstandard

    path = settings.dsh_home / "sessions" / "--allowed-project--" / session_id / "session.jsonl.zstd"
    events = [
        {
            "type": "session",
            "version": 0,
            "id": session_id,
            "createdAt": 1787000000000,
            "cwd": str(workspace),
            "agentPreset": JUDGE_PROFILE,
        },
        {"type": "request/header", "seq": 1, "data": {"header": {"config": {"model": model}}}},
    ]
    payload = "\n".join(json.dumps(event) for event in events).encode()
    path.write_bytes(zstandard.ZstdCompressor().compress(payload))


@pytest.mark.parametrize(
    "proposal, match",
    [
        ('```json\n{"overall_status": "success", "findings": []}\n```', "fence"),
        (json.dumps({"summary": "no status"}), "overall_status"),
    ],
)
def test_dsh_engine_rejects_non_strict_proposals(base, proposal, match):
    from auditrail.judges.base import JudgeExecutionError

    run = _write_run(base / "runs")
    settings = _settings(base)
    captured = []
    engine = DshJudgeEngine(settings, runner=_fake_runner_factory(proposal, captured))

    with pytest.raises(JudgeExecutionError, match=match):
        engine.run(JudgeRequest(run_dir=run, run_id="run-1"))


def test_dsh_engine_maps_timeout_to_explicit_failure(base):
    from auditrail.judges.base import JudgeExecutionError

    run = _write_run(base / "runs")
    settings = _settings(base)

    def runner(argv, *, cwd, timeout_seconds, cancel_event, max_stdout_bytes, max_stderr_bytes, on_stdout_line):
        return DshProcessOutput((), b"", None, timed_out=True)

    with pytest.raises(JudgeExecutionError, match="timeout"):
        DshJudgeEngine(settings, runner=runner).run(JudgeRequest(run_dir=run, run_id="run-1"))


def test_dsh_engine_maps_missing_binary_to_unavailable(base):
    run = _write_run(base / "runs")
    settings = _settings(base)

    def runner(argv, *, cwd, timeout_seconds, cancel_event, max_stdout_bytes, max_stderr_bytes, on_stdout_line):
        raise FileNotFoundError("no such binary")

    with pytest.raises(JudgeUnavailableError, match="failed to start"):
        DshJudgeEngine(settings, runner=runner).run(JudgeRequest(run_dir=run, run_id="run-1"))


def test_dsh_engine_prompt_omits_argv_injection_and_keeps_message_whole(base):
    run = _write_run(base / "runs")
    settings = _settings(base)
    captured = []
    engine = DshJudgeEngine(settings, runner=_fake_runner_factory(_proposal(), captured))

    engine.run(JudgeRequest(run_dir=run, run_id="run-1"))

    argv = captured[0]["argv"]
    assert argv[-1].startswith("Analyze this Auditrail run")
    assert "--patch" not in argv
    assert "--profile" in argv
    assert argv[argv.index("--profile") + 1] == JUDGE_PROFILE


# -- report writing keeps deterministic authority ----------------------


def test_dsh_engine_proposal_is_normalized_and_capped_by_shared_pipeline(base):
    run = _write_run(base / "runs")
    settings = _settings(base)
    captured = []
    engine = DshJudgeEngine(settings, runner=_fake_runner_factory(_proposal(), captured))

    result = engine.run(JudgeRequest(run_dir=run, run_id="run-1"))
    paths = write_judge_report(str(run), result.proposal, judge_metadata={**result.metadata, "engine": result.engine})
    report = json.loads(Path(paths["json_path"]).read_text(encoding="utf-8"))

    assert report["judge"]["engine"] == "dsh"
    assert report["judge"]["profile"] == JUDGE_PROFILE
    assert Path(paths["markdown_path"]).exists()
    assert Path(paths["chart_path"]).exists()
    # Every dimension key survives normalization.
    assert len(report["dimensions"]) == 13
    # Deterministic dimension logic still owns closure and citation scoring,
    # now merged with the judge's own submissions.
    assert report["dimensions"]["execution_closure"]["source"] == "deterministic+judge"
    assert report["dimensions"]["evidence_citation"]["source"] == "deterministic+judge"
    assert report["dimensions"]["tool_selection"]["status"] == "unknown"
    assert report["dimensions"]["error_recovery"]["status"] == "unknown"
    assert report["judge_reliability"]["total_evidence_count"] == 0


def test_malformed_dsh_proposal_cannot_weaken_deterministic_failure(base):
    run = _write_run(base / "runs")
    (run / "report.md").unlink()
    # An inferred-missing deliverable is only unverifiable; the deterministic
    # failure this test needs must come from an authoritative source. Declare
    # the artifact missing via evaluation.json (required artifact check).
    from auditrail.evaluation import make_artifact_check, new_envelope, write_evaluation

    envelope = new_envelope("completed")
    envelope["artifact_checks"] = [
        make_artifact_check("report.md", required=True, status="fail", exists=False, valid=False)
    ]
    write_evaluation(run, envelope)
    settings = _settings(base)
    captured = []
    engine = DshJudgeEngine(
        settings,
        runner=_fake_runner_factory(
            _proposal(overall_status="success", completion_score=100),
            captured,
        ),
    )

    result = engine.run(JudgeRequest(run_dir=run, run_id="run-1"))
    paths = write_judge_report(str(run), result.proposal, judge_metadata={**result.metadata, "engine": result.engine})
    report = json.loads(Path(paths["json_path"]).read_text(encoding="utf-8"))

    assert report["overall_status"] == "failed"
    assert report["completion_score"] <= 40
    assert any(item["category"] == "delivery" for item in report["findings"])


# -- engine metadata compatibility -------------------------------------


def test_normalize_judge_report_preserves_engine_metadata_and_old_reports_still_load(base):
    from auditrail.agents.schemas import normalize_judge_report

    legacy = normalize_judge_report(
        {"overall_status": "success", "completion_score": 90, "confidence": "high", "summary": "legacy"}
    )
    assert "judge" not in legacy

    with_metadata = normalize_judge_report(
        {
            "overall_status": "success",
            "completion_score": 90,
            "summary": "with provenance",
            "judge": {"engine": "DSH", "runtime": "dsh", "exit_code": 0, "notes": None, "nested": {"a": 1}},
        }
    )
    assert with_metadata["judge"]["engine"] == "dsh"
    assert with_metadata["judge"]["exit_code"] == 0
    assert "nested" not in with_metadata["judge"]
    assert "notes" not in with_metadata["judge"]


# -- backend HTTP contract ---------------------------------------------


def _patch_settings(monkeypatch, settings):
    monkeypatch.setattr("backend.app.routes.judge.get_settings", lambda: settings)


def test_backend_lists_judge_engines(base, monkeypatch):
    settings = _settings(base)
    _patch_settings(monkeypatch, settings)
    client = TestClient(app)

    payload = client.get("/api/judge/engines").json()

    assert payload["default_engine"] == "deepagent"
    engines = {item["id"]: item for item in payload["engines"]}
    assert engines["deepagent"]["available"] is True
    assert engines["dsh"]["available"] is True


def test_backend_judge_rejects_unknown_engine(base, monkeypatch):
    run = _write_run(base / "runs")
    settings = _settings(base, runs_dir=run.parent)
    _patch_settings(monkeypatch, settings)
    client = TestClient(app)

    response = client.post("/api/runs/run-1/judge", json={"engine": "browser-supplied"})

    assert response.status_code == 400
    assert not (run / "agent_judge.json").exists()


def test_backend_dsh_judge_returns_503_when_not_configured(base, monkeypatch):
    run = _write_run(base / "runs")
    settings = _settings(base, runs_dir=run.parent, dsh_judge_profile="")
    _patch_settings(monkeypatch, settings)
    client = TestClient(app)

    response = client.post("/api/runs/run-1/judge", json={"engine": "dsh"})

    assert response.status_code == 503
    assert not (run / "agent_judge.json").exists()


def test_backend_dsh_judge_writes_report_with_engine_metadata(base, monkeypatch):
    run = _write_run(base / "runs")
    settings = _settings(base, runs_dir=run.parent)
    _patch_settings(monkeypatch, settings)
    captured = []
    monkeypatch.setattr(
        "auditrail.judges.dsh_engine.DshJudgeEngine",
        lambda _settings=None: DshJudgeEngine(settings, runner=_fake_runner_factory(_proposal(), captured)),
    )
    client = TestClient(app)

    response = client.post("/api/runs/run-1/judge", json={"engine": "dsh"})

    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "dsh"
    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    assert report["judge"]["engine"] == "dsh"
    assert report["judge"]["runtime"] == "dsh"
    assert report["summary"] == "语义分析结论"


def test_backend_dsh_judge_rejects_invalid_proposal_as_500(base, monkeypatch):
    run = _write_run(base / "runs")
    settings = _settings(base, runs_dir=run.parent)
    _patch_settings(monkeypatch, settings)
    captured = []
    monkeypatch.setattr(
        "auditrail.judges.dsh_engine.DshJudgeEngine",
        lambda _settings=None: DshJudgeEngine(settings, runner=_fake_runner_factory("not json", captured)),
    )
    client = TestClient(app)

    response = client.post("/api/runs/run-1/judge", json={"engine": "dsh"})

    assert response.status_code == 500
    assert not (run / "agent_judge.json").exists()


def test_backend_deepagent_judge_without_engine_field_still_works(base, monkeypatch):
    run = _write_run(base / "runs")
    settings = _settings(base, runs_dir=run.parent)
    _patch_settings(monkeypatch, settings)

    async def fake_invoke(messages, config=None):
        return {
            "overall_status": "success",
            "completion_score": 88,
            "confidence": "high",
            "summary": "ok",
            "dimensions": full_judge_dimensions(),
        }

    monkeypatch.setattr("auditrail.agents.agent.invoke_agent", fake_invoke)
    client = TestClient(app)

    response = client.post("/api/runs/run-1/judge")

    assert response.status_code == 200
    assert response.json()["engine"] == "deepagent"
    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    assert report["judge"]["engine"] == "deepagent"


def test_dsh_options_expose_judge_engine_catalog(base, monkeypatch):
    settings = _settings(base)
    monkeypatch.setattr("backend.app.routes.dsh.get_settings", lambda: settings)
    client = TestClient(app)

    payload = client.get("/api/dsh/options").json()

    assert payload["judge"]["default_engine"] == "deepagent"
    assert {item["id"] for item in payload["judge"]["engines"]} == {"deepagent", "dsh"}


# -- multi-judge storage through the shared pipeline -------------------


def test_dsh_and_deepagent_reports_survive_subsequent_judge_runs(base, monkeypatch):
    run = _write_run(base / "runs")
    settings = _settings(base, runs_dir=run.parent)
    _patch_settings(monkeypatch, settings)
    monkeypatch.setattr("backend.app.routes.runs.get_settings", lambda: settings)
    captured = []
    monkeypatch.setattr(
        "auditrail.judges.dsh_engine.DshJudgeEngine",
        lambda _settings=None: DshJudgeEngine(settings, runner=_fake_runner_factory(_proposal(), captured)),
    )

    async def fake_invoke(messages, config=None):
        return {
            "overall_status": "success",
            "completion_score": 88,
            "confidence": "high",
            "summary": "deepagent",
            "dimensions": full_judge_dimensions(),
        }

    monkeypatch.setattr("auditrail.agents.agent.invoke_agent", fake_invoke)
    client = TestClient(app)

    assert client.post("/api/runs/run-1/judge", json={"engine": "dsh"}).status_code == 200
    assert client.post("/api/runs/run-1/judge", json={"engine": "deepagent"}).status_code == 200
    # Running DeepAgent after DSH must not delete the DSH report.
    assert client.post("/api/runs/run-1/judge", json={"engine": "dsh"}).status_code == 200

    listed = client.get("/api/runs/run-1/judges").json()
    assert [entry["engine"] for entry in listed["reports"]] == ["deepagent", "dsh"]
    assert listed["active_engine"] == "dsh"

    dsh = client.get("/api/runs/run-1/judges/dsh").json()
    assert dsh["judge"]["engine"] == "dsh"
    assert dsh["judge"]["profile"] == JUDGE_PROFILE
    deepagent = client.get("/api/runs/run-1/judges/deepagent").json()
    assert deepagent["judge"]["engine"] == "deepagent"
    assert deepagent["overall_status"] == "success"

    # Every stored report came from the shared pipeline and can be re-normalized.
    from auditrail.agents.schemas import normalize_judge_report

    for engine in ("deepagent", "dsh"):
        data = json.loads((run / "judges" / engine / "agent_judge.json").read_text(encoding="utf-8"))
        assert len(normalize_judge_report(data)["dimensions"]) == 13
        assert (run / "judges" / engine / "agent_judge.md").exists()
        assert (run / "judges" / engine / "chart_data.json").exists()

    comparison = client.get("/api/runs/run-1/judges/compare").json()
    assert comparison["engines"] == ["deepagent", "dsh"]
    assert comparison["overall_status"]["disagreement"] is True
    # Deterministic dimensions stay deterministic for every engine, including the
    # ones engine proposals disagree about.
    assert comparison["dimensions"]["evidence_citation"]["status"]["deepagent"] in {"fail", "pass", "partial"}
    assert comparison["disagreements"]


# -- dimension completeness gate ---------------------------------------


def _incomplete_proposal(*missing_keys):
    """A syntactically valid strict-JSON proposal that omits dimension keys."""
    dimensions = full_judge_dimensions()
    for key in missing_keys:
        del dimensions[key]
    return _proposal(dimensions=dimensions)


def _sequenced_runner(proposals, captured):
    """Fake DSH runner returning a different proposal per invocation."""
    queue = list(proposals)

    def runner(argv, *, cwd, timeout_seconds, cancel_event, max_stdout_bytes, max_stderr_bytes, on_stdout_line):
        captured.append({"argv": list(argv), "cwd": str(cwd), "timeout": timeout_seconds})
        proposal = queue.pop(0) if queue else proposals[-1]
        lines = _judge_stdout(proposal, workspace=cwd)
        return DshProcessOutput(tuple(line.encode("utf-8") + b"\n" for line in lines), b"", 0)

    return runner


def test_parseable_dsh_proposal_with_missing_dimensions_is_not_publishable():
    # 「JSON 可解析且 report-shaped」不等于「报告完整」：缺维度的严格 JSON
    # 仍必须被识别为结构不完整。
    proposal = parse_judge_proposal(json.dumps(_proposal(dimensions={})))
    assert missing_judge_dimensions(proposal) == list(DIMENSION_DEFINITIONS)
    complete = parse_judge_proposal(json.dumps(_proposal(dimensions=full_judge_dimensions())))
    assert missing_judge_dimensions(complete) == []


def test_dsh_engine_repairs_incomplete_proposal_once(base):
    run = _write_run(base / "runs")
    settings = _settings(base)
    captured = []
    runner = _sequenced_runner([_incomplete_proposal("verification", "efficiency"), _proposal()], captured)
    engine = DshJudgeEngine(settings, runner=runner)

    result = engine.run(JudgeRequest(run_dir=run, run_id="run-1"))

    assert len(captured) == 2
    repair_message = captured[1]["argv"][-1]
    assert "verification" in repair_message
    assert "efficiency" in repair_message
    assert missing_judge_dimensions(result.proposal) == []


def test_dsh_engine_fails_when_repair_stays_incomplete(base):
    run = _write_run(base / "runs")
    settings = _settings(base)
    captured = []
    incomplete = _incomplete_proposal("verification", "efficiency")
    runner = _sequenced_runner([incomplete, incomplete], captured)
    engine = DshJudgeEngine(settings, runner=runner)

    with pytest.raises(JudgeExecutionError, match="verification"):
        engine.run(JudgeRequest(run_dir=run, run_id="run-1"))
    # 至多一次修复尝试，不会无限重试，也不写出任何报告文件。
    assert len(captured) == 2
    assert not (run / "agent_judge.json").exists()
    assert not (run / "judges").exists()


def test_deepagent_engine_repairs_incomplete_proposal_once(base):
    run = _write_run(base / "runs")
    calls = []

    async def fake_invoke(messages, config=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"overall_status": "partial", "completion_score": 60, "summary": "缺维度"}
        return {
            "overall_status": "partial",
            "completion_score": 60,
            "summary": "完整报告",
            "dimensions": full_judge_dimensions(),
        }

    result = DeepAgentJudgeEngine(fake_invoke).run(JudgeRequest(run_dir=run, run_id="run-1"))

    assert len(calls) == 2
    repair_prompt = calls[1][0]["content"]
    assert "intent_recognition" in repair_prompt
    assert "efficiency" in repair_prompt
    assert result.proposal["summary"] == "完整报告"
    assert missing_judge_dimensions(result.proposal) == []


def test_deepagent_engine_fails_when_repair_stays_incomplete(base):
    run = _write_run(base / "runs")
    calls = []

    async def fake_invoke(messages, config=None):
        calls.append(messages)
        return {"overall_status": "partial", "completion_score": 60, "summary": "缺维度"}

    with pytest.raises(JudgeExecutionError, match="intent_recognition"):
        DeepAgentJudgeEngine(fake_invoke).run(JudgeRequest(run_dir=run, run_id="run-1"))
    assert len(calls) == 2


def test_deepagent_engine_prefers_complete_tool_call_report(base):
    # DeepAgent 返回值里最后一条 assistant 消息是简略 JSON，但更早的
    # write_judge_report 工具调用提交了完整 13 维报告：必须选择完整候选。
    run = _write_run(base / "runs")
    full = {
        "overall_status": "partial",
        "completion_score": 55,
        "summary": "工具调用中的完整报告",
        "findings": [],
        "dimensions": full_judge_dimensions(),
    }
    thin = {"overall_status": "failed", "summary": "简略总结", "findings": []}

    async def fake_invoke(messages, config=None):
        return {
            "messages": [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"name": "write_judge_report", "args": {"run_path": str(run), "report": full}}
                    ],
                },
                {"role": "tool", "content": json.dumps({"json_path": "x"})},
                {"role": "assistant", "content": json.dumps(thin, ensure_ascii=False)},
            ]
        }

    result = DeepAgentJudgeEngine(fake_invoke).run(JudgeRequest(run_dir=run, run_id="run-1"))

    assert result.proposal["summary"] == "工具调用中的完整报告"
    assert missing_judge_dimensions(result.proposal) == []
