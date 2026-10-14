"""Deterministic acceptance-case runner for black-box targets.

A suite file is JSON: a target description plus a list of cases. Each case
declares the argv to run, optional file fixtures, and the expected outcome.
The runner executes every case through an injectable command runner, turns each
expectation into an explicit check, and returns a pass/fail report.

This module is the deterministic path: it spawns the target binary and reads its
output, and it never touches the network, model credentials, or the ``.[agent]``
extra. Tests inject a fake runner so no real binary is required.

Case fields
-----------
``id``            unique case id, e.g. ``DSH-CLI-004``.
``title``         human-readable title.
``module``        optional grouping label, usable with ``only``.
``priority``      optional label such as ``P0``.
``args``          argv after the target binary; ``{tmp}`` expands to the fixture dir.
``files``         optional ``{relative_path: content}`` written into the fixture dir.
``env``           optional ``{NAME: value}`` environment overrides for the spawned
                  process; values expand ``{tmp}`` to the fixture dir. Use it to
                  isolate target state (e.g. ``DSH_HOME``) per case.
``timeout_s``     per-case timeout, default 30.
``expect``        assertion mapping, see ``_CHECK_KEYS``. ``file_exists`` and
                  ``file_not_exists`` take workspace-relative paths (no absolute
                  paths, no ``..``) and check the fixture dir after the run —
                  e.g. a profile directory the target should have created.
``xfail``         ``true`` marks a known defect: a failing case is reported as
                  ``xfail`` and does not fail the suite, while an unexpectedly
                  passing case is reported as ``xpass`` and does fail it.
``defect``        defect id or description, required when ``xfail`` is set.
``notes``         optional free-text note.
``session``       optional DSH session-behaviour expectations, see
                  ``_SESSION_CHECK_KEYS``. After the argv completes, the runner
                  diffs the session recordings under the case's DSH home (the
                  ``env`` override, else the operator's default) against a
                  snapshot taken before the run, converts the newest new
                  recording with ``dsh_source`` and audits it with ``audit`` —
                  e.g. a headless agent run can be asserted to have been
                  sandbox-denied via ``trajectory_matches``.

A timeout or spawn failure is always reported as ``error`` and is never absorbed
by ``xfail``: a broken environment must stay visible.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from .trajectory import write_json


# Assertion keys accepted in ``expect``. An unknown key is a hard error: a
# silently ignored typo would turn an assertion into a no-op that always passes.
_CHECK_KEYS = (
    "exit_code",
    "stdout_empty",
    "stderr_empty",
    "stdout_contains",
    "stdout_not_contains",
    "stderr_contains",
    "stderr_not_contains",
    "stdout_matches",
    "stdout_not_matches",
    "stderr_matches",
    "stderr_not_matches",
    "file_exists",
    "file_not_exists",
)

# Assertion keys accepted in a case's ``session`` block.
_SESSION_CHECK_KEYS = (
    "recorded",
    "trajectory_contains",
    "trajectory_not_contains",
    "trajectory_matches",
    "trajectory_not_matches",
    "uses_tool",
    "not_uses_tool",
    "tool_calls_min",
    "tool_calls_max",
    "no_missing_tool_results",
    "no_orphan_tool_results",
    "no_tool_errors",
    "final_answer",
)

_OUTPUT_LIMIT = 4000

Runner = Callable[[List[str], Path, float, Dict[str, str]], Dict[str, Any]]


def load_suite(path: str | Path) -> Dict[str, Any]:
    """Load and validate a suite file. Raises ``ValueError`` on any problem."""
    suite_path = Path(path)
    try:
        payload = json.loads(suite_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("suite file not found: %s" % suite_path) from exc
    except json.JSONDecodeError as exc:
        raise ValueError("suite file is not valid JSON: %s: %s" % (suite_path, exc)) from exc
    if not isinstance(payload, dict):
        raise ValueError("suite file must contain a JSON object: %s" % suite_path)

    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("suite %s has no cases" % suite_path)

    seen: set[str] = set()
    for index, case in enumerate(cases):
        _validate_case(case, index, seen)
    return payload


def _validate_case(case: Any, index: int, seen: set[str]) -> None:
    where = "cases[%s]" % index
    if not isinstance(case, dict):
        raise ValueError("%s must be an object" % where)
    case_id = str(case.get("id") or "").strip()
    if not case_id:
        raise ValueError("%s is missing id" % where)
    if case_id in seen:
        raise ValueError("duplicate case id: %s" % case_id)
    seen.add(case_id)
    if not str(case.get("title") or "").strip():
        raise ValueError("%s (%s) is missing title" % (where, case_id))
    args = case.get("args")
    if not isinstance(args, list) or not args or not all(isinstance(item, str) for item in args):
        raise ValueError("%s (%s) needs args as a non-empty list of strings" % (where, case_id))
    expect = case.get("expect")
    if not isinstance(expect, dict) or not expect:
        raise ValueError("%s (%s) needs a non-empty expect object" % (where, case_id))
    unknown = sorted(set(expect) - set(_CHECK_KEYS))
    if unknown:
        raise ValueError(
            "%s (%s) has unknown expect keys: %s; allowed: %s"
            % (where, case_id, ", ".join(unknown), ", ".join(_CHECK_KEYS))
        )
    for key in ("file_exists", "file_not_exists"):
        for relpath in _as_list(expect.get(key) or []):
            _validate_expect_path(relpath, where, case_id)
    files = case.get("files")
    if files is not None and not isinstance(files, dict):
        raise ValueError("%s (%s) files must be an object of path -> content" % (where, case_id))
    env = case.get("env")
    if env is not None and (
        not isinstance(env, dict) or not all(isinstance(key, str) for key in env)
    ):
        raise ValueError("%s (%s) env must be an object of NAME -> value" % (where, case_id))
    session = case.get("session")
    if session is not None:
        if not isinstance(session, dict):
            raise ValueError("%s (%s) session must be an object" % (where, case_id))
        unknown_session = sorted(set(session) - set(_SESSION_CHECK_KEYS))
        if unknown_session:
            raise ValueError(
                "%s (%s) has unknown session keys: %s; allowed: %s"
                % (where, case_id, ", ".join(unknown_session), ", ".join(_SESSION_CHECK_KEYS))
            )
    if case.get("xfail") and not str(case.get("defect") or "").strip():
        raise ValueError("%s (%s) is xfail and must name a defect" % (where, case_id))


def evaluate_case(case: Dict[str, Any], outcome: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Turn one case's expectations into explicit, individually reported checks."""
    expect = case.get("expect") or {}
    stdout = str(outcome.get("stdout") or "")
    stderr = str(outcome.get("stderr") or "")
    checks: List[Dict[str, Any]] = []

    if "exit_code" in expect:
        checks.append(_check_exit_code(expect["exit_code"], outcome.get("exit_code")))
    for key in ("stdout_empty", "stderr_empty"):
        if key in expect:
            checks.append(_check_empty(key, bool(expect[key]), stdout if key == "stdout_empty" else stderr))
    for key in ("stdout_contains", "stdout_not_contains", "stderr_contains", "stderr_not_contains"):
        if key in expect:
            text = stdout if key.startswith("stdout") else stderr
            checks.append(_check_contains(key, expect[key], text))
    for key in ("stdout_matches", "stdout_not_matches", "stderr_matches", "stderr_not_matches"):
        if key in expect:
            text = stdout if key.startswith("stdout") else stderr
            checks.append(_check_matches(key, expect[key], text))
    return checks


