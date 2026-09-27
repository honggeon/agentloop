import json
from pathlib import Path

import pytest

from agentloop.cases import (
    evaluate_case,
    load_suite,
    render_report_markdown,
    run_case,
    run_suite,
    write_report,
)

SUITE_PATH = Path(__file__).resolve().parents[1] / "cases" / "dsh-cli.json"


def _case(**overrides):
    case = {
        "id": "C-1",
        "title": "probe case",
        "args": ["--flag"],
        "expect": {"exit_code": 0},
    }
    case.update(overrides)
    return case


def _outcome(**overrides):
    outcome = {"exit_code": 0, "stdout": "", "stderr": "", "timed_out": False, "duration_s": 0.01}
    outcome.update(overrides)
    return outcome


def _runner(outcome=None, recorder=None):
    def run(argv, cwd, timeout_s, env):
        if recorder is not None:
            recorder.append({"argv": list(argv), "cwd": str(cwd), "timeout_s": timeout_s, "env": dict(env)})
        return dict(outcome or _outcome())

    return run


def _suite(cases):
    return {"name": "probe", "binary": "probe-bin", "cases": cases}


# --- suite loading and validation -------------------------------------------------


def test_shipped_suite_loads_and_validates():
    suite = load_suite(SUITE_PATH)
    ids = [case["id"] for case in suite["cases"]]
    assert ids == sorted(set(ids), key=ids.index), "case ids must be unique"
    assert suite["binary"] == "dsh"
    assert len(suite["cases"]) >= 8


def test_load_suite_rejects_unknown_expect_key(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite([_case(expect={"exit_codee": 0})])), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown expect keys: exit_codee"):
        load_suite(path)


def test_load_suite_rejects_duplicate_case_id(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite([_case(), _case()])), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate case id"):
        load_suite(path)


def test_load_suite_rejects_missing_args(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite([_case(args=[])])), encoding="utf-8")
    with pytest.raises(ValueError, match="needs args"):
        load_suite(path)


def test_load_suite_rejects_xfail_without_defect(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite([_case(xfail=True)])), encoding="utf-8")
    with pytest.raises(ValueError, match="must name a defect"):
        load_suite(path)


def test_load_suite_rejects_non_object_env(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite([_case(env=["DSH_HOME=/x"])])), encoding="utf-8")
    with pytest.raises(ValueError, match="env must be an object"):
        load_suite(path)


