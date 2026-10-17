"""Phase 0 single-run executor: one TestCase against one HTTP/SSE Agent.

This module is core collection logic: it reuses ``AgentClient`` and
``_write_run_outputs`` exactly like the CLI does, then adds the platform
lifecycle on top — ``run.json`` (Run Contract v1), ``evaluation.json``
(EvaluationEnvelope v1) and ``error.json`` for pre-collection failures. It
never writes judge reports; judging stays with ``write_judge_report``.

Determinism: this module needs no model credentials and no ``.[agent]``
extra; tests inject a fake ``AgentClient``.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import evaluation as evaluation_mod
from .cli import _source_meta, _write_run_outputs
from .client import AgentClient, AuditrailHTTPError
from .netguard import EndpointPolicy, EndpointRejected, redirect_guard, validate_endpoint
from .run_contract import (
    new_run_record,
    safe_message_for,
    update_run_status,
    write_error,
    write_run_record,
)
from .testcases import normalize_test_case, resolve_within


class RunFailure(RuntimeError):
    """A classified run failure: lifecycle status + safe error protocol fields."""

    def __init__(
        self,
        status: str,
        code: str,
        message: str,
        *,
        phase: str,
        retryable: bool = False,
        external_status: Optional[int] = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.external_status = external_status


def create_platform_run(
    runs_dir: Path,
    run_id: str,
    *,
    agent: Dict[str, Any],
    test_case: Dict[str, Any],
    suite_ref: Optional[Dict[str, Any]] = None,
) -> Path:
    """Create the run directory and its initial ``queued`` run.json."""
    run_dir = Path(runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    record = new_run_record(
        run_id,
        agent_ref={
            "id": agent.get("agent_id"),
            "version": agent.get("version"),
            "config_hash": agent.get("config_hash"),
        },
        test_case_ref={"id": test_case.get("case_id"), "version": test_case.get("version")},
    )
    if suite_ref:
        record["suite_ref"] = dict(suite_ref)
    write_run_record(run_dir, record)
    return run_dir


def execute_run(
    run_dir: Path,
    *,
    agent: Dict[str, Any],
    test_case: Dict[str, Any],
    judge: bool = False,
    policy: Optional[EndpointPolicy] = None,
    client_factory: Optional[Callable[..., AgentClient]] = None,
    judge_runner: Optional[Callable[[Path], None]] = None,
    cancel_event: Optional[Any] = None,
) -> Dict[str, Any]:
    """Execute one queued run to a terminal lifecycle state.

    Returns the final ``run.json`` record. Failures advance the state machine
    to ``failed``/``timed_out`` with ``status_reason`` and, when the run failed
    before complete reports existed, an ``error.json``; they never raise.

    ``cancel_event`` (a ``threading.Event``-like object) enables cooperative
    cancellation, used by the Phase 1 suite executor: the run checks it between
    collection steps and finishes as ``cancelled`` instead of writing reports.
    """
    run_dir = Path(run_dir)
    case = normalize_test_case(test_case)
    try:
        if case["adapter"] != "http_session":
            raise RunFailure(
                "failed",
                "unsupported_adapter",
                "Phase 0 platform runner supports adapter=http_session only, got %s" % case["adapter"],
                phase="validate",
                retryable=False,
            )
        update_run_status(run_dir, "running")
        _raise_if_cancelled(cancel_event, phase="collect")
        _execute_http_case(run_dir, agent, case, policy=policy, client_factory=client_factory, cancel_event=cancel_event)
        envelope = build_evaluation_envelope(run_dir, case)
        written = evaluation_mod.write_evaluation(run_dir, envelope)
        if written.get("status") == "invalid":
            # The envelope itself is malformed: keep artifacts, note the reason,
            # but do not fail the run lifecycle over an evaluation-side defect.
            update_run_status(
                run_dir,
                "completed",
                status_reason="evaluation.json invalid: %s" % written.get("error"),
            )
        else:
            update_run_status(run_dir, "completed")
    except RunFailure as exc:
        _fail_run(run_dir, exc)
    except Exception as exc:  # runner exception: never absorbed, always visible
        _fail_run(
            run_dir,
            RunFailure(
                "failed",
                "internal_error",
                safe_message_for(exc),
                phase="runner",
                retryable=False,
            ),
        )
    record = _reload(run_dir)
    if judge and record.get("status") == "completed":
        note = _run_judge(run_dir, judge_runner)
        _record_judge_outcome(run_dir, note)
        record = _reload(run_dir)
    return record


def _record_judge_outcome(run_dir: Path, note: Optional[str]) -> None:
    """Record judge success/failure inside evaluation.json (never in run.json).

    A judge failure leaves the Run ``completed``; the envelope carries the
    reason so the API/UI can show why no judge report exists.
    """
    envelope = evaluation_mod.load_evaluation(run_dir)
    if envelope is None:
        return
    envelope = dict(envelope)
    envelope["judge"] = {"status": "failed", "error": note} if note else {"status": "completed"}
    evaluation_mod.write_evaluation(run_dir, envelope)


def _reload(run_dir: Path) -> Dict[str, Any]:
    from .run_contract import read_run_record

    return read_run_record(run_dir) or {}


def _fail_run(run_dir: Path, failure: RunFailure) -> None:
    has_reports = (run_dir / "summary.json").exists() and (run_dir / "audit.json").exists()
    if not has_reports:
        write_error(
            run_dir,
            phase=failure.phase,
            error_type=failure.code,
            retryable=failure.retryable,
            safe_message=str(failure),
            external_status=failure.external_status,
        )
    update_run_status(run_dir, failure.status, status_reason="%s: %s" % (failure.code, failure))


def _raise_if_cancelled(cancel_event: Optional[Any], *, phase: str) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RunFailure(
            "cancelled",
            "cancelled",
            "run cancelled before completion",
            phase=phase,
            retryable=False,
        )


def _execute_http_case(
    run_dir: Path,
    agent: Dict[str, Any],
    case: Dict[str, Any],
    *,
    policy: Optional[EndpointPolicy],
    client_factory: Optional[Callable[..., AgentClient]],
    cancel_event: Optional[Any] = None,
) -> None:
    endpoint = agent.get("endpoint")
    try:
        endpoint = validate_endpoint(endpoint, policy)
    except EndpointRejected as exc:
        raise RunFailure("failed", "endpoint_rejected", exc.reason, phase="validate", retryable=False) from exc

    capabilities = set(agent.get("capabilities") or [])
    input_spec = case["input"]
    user_id = str(input_spec.get("user_id") or "")
    timeout_seconds = float(case.get("timeout_seconds") or 120)

    factory = client_factory or AgentClient
    client = factory(
        endpoint,
        user_id,
        token=None,  # credentials come from the environment, never the case/agent config
        timeout=min(60.0, timeout_seconds),
        url_guard=redirect_guard(policy),
    )

    deadline = time.monotonic() + timeout_seconds
    try:
        if input_spec.get("message"):
            if "chat" not in capabilities:
                raise RunFailure(
                    "failed",
                    "capability_not_supported",
                    "agent %s does not declare the chat capability" % agent.get("agent_id"),
                    phase="validate",
                    retryable=False,
                )
            session_id = _chat_flow(client, input_spec, case, deadline, cancel_event=cancel_event)
        else:
            session_id = str(input_spec.get("session_id") or "")
        session_response = client.fetch_session(session_id)
    except RunFailure:
        raise
    except EndpointRejected as exc:
        # A redirect target failed the SSRF policy after the initial connect.
        raise RunFailure("failed", "endpoint_rejected", exc.reason, phase="collect", retryable=False) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise RunFailure(
            "timed_out", "agent_timeout", "agent call timed out", phase="collect", retryable=True
        ) from exc
    except urllib.error.URLError as exc:
        if _is_timeout_reason(exc):
            raise RunFailure(
                "timed_out", "agent_timeout", "agent call timed out", phase="collect", retryable=True
            ) from exc
        raise RunFailure(
            "failed", "agent_unreachable", "cannot reach agent endpoint", phase="collect", retryable=True
        ) from exc
    except AuditrailHTTPError as exc:
        if exc.status == 0 and _is_timeout_text(exc.body):
            raise RunFailure(
                "timed_out", "agent_timeout", "agent call timed out", phase="collect", retryable=True
            ) from exc
        if exc.status == 0:
            raise RunFailure(
                "failed", "agent_unreachable", "cannot reach agent endpoint", phase="collect", retryable=True
            ) from exc
        raise RunFailure(
            "failed",
            "agent_http_error",
            "agent returned HTTP %s" % exc.status,
            phase="collect",
            retryable=exc.status >= 500,
            external_status=exc.status,
        ) from exc

    if not isinstance(session_response, dict) or not session_response:
        raise RunFailure(
            "failed",
            "invalid_agent_response",
            "agent returned an empty session response",
            phase="collect",
            retryable=True,
        )
    _raise_if_cancelled(cancel_event, phase="collect")
    _write_run_outputs(run_dir, session_response, source=_source_meta(endpoint, user_id))


def _chat_flow(
    client: AgentClient,
    input_spec: Dict[str, Any],
    case: Dict[str, Any],
    deadline: float,
    *,
    cancel_event: Optional[Any] = None,
) -> str:
    message = str(input_spec.get("message") or "")
    created = client.create_session(title="auditrail: %s" % case.get("title", case["case_id"]))
    session = created.get("session") or {}
    session_id = session.get("session_id")
    if not session_id:
        raise RunFailure(
            "failed",
            "invalid_agent_response",
            "session creation response did not include session.session_id",
            phase="collect",
            retryable=True,
        )
    started = client.start_chat(session_id, message)
    stream_id = started.get("stream_id")
    if not stream_id:
        raise RunFailure(
            "failed",
            "invalid_agent_response",
            "chat start response did not include stream_id",
            phase="collect",
            retryable=True,
        )
    for event_name, payload in client.stream_events(stream_id):
        _raise_if_cancelled(cancel_event, phase="collect")
        if time.monotonic() > deadline:
            raise RunFailure(
                "timed_out", "agent_timeout", "chat stream exceeded the case timeout", phase="collect", retryable=True
            )
        if event_name == "error":
            detail = " ".join(json.dumps(payload, ensure_ascii=False, default=str).split())[:300]
            raise RunFailure(
                "failed",
                "agent_stream_error",
                "agent stream reported an error: %s" % detail,
                phase="collect",
                retryable=True,
            )
        if event_name in {"stream_end", "cancelled"}:
            break
    return str(session_id)


def _is_timeout_reason(exc: urllib.error.URLError) -> bool:
    return _is_timeout_text(str(getattr(exc, "reason", "") or ""))


def _is_timeout_text(text: str) -> bool:
    lowered = str(text or "").lower()
    return "timed out" in lowered or "timeout" in lowered


def build_evaluation_envelope(run_dir: Path, case: Dict[str, Any]) -> Dict[str, Any]:
    """Build the EvaluationEnvelope v1 from deterministic run artifacts."""
    run_dir = Path(run_dir)
    audit = _read_json(run_dir / "audit.json")
    envelope = evaluation_mod.new_envelope("completed")
    envelope["deterministic_checks"] = _audit_checks(audit)
    envelope["expectation_results"] = _expectation_results(case, audit, run_dir)
    envelope["artifact_checks"] = _artifact_checks(case, run_dir)
    envelope["final_quality"] = evaluation_mod.compute_final_quality(envelope)
    return envelope


def _audit_checks(audit: Dict[str, Any]) -> List[Dict[str, Any]]:
    findings = [item for item in audit.get("findings") or [] if isinstance(item, dict)]
    codes = [str(item.get("code") or "") for item in findings]
    specs = [
        ("dangerous_tools", "dangerous_tool_used", ["audit.json"]),
        ("missing_tool_results", "missing_tool_result", ["audit.json"]),
        ("orphan_tool_results", "orphan_tool_result", ["audit.json"]),
        ("tool_errors", "tool_error", ["audit.json"]),
        ("final_answer", "missing_final_answer", ["audit.json"]),
    ]
    checks = []
    for kind, code, evidence in specs:
        failures = [item for item in findings if str(item.get("code") or "") == code]
        status = "fail" if failures else "pass"
        blocking = kind in evaluation_mod.DEFAULT_BLOCKING_KINDS and status == "fail"
        checks.append(
            evaluation_mod.make_check(
                "audit.%s" % kind,
                kind,
                status,
                blocking=blocking,
                blocking_source="structured_audit" if blocking else "none",
                evidence_refs=evidence,
                message="; ".join(str(item.get("message") or "")[:120] for item in failures[:3]),
            )
        )
    return checks


def _expectation_results(case: Dict[str, Any], audit: Dict[str, Any], run_dir: Path) -> List[Dict[str, Any]]:
    results = []
    findings = [item for item in audit.get("findings") or [] if isinstance(item, dict)]
    codes = {str(item.get("code") or "") for item in findings}
    for expectation in case.get("expectations") or []:
        exp_type = str(expectation.get("type") or "")
        required = bool(expectation.get("required", True))
        evidence = ["audit.json"]
        message = ""
        if exp_type == "final_answer_present":
            ok = int(audit.get("final_answer_chars") or 0) > 0
            status = "pass" if ok else "fail"
        elif exp_type == "no_tool_errors":
            status = "fail" if "tool_error" in codes else "pass"
        elif exp_type == "tool_error_is_observed":
            status = "pass" if "tool_error" in codes else "fail"
        elif exp_type == "no_missing_tool_results":
            status = "fail" if "missing_tool_result" in codes else "pass"
        elif exp_type == "no_orphan_tool_results":
            status = "fail" if "orphan_tool_result" in codes else "pass"
        elif exp_type == "no_dangerous_tools":
            status = "fail" if "dangerous_tool_used" in codes else "pass"
        elif exp_type == "artifact_exists":
            status = "unknown"  # computed by _artifact_checks, not here
            evidence = []
        else:
            status = "unknown"
            evidence = []
            message = "expectation type %r is not computable in Phase 0" % exp_type
        results.append(
            evaluation_mod.make_expectation_result(
                str(expectation.get("id") or ""),
                required=required,
                status=status,
                evidence_refs=evidence,
                message=message,
            )
        )
    return results


def _artifact_checks(case: Dict[str, Any], run_dir: Path) -> List[Dict[str, Any]]:
    checks = []
    for expectation in case.get("expectations") or []:
        if str(expectation.get("type") or "") != "artifact_exists":
            continue
        relpath = str(expectation.get("path") or "")
        try:
            target = resolve_within(run_dir, relpath)
            exists = target.is_file()
            status = "pass" if exists else "fail"
        except ValueError as exc:
            # An illegal path is a case definition error: fail closed, and make
            # the check itself carry the reason.
            checks.append(
                evaluation_mod.make_artifact_check(
                    relpath or "(invalid)",
                    required=bool(expectation.get("required", True)),
                    status="fail",
                    exists=False,
                    valid=False,
                    evidence_refs=[],
                )
            )
            continue
        checks.append(
            evaluation_mod.make_artifact_check(
                relpath,
                required=bool(expectation.get("required", True)),
                status=status,
                exists=exists,
                valid=exists,
                evidence_refs=[relpath] if exists else [],
            )
        )
    return checks


def _run_judge(run_dir: Path, judge_runner: Optional[Callable[[Path], None]]) -> Optional[str]:
    """Optionally run an LLM judge; failures never change the Run lifecycle."""
    if judge_runner is None:
        return "judge requested but no judge runner configured"
    try:
        judge_runner(run_dir)
    except Exception as exc:
        return "judge failed: %s" % safe_message_for(exc)
    return None


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