def _check_exit_code(spec: Any, actual: Any) -> Dict[str, Any]:
    if isinstance(spec, dict):
        if "in" in spec:
            allowed = [int(item) for item in spec["in"]]
            ok = actual in allowed
            return _check("exit_code", "in %s" % allowed, actual, ok, "exit code is not one of the allowed values")
        if "not" in spec:
            forbidden = int(spec["not"])
            ok = actual != forbidden
            return _check("exit_code", "not %s" % forbidden, actual, ok, "exit code equals the forbidden value")
        raise ValueError("exit_code object form supports only 'in' and 'not'")
    expected = int(spec)
    return _check("exit_code", expected, actual, actual == expected, "exit code did not match")


def _check_empty(name: str, want_empty: bool, text: str) -> Dict[str, Any]:
    is_empty = not text.strip()
    ok = is_empty == want_empty
    return _check(
        name,
        "empty" if want_empty else "not empty",
        "empty" if is_empty else "%s chars" % len(text.strip()),
        ok,
        "%s was not %s" % (name, "empty" if want_empty else "non-empty"),
    )


def _check_contains(name: str, spec: Any, text: str) -> Dict[str, Any]:
    needles = _as_list(spec)
    if name.endswith("_not_contains"):
        found = [needle for needle in needles if needle in text]
        return _check(name, needles, found or "none found", not found, "forbidden text is present")
    missing = [needle for needle in needles if needle not in text]
    return _check(name, needles, missing or "all present", not missing, "required text is missing")