def test_load_suite_rejects_invalid_json(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_suite(path)


def test_load_suite_rejects_missing_file(tmp_path):
    with pytest.raises(ValueError, match="suite file not found"):
        load_suite(tmp_path / "absent.json")


# --- expectation evaluation -------------------------------------------------------


def test_evaluate_exit_code_forms():
    assert evaluate_case(_case(expect={"exit_code": 3}), _outcome(exit_code=3))[0]["ok"] is True
    assert evaluate_case(_case(expect={"exit_code": {"not": 0}}), _outcome(exit_code=1))[0]["ok"] is True
    assert evaluate_case(_case(expect={"exit_code": {"not": 0}}), _outcome(exit_code=0))[0]["ok"] is False
    assert evaluate_case(_case(expect={"exit_code": {"in": [1, 2]}}), _outcome(exit_code=2))[0]["ok"] is True
    assert evaluate_case(_case(expect={"exit_code": {"in": [1, 2]}}), _outcome(exit_code=0))[0]["ok"] is False


def test_evaluate_text_assertions():
    case = _case(expect={"stdout_contains": ["alpha", "beta"]})
    assert evaluate_case(case, _outcome(stdout="alpha beta"))[0]["ok"] is True
    assert evaluate_case(case, _outcome(stdout="alpha"))[0]["ok"] is False

    case = _case(expect={"stderr_not_contains": "secret"})
    assert evaluate_case(case, _outcome(stderr="public"))[0]["ok"] is True
    assert evaluate_case(case, _outcome(stderr="my secret"))[0]["ok"] is False


def test_evaluate_regex_assertions_are_multiline():
    case = _case(expect={"stderr_not_matches": ["(?m)^\\s+at ", "Node\\.js v\\d"]})
    clean = _outcome(stderr="error: profile does not exist\n")
    assert evaluate_case(case, clean)[0]["ok"] is True

    trace = _outcome(stderr="Error: boom\n    at loadProfile (file:///x.js:1)\nNode.js v22.23.0\n")
    checks = evaluate_case(case, trace)
    assert checks[0]["ok"] is False
    assert set(checks[0]["actual"]) == {"(?m)^\\s+at ", "Node\\.js v\\d"}


def test_evaluate_empty_assertions():
    assert evaluate_case(_case(expect={"stderr_empty": True}), _outcome(stderr="  \n"))[0]["ok"] is True
    assert evaluate_case(_case(expect={"stderr_empty": True}), _outcome(stderr="boom"))[0]["ok"] is False


def test_evaluate_rejects_invalid_regex():
    with pytest.raises(ValueError, match="invalid regex"):
        evaluate_case(_case(expect={"stdout_matches": "(unclosed"}), _outcome())


# --- case execution ---------------------------------------------------------------


def test_run_case_passes_and_records_argv(tmp_path):
    recorder = []
    result = run_case(_case(args=["--profile", "{tmp}/x.yml"]), _runner(recorder=recorder), "probe-bin", tmp_path)

    assert result["verdict"] == "passed"
    assert result["argv"][0] == "probe-bin"
    assert recorder[0]["argv"][2] == str(tmp_path / "C-1" / "x.yml")
    assert recorder[0]["cwd"] == str(tmp_path / "C-1")


def test_run_case_materializes_fixture_files(tmp_path):
    seen = {}

    def run(argv, cwd, timeout_s, env):
        seen["exists"] = (Path(cwd) / "overlay.yml").is_file()
        seen["content"] = (Path(cwd) / "overlay.yml").read_text(encoding="utf-8")
        return _outcome()

    case = _case(files={"overlay.yml": "- insert:\n    - id: probe\n"})
    run_case(case, run, "probe-bin", tmp_path)

    assert seen["exists"] is True
    assert "id: probe" in seen["content"]


def test_run_case_expands_and_passes_env(tmp_path):
    recorder = []
    case = _case(env={"DSH_HOME": "{tmp}/home", "PLAIN": "value"})
    result = run_case(case, _runner(recorder=recorder), "probe-bin", tmp_path)

    assert recorder[0]["env"] == {"DSH_HOME": str(tmp_path / "C-1" / "home"), "PLAIN": "value"}
    assert result["env"]["PLAIN"] == "value"


def test_run_case_defaults_to_empty_env(tmp_path):
    recorder = []
    run_case(_case(), _runner(recorder=recorder), "probe-bin", tmp_path)
    assert recorder[0]["env"] == {}


def test_run_case_checks_file_expectations(tmp_path):
    case = _case(
        files={"made.txt": "payload"},
        expect={"exit_code": 0, "file_exists": ["made.txt"], "file_not_exists": ["absent.txt"]},
    )
    result = run_case(case, _runner(), "probe-bin", tmp_path)
    assert result["verdict"] == "passed"
    assert [check["assertion"] for check in result["checks"]] == ["exit_code", "file_exists", "file_not_exists"]

    case = _case(expect={"exit_code": 0, "file_exists": ["missing.txt"]})
    result = run_case(case, _runner(), "probe-bin", tmp_path)
    assert result["verdict"] == "failed"
    assert result["failed_assertions"] == ["file_exists"]


def test_load_suite_rejects_escaping_expect_path(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite([_case(expect={"exit_code": 0, "file_exists": ["../escape"]})])), encoding="utf-8")
    with pytest.raises(ValueError, match="must be relative"):
        load_suite(path)
    path.write_text(json.dumps(_suite([_case(expect={"exit_code": 0, "file_exists": ["/abs"]})])), encoding="utf-8")
    with pytest.raises(ValueError, match="must be relative"):
        load_suite(path)


# --- DSH session-behaviour expectations --------------------------------------------


def _write_dsh_session(home, events, session_id="session-1"):
    import zstandard

    path = Path(home) / "sessions" / "ws" / session_id / "session.jsonl.zstd"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = "\n".join(json.dumps(event) for event in events).encode("utf-8")
    path.write_bytes(zstandard.ZstdCompressor().compress(data))
    return path