def _check_matches(name: str, spec: Any, text: str) -> Dict[str, Any]:
    patterns = _as_list(spec)
    matched = []
    for pattern in patterns:
        try:
            if re.search(pattern, text, re.MULTILINE):
                matched.append(pattern)
        except re.error as exc:
            raise ValueError("invalid regex %r: %s" % (pattern, exc)) from exc
    if name.endswith("_not_matches"):
        return _check(name, patterns, matched or "no match", not matched, "forbidden pattern matched")
    missing = [pattern for pattern in patterns if pattern not in matched]
    return _check(name, patterns, missing or "all matched", not missing, "required pattern did not match")


def _check(assertion: str, expected: Any, actual: Any, ok: bool, description: str) -> Dict[str, Any]:
    return {
        "assertion": assertion,
        "expected": expected,
        "actual": actual,
        "ok": bool(ok),
        "message": "" if ok else description,
    }


def _as_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _validate_expect_path(relpath: str, where: str, case_id: str) -> None:
    """Expect paths must stay inside the case workspace."""
    path = Path(relpath)
    if not relpath or path.is_absolute() or ".." in path.parts:
        raise ValueError(
            "%s (%s) expect path must be relative and stay inside the workspace: %r"
            % (where, case_id, relpath)
        )


def evaluate_file_checks(case: Dict[str, Any], workspace: Path) -> List[Dict[str, Any]]:
    """Check ``file_exists`` / ``file_not_exists`` expectations against the fixture dir."""
    expect = case.get("expect") or {}
    checks: List[Dict[str, Any]] = []
    for key in ("file_exists", "file_not_exists"):
        if key not in expect:
            continue
        want_exists = key == "file_exists"
        for relpath in _as_list(expect[key]):
            exists = (Path(workspace) / relpath).exists()
            checks.append(
                _check(
                    key,
                    relpath,
                    "exists" if exists else "missing",
                    exists == want_exists,
                    "%s should %s" % (relpath, "exist" if want_exists else "not exist"),
                )
            )
    return checks


def snapshot_session_files(env: Dict[str, str]) -> set:
    """Snapshot the DSH session recordings visible before a case runs."""
    from . import dsh_source

    home = Path(env["DSH_HOME"]) if env.get("DSH_HOME") else dsh_source.default_dsh_home()
    try:
        return set(dsh_source.iter_session_files(home))
    except Exception:
        return set()


def evaluate_session(
    case: Dict[str, Any], before: set, env: Dict[str, str]
) -> "tuple[List[Dict[str, Any]], str]":
    """Audit the DSH session a case recorded and check the ``session`` block.

    Returns the checks plus the recording path (empty when none was found).
    """
    from . import audit, dsh_source

    spec = case.get("session") or {}
    home = Path(env["DSH_HOME"]) if env.get("DSH_HOME") else dsh_source.default_dsh_home()
    try:
        after = set(dsh_source.iter_session_files(home))
    except Exception:
        after = set()
    new = sorted(after - set(before), key=lambda p: p.stat().st_mtime)

    checks: List[Dict[str, Any]] = []
    want_recorded = bool(spec.get("recorded", True))
    checks.append(
        _check(
            "session.recorded",
            "recorded" if want_recorded else "not recorded",
            ("%d new" % len(new)) if new else "none",
            bool(new) == want_recorded,
            "a DSH session should %shave been recorded" % ("" if want_recorded else "not "),
        )
    )
    if not new:
        return checks, ""

    path = new[-1]
    try:
        events = dsh_source.read_events(path)
        messages = dsh_source.events_to_messages(events)
        report = audit.audit_messages(messages)
    except Exception as exc:
        checks.append(_check("session.parse", "readable", type(exc).__name__, False, "session recording could not be parsed: %s" % exc))
        return checks, str(path)

    text = _flatten_messages(messages)
    for key in ("trajectory_contains", "trajectory_not_contains"):
        if key in spec:
            checks.append(_check_contains("session." + key, spec[key], text))
    for key in ("trajectory_matches", "trajectory_not_matches"):
        if key in spec:
            checks.append(_check_matches("session." + key, spec[key], text))

    tool_names = set(report.get("tool_names") or [])
    for key in ("uses_tool", "not_uses_tool"):
        if key not in spec:
            continue
        names = _as_list(spec[key])
        if key == "uses_tool":
            missing = [name for name in names if name not in tool_names]
            checks.append(_check("session.uses_tool", names, missing or "all used", not missing, "expected tool was not called"))
        else:
            found = [name for name in names if name in tool_names]
            checks.append(_check("session.not_uses_tool", names, found or "none used", not found, "forbidden tool was called"))

    call_count = int(report.get("tool_call_count") or 0)
    if "tool_calls_min" in spec:
        minimum = int(spec["tool_calls_min"])
        checks.append(_check("session.tool_calls_min", minimum, call_count, call_count >= minimum, "fewer tool calls than expected"))
    if "tool_calls_max" in spec:
        maximum = int(spec["tool_calls_max"])
        checks.append(_check("session.tool_calls_max", maximum, call_count, call_count <= maximum, "more tool calls than expected"))

    if "no_missing_tool_results" in spec:
        missing = report.get("missing_tool_results") or []
        checks.append(_check("session.no_missing_tool_results", "none", len(missing), not missing, "tool calls without results: %s" % ", ".join(missing[:5])))
    if "no_orphan_tool_results" in spec:
        orphans = report.get("orphan_tool_results") or []
        checks.append(_check("session.no_orphan_tool_results", "none", len(orphans), not orphans, "tool results without calls: %s" % ", ".join(orphans[:5])))
    if "no_tool_errors" in spec:
        errors = [f for f in report.get("findings") or [] if f.get("code") == "tool_error"]
        checks.append(_check("session.no_tool_errors", "none", len(errors), not errors, "tool errors found: %s" % "; ".join(str(e.get("message"))[:120] for e in errors[:3])))
    if "final_answer" in spec:
        chars = int(report.get("final_answer_chars") or 0)
        want = bool(spec["final_answer"])
        checks.append(_check("session.final_answer", "present" if want else "absent", "%s chars" % chars, (chars > 0) == want, "final answer expectation not met"))
    return checks, str(path)


def _flatten_messages(messages: Iterable[Dict[str, Any]]) -> str:
    """Flatten normalized messages into one searchable text for trajectory assertions."""
    parts: List[str] = []
    for message in messages:
        parts.append(str(message.get("role") or ""))
        for key in ("content", "reasoning"):
            if message.get(key):
                parts.append(str(message[key]))
        for call in message.get("tool_calls") or []:
            parts.append(str(call.get("name") or ""))
            parts.append(str(call.get("arguments") or ""))
    return "\n".join(parts)