def _probe_events(tool_text="file.txt"):
    return [
        {"type": "session", "id": "s-1", "data": {"cwd": "/x"}},
        {"type": "user/message", "data": {"id": "u1", "content": [{"type": "text", "text": "do it"}]}},
        {
            "type": "assistant/message",
            "data": {"message": {"id": "a1", "content": [
                {"type": "tool-call", "id": "c1", "name": "bash", "arguments": '{"command": "ls"}'},
            ]}},
        },
        {
            "type": "tool/result",
            "data": {"message": {"id": "t1", "content": [
                {"type": "tool-result", "toolCallId": "c1", "content": [{"type": "text", "text": tool_text}]},
            ]}},
        },
        {"type": "assistant/message", "data": {"message": {"id": "a2", "content": [{"type": "text", "text": "done"}]}}},
    ]


def test_run_case_audits_recorded_session(tmp_path):
    home = tmp_path / "dsh-home"

    def run(argv, cwd, timeout_s, env):
        _write_dsh_session(home, _probe_events())
        return _outcome()

    case = _case(
        env={"DSH_HOME": str(home)},
        session={
            "recorded": True,
            "uses_tool": ["bash"],
            "not_uses_tool": ["write"],
            "trajectory_contains": ["file.txt"],
            "trajectory_not_contains": ["nope"],
            "trajectory_matches": ["file\\.txt"],
            "tool_calls_min": 1,
            "tool_calls_max": 3,
            "no_missing_tool_results": True,
            "no_orphan_tool_results": True,
            "no_tool_errors": True,
            "final_answer": True,
        },
    )
    result = run_case(case, run, "probe-bin", tmp_path)
    assert result["verdict"] == "passed"
    assert result["session_path"].endswith("session.jsonl.zstd")


def test_run_case_fails_when_session_not_recorded(tmp_path):
    home = tmp_path / "dsh-home"
    case = _case(env={"DSH_HOME": str(home)}, session={"recorded": True})
    result = run_case(case, _runner(), "probe-bin", tmp_path)
    assert result["verdict"] == "failed"
    assert result["failed_assertions"] == ["session.recorded"]
    assert result["session_path"] == ""


def test_run_case_asserts_no_session_recorded(tmp_path):
    home = tmp_path / "dsh-home"
    case = _case(env={"DSH_HOME": str(home)}, session={"recorded": False})
    assert run_case(case, _runner(), "probe-bin", tmp_path)["verdict"] == "passed"

    def run(argv, cwd, timeout_s, env):
        _write_dsh_session(home, _probe_events())
        return _outcome()

    result = run_case(case, run, "probe-bin", tmp_path)
    assert result["verdict"] == "failed"
    assert result["failed_assertions"] == ["session.recorded"]


def test_run_case_session_checks_report_failures(tmp_path):
    home = tmp_path / "dsh-home"

    def run(argv, cwd, timeout_s, env):
        _write_dsh_session(home, _probe_events(tool_text="error executing tool: boom"))
        return _outcome()

    case = _case(
        env={"DSH_HOME": str(home)},
        session={"uses_tool": ["write"], "trajectory_contains": ["absent-text"], "no_tool_errors": True},
    )
    result = run_case(case, run, "probe-bin", tmp_path)
    assert result["verdict"] == "failed"
    assert set(result["failed_assertions"]) == {
        "session.uses_tool",
        "session.trajectory_contains",
        "session.no_tool_errors",
    }


def test_load_suite_rejects_unknown_session_key(tmp_path):
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(_suite([_case(session={"recordedd": True})])), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown session keys: recordedd"):
        load_suite(path)


def test_run_case_fails_when_a_check_fails(tmp_path):
    result = run_case(_case(expect={"exit_code": 0}), _runner(_outcome(exit_code=1)), "probe-bin", tmp_path)
    assert result["verdict"] == "failed"
    assert result["failed_assertions"] == ["exit_code"]