def run_case(case: Dict[str, Any], runner: Runner, binary: str, workspace_root: Path) -> Dict[str, Any]:
    """Execute one case and return its verdict with per-check evidence."""
    case_id = str(case["id"])
    workspace = Path(workspace_root) / case_id
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    for relpath, content in (case.get("files") or {}).items():
        target = workspace / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")

    argv = [binary] + [arg.replace("{tmp}", str(workspace)) for arg in case["args"]]
    env = {
        str(key): str(value).replace("{tmp}", str(workspace))
        for key, value in (case.get("env") or {}).items()
    }
    timeout_s = float(case.get("timeout_s") or 30)
    session_before = snapshot_session_files(env) if case.get("session") else None
    outcome = runner(argv, workspace, timeout_s, env)
    checks = evaluate_case(case, outcome)
    checks.extend(evaluate_file_checks(case, workspace))
    session_path = ""
    if session_before is not None:
        session_checks, session_path = evaluate_session(case, session_before, env)
        checks.extend(session_checks)
    xfail = bool(case.get("xfail"))
    verdict = _verdict(checks, outcome, xfail)

    return {
        "id": case_id,
        "title": case.get("title") or "",
        "module": case.get("module") or "",
        "priority": case.get("priority") or "",
        "argv": argv,
        "env": env,
        "verdict": verdict,
        "xfail": xfail,
        "defect": case.get("defect") or "",
        "notes": case.get("notes") or "",
        "exit_code": outcome.get("exit_code"),
        "timed_out": bool(outcome.get("timed_out")),
        "duration_s": round(float(outcome.get("duration_s") or 0.0), 3),
        "checks": checks,
        "failed_assertions": [check["assertion"] for check in checks if not check["ok"]],
        "session_path": session_path,
        "stdout_tail": _tail(str(outcome.get("stdout") or "")),
        "stderr_tail": _tail(str(outcome.get("stderr") or "")),
    }


def _verdict(checks: List[Dict[str, Any]], outcome: Dict[str, Any], xfail: bool) -> str:
    if outcome.get("timed_out") or outcome.get("spawn_error"):
        return "error"
    failed = [check for check in checks if not check["ok"]]
    if not failed:
        return "xpass" if xfail else "passed"
    return "xfail" if xfail else "failed"


def run_suite(
    suite: Dict[str, Any],
    runner: Optional[Runner] = None,
    only: Optional[Iterable[str]] = None,
    binary: Optional[str] = None,
    workspace_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Run every selected case and return a report with counts and verdicts."""
    runner = runner or default_runner
    binary = binary or str(suite.get("binary") or "dsh")
    selected = _select_cases(suite.get("cases") or [], only)

    started = time.monotonic()
    owned_root = None
    root = Path(workspace_root) if workspace_root else None
    if root is None:
        owned_root = tempfile.mkdtemp(prefix="auditrail-cases-")
        root = Path(owned_root)
    try:
        cases = [run_case(case, runner, binary, root) for case in selected]
    finally:
        if owned_root:
            shutil.rmtree(owned_root, ignore_errors=True)

    counts = Counter(case["verdict"] for case in cases)
    summary = {
        "total": len(cases),
        "passed": counts["passed"],
        "failed": counts["failed"],
        "xfail": counts["xfail"],
        "xpass": counts["xpass"],
        "error": counts["error"],
    }
    summary["ok"] = not (summary["failed"] or summary["error"] or summary["xpass"])

    return {
        "suite": suite.get("name") or "unnamed",
        "target": suite.get("target") or "",
        "binary": binary,
        "started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration_s": round(time.monotonic() - started, 3),
        "summary": summary,
        "cases": cases,
    }


def _select_cases(cases: List[Dict[str, Any]], only: Optional[Iterable[str]]) -> List[Dict[str, Any]]:
    if not only:
        return list(cases)
    wanted = {str(item).strip() for item in only if str(item).strip()}
    selected = [
        case
        for case in cases
        if str(case.get("id")) in wanted or str(case.get("module") or "") in wanted
    ]
    if not selected:
        raise ValueError("no case matched --only %s" % ", ".join(sorted(wanted)))
    return selected


def default_runner(argv: List[str], cwd: Path, timeout_s: float, env: Dict[str, str]) -> Dict[str, Any]:
    """Run one argv with no shell, capturing stdout/stderr and the exit code."""
    started = time.monotonic()
    spawn_env = dict(os.environ)
    spawn_env.update(env)
    try:
        proc = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            env=spawn_env,
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "stdout": _text(exc.stdout),
            "stderr": _text(exc.stderr),
            "timed_out": True,
            "duration_s": time.monotonic() - started,
        }
    except FileNotFoundError as exc:
        return {
            "exit_code": None,
            "stdout": "",
            "stderr": str(exc),
            "spawn_error": str(exc),
            "duration_s": time.monotonic() - started,
        }
    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout or "",
        "stderr": proc.stderr or "",
        "timed_out": False,
        "duration_s": time.monotonic() - started,
    }


def write_report(report: Dict[str, Any], out_dir: str | Path) -> Dict[str, str]:
    """Write the JSON and Markdown reports and return their paths."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9]+", "-", str(report.get("suite") or "suite").lower()).strip("-") or "suite"
    json_path = directory / ("%s-%s.json" % (slug, stamp))
    markdown_path = directory / ("%s-%s.md" % (slug, stamp))
    write_json(json_path, report)
    markdown_path.write_text(render_report_markdown(report), encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(markdown_path)}


def render_report_markdown(report: Dict[str, Any]) -> str:
    """Render a human-readable suite report."""
    summary = report.get("summary") or {}
    lines = [
        "# Case Suite Report — %s" % (report.get("suite") or ""),
        "",
        "- target: `%s`" % (report.get("target") or ""),
        "- binary: `%s`" % (report.get("binary") or ""),
        "- started: `%s`" % (report.get("started_at") or ""),
        "- duration: `%ss`" % (report.get("duration_s") or 0),
        "- result: **%s**" % ("PASS" if summary.get("ok") else "FAIL"),
        "",
        "## Summary",
        "",
        "| total | passed | failed | xfail | xpass | error |",
        "|---:|---:|---:|---:|---:|---:|",
        "| %s | %s | %s | %s | %s | %s |"
        % (
            summary.get("total", 0),
            summary.get("passed", 0),
            summary.get("failed", 0),
            summary.get("xfail", 0),
            summary.get("xpass", 0),
            summary.get("error", 0),
        ),
        "",
        "## Cases",
        "",
        "| case | verdict | exit | priority | title |",
        "|---|---|---:|---|---|",
    ]
    for case in report.get("cases") or []:
        lines.append(
            "| `%s` | %s | %s | %s | %s |"
            % (
                case.get("id"),
                _verdict_label(case),
                "timeout" if case.get("timed_out") else case.get("exit_code"),
                case.get("priority") or "",
                _escape(case.get("title") or ""),
            )
        )

    problems = [case for case in report.get("cases") or [] if case.get("verdict") not in {"passed", "xfail"}]
    if problems:
        lines.extend(["", "## Failed Checks", ""])
        for case in problems:
            lines.append("### `%s` %s" % (case.get("id"), case.get("title") or ""))
            lines.append("")
            lines.append("`%s`" % " ".join(case.get("argv") or []))
            lines.append("")
            for check in case.get("checks") or []:
                if check.get("ok"):
                    continue
                lines.append(
                    "- **%s**: expected `%s`, got `%s`"
                    % (check.get("assertion"), check.get("expected"), check.get("actual"))
                )
                if check.get("message"):
                    lines.append("  - %s" % check["message"])
            lines.append("")
            if case.get("stderr_tail"):
                lines.extend(["stderr:", "", "```text", case["stderr_tail"], "```", ""])

    known = [case for case in report.get("cases") or [] if case.get("verdict") == "xfail"]
    if known:
        lines.extend(["## Known Defects (xfail)", ""])
        for case in known:
            lines.append(
                "- `%s` — %s (failed: %s)"
                % (case.get("id"), case.get("defect") or "", ", ".join(case.get("failed_assertions") or []))
            )
        lines.append("")
    return "\n".join(lines)


def _verdict_label(case: Dict[str, Any]) -> str:
    verdict = str(case.get("verdict") or "")
    if verdict == "xfail":
        return "XFAIL"
    if verdict == "xpass":
        return "XPASS"
    return verdict.upper()


def _escape(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _tail(text: str, limit: int = _OUTPUT_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