def test_run_case_xfail_absorbs_a_failure(tmp_path):
    case = _case(expect={"exit_code": 0}, xfail=True, defect="BUG-1")
    result = run_case(case, _runner(_outcome(exit_code=1)), "probe-bin", tmp_path)
    assert result["verdict"] == "xfail"
    assert result["defect"] == "BUG-1"


def test_run_case_xpass_marks_an_unexpected_pass(tmp_path):
    case = _case(expect={"exit_code": 0}, xfail=True, defect="BUG-1")
    result = run_case(case, _runner(_outcome(exit_code=0)), "probe-bin", tmp_path)
    assert result["verdict"] == "xpass"


def test_run_case_timeout_is_an_error_not_an_xfail(tmp_path):
    case = _case(expect={"exit_code": 0}, xfail=True, defect="BUG-1")
    result = run_case(case, _runner(_outcome(exit_code=None, timed_out=True)), "probe-bin", tmp_path)
    assert result["verdict"] == "error"


# --- suite execution and reporting -------------------------------------------------


def test_run_suite_counts_and_ok_flag(tmp_path):
    suite = _suite(
        [
            _case(id="P", expect={"exit_code": 0}),
            _case(id="F", expect={"exit_code": 0}, args=["--f"]),
        ]
    )
    report = run_suite(suite, runner=_runner(_outcome(exit_code=0)), workspace_root=tmp_path)
    assert report["summary"] == {"total": 2, "passed": 2, "failed": 0, "xfail": 0, "xpass": 0, "error": 0, "ok": True}

    report = run_suite(suite, runner=_runner(_outcome(exit_code=7)), workspace_root=tmp_path)
    assert report["summary"]["failed"] == 2
    assert report["summary"]["ok"] is False


def test_run_suite_ok_is_false_on_xpass_and_error(tmp_path):
    suite = _suite([_case(id="X", xfail=True, defect="BUG-1")])
    report = run_suite(suite, runner=_runner(_outcome()), workspace_root=tmp_path)
    assert report["summary"]["xpass"] == 1
    assert report["summary"]["ok"] is False

    suite = _suite([_case(id="T")])
    report = run_suite(suite, runner=_runner(_outcome(timed_out=True, exit_code=None)), workspace_root=tmp_path)
    assert report["summary"]["error"] == 1
    assert report["summary"]["ok"] is False


def test_run_suite_only_filter_matches_id_and_module(tmp_path):
    suite = _suite([_case(id="A", module="M1"), _case(id="B", module="M2")])
    assert [c["id"] for c in run_suite(suite, _runner(), only=["M2"], workspace_root=tmp_path)["cases"]] == ["B"]
    assert [c["id"] for c in run_suite(suite, _runner(), only=["A"], workspace_root=tmp_path)["cases"]] == ["A"]
    with pytest.raises(ValueError, match="no case matched"):
        run_suite(suite, _runner(), only=["nope"], workspace_root=tmp_path)


def test_run_suite_uses_the_suite_binary(tmp_path):
    recorder = []
    run_suite(_suite([_case()]), runner=_runner(recorder=recorder), workspace_root=tmp_path)
    assert recorder[0]["argv"][0] == "probe-bin"


def test_render_report_markdown_includes_verdicts_and_defects(tmp_path):
    suite = _suite([_case(id="OK"), _case(id="BAD", expect={"exit_code": 0}, args=["--b"], xfail=True, defect="BUG-9")])
    report = run_suite(suite, runner=_runner(_outcome(exit_code=1)), workspace_root=tmp_path)
    markdown = render_report_markdown(report)
    assert "# Case Suite Report — probe" in markdown
    assert "XFAIL" in markdown
    assert "BUG-9" in markdown
    assert "`BAD`" in markdown


def test_write_report_writes_json_and_markdown(tmp_path):
    report = run_suite(_suite([_case()]), runner=_runner(), workspace_root=tmp_path)
    paths = write_report(report, tmp_path / "results")
    assert Path(paths["json_path"]).is_file()
    assert Path(paths["markdown_path"]).is_file()
    payload = json.loads(Path(paths["json_path"]).read_text(encoding="utf-8"))
    assert payload["suite"] == "probe"
    assert payload["cases"][0]["id"] == "C-1"
