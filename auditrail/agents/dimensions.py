from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .schemas import normalize_judge_report

#: Audit tool-call statuses that count as a failure for deterministic rules.
FAILED_STATUSES = {"error", "failed", "missing", "blocked", "timeout"}


def assess_dimensions(run_path: str | Path, report: dict[str, Any], reliability: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Apply deterministic dimension rules over the judge proposal.

    Every rule application is traceable: the judge's original score/status,
    the deterministic candidate, the rule id, the action (set/cap/keep), the
    before/after scores and the rule's verbatim evidence land in the
    dimension's ``evidence_chain``. Deterministic conclusions are never
    overwritten by the LLM judge because this runs after the proposal.
    """
    run_dir = Path(run_path)
    normalized = normalize_judge_report(report)
    dimensions = {key: dict(value) for key, value in normalized["dimensions"].items()}
    audit = _read_json(run_dir / "audit.json")
    trajectory = _read_jsonl(run_dir / "trajectory.jsonl")
    artifacts = _ArtifactQuotes(audit, trajectory)

    _apply_rule(dimensions, "tool_selection", _tool_selection_rule(audit, artifacts), cap=True)
    _apply_rule(dimensions, "execution_closure", _execution_closure_rule(trajectory, normalized, artifacts), cap=False)
    _apply_rule(dimensions, "error_recovery", _error_recovery_rule(audit, artifacts), cap=True)
    _apply_rule(dimensions, "evidence_citation", _evidence_citation_rule(reliability, normalized, artifacts), cap=False)
    _apply_rule(dimensions, "intent_recognition", _intent_recognition_rule(run_dir), cap=True)
    return dimensions


class _ArtifactQuotes:
    """Pick evidence quotes that survive verbatim JSON validation."""

    def __init__(self, audit: dict[str, Any], trajectory: list[dict[str, Any]]):
        self.audit = audit
        self.trajectory = trajectory
        self.audit_dump = json.dumps(audit, ensure_ascii=False, default=str) if audit else ""
        self.trajectory_dumps = [json.dumps(row, ensure_ascii=False, default=str) for row in trajectory]

    def audit_evidence(self, text: object) -> dict[str, Any] | None:
        quote = verbatim_quote(text, self.audit_dump)
        if not quote:
            return None
        return {"source": "audit.json", "quote": quote}

    def trajectory_evidence(self, row: dict[str, Any], text: object) -> dict[str, Any] | None:
        index = row.get("index")
        dump = self.trajectory_dumps[index] if isinstance(index, int) and 0 <= index < len(self.trajectory_dumps) else ""
        if not dump:
            dump = json.dumps(row, ensure_ascii=False, default=str)
        quote = verbatim_quote(text, dump)
        if not quote:
            return None
        evidence: dict[str, Any] = {"source": "trajectory.jsonl", "quote": quote}
        if isinstance(index, int):
            evidence["message_index"] = index
        return evidence

    def call_evidence(self, call: dict[str, Any]) -> dict[str, Any] | None:
        """Verbatim audit.json evidence for one audited tool call."""
        evidence = self.audit_evidence(call.get("result_preview") or call.get("args_preview"))
        if evidence is not None:
            return evidence
        status = str(call.get("status") or "").lower()
        if status:
            # ``"status": "error"`` appears verbatim under json.dumps' default
            # separators; it is the minimal honest quote for an empty preview.
            marker = '"status": "%s"' % status
            if marker in self.audit_dump:
                return {"source": "audit.json", "quote": marker}
        return None


def verbatim_quote(text: object, haystack: str, limit: int = 160) -> str:
    """Return a substring of ``text`` that appears verbatim in ``haystack``.

    JSON serialization escapes ``"``, ``\\`` and control characters, so a raw
    field value containing them would never match the dumped artifact. The
    quote is split on those characters and the longest verbatim fragment wins;
    an empty result means no honest quote exists and the caller must not emit
    one (a fabricated quote would fail validation anyway).
    """
    value = str(text or "").strip()
    if not value or not haystack:
        return ""
    candidates = [value[:limit]]
    for piece in re.split(r'["\\\x00-\x1f]+', value):
        piece = piece.strip()
        if piece:
            candidates.append(piece[:limit])
    for candidate in sorted(set(candidates), key=len, reverse=True):
        if candidate and candidate in haystack:
            return candidate
    return ""


def _apply_rule(dimensions: dict[str, dict[str, Any]], key: str, rule: dict[str, Any] | None, *, cap: bool) -> None:
    """Apply one deterministic rule and record its score trace.

    Actions: ``set`` assigns the deterministic score (the judge left the
    dimension unevaluated or scored higher for a ``set`` rule), ``cap`` lowers
    a judge score to the deterministic ceiling, ``keep`` leaves the judge
    score untouched but still records that the rule ran and why.
    """
    if rule is None:
        return
    current = dimensions.get(key) or {}
    current_source = current.get("source")
    judged = current_source not in {"missing", None, ""}
    judge_score = current.get("score")
    judge_status = str(current.get("status") or "unknown")
    unevaluated = judge_status == "unknown" or not judged
    candidate = rule["score"]

    if unevaluated or judge_score is None:
        action, final_score = "set", candidate
    elif cap and int(judge_score) <= candidate:
        action, final_score = "keep", int(judge_score)
    elif not cap and int(judge_score) < candidate:
        action, final_score = "keep", int(judge_score)
    else:
        action, final_score = ("cap" if cap else "set"), candidate

    chain = dict(current.get("evidence_chain") or {})
    trace = dict(chain.get("score_trace") or {})
    if "judge_score" not in trace:
        trace["judge_score"] = int(judge_score) if judged and judge_score is not None else None
        trace["judge_status"] = judge_status if judged else None
    trace["deterministic_score"] = candidate
    trace["final_score"] = final_score
    rules = list(trace.get("rules") or [])
    rules.append(
        {
            "id": rule["rule_id"],
            "action": action,
            "before": int(judge_score) if judged and judge_score is not None else None,
            "candidate": candidate,
            "after": final_score,
            "reason": rule["reason"],
            "evidence": list(rule.get("evidence") or []),
        }
    )
    trace["rules"] = rules
    chain["score_trace"] = trace
    if rule.get("checks"):
        chain["checks"] = list(chain.get("checks") or []) + list(rule["checks"])
    if rule.get("gaps"):
        chain["gaps"] = list(chain.get("gaps") or []) + list(rule["gaps"])

    updated = dict(current)
    if action != "keep":
        updated["score"] = final_score
        updated["status"] = _status(final_score)
        updated["summary"] = rule["summary"]
        updated["recommendation"] = rule["recommendation"]
        updated["source"] = "deterministic+judge" if judged else "deterministic"
        updated["evidence"] = _merge_evidence(current.get("evidence") or [], rule.get("evidence") or [])
    updated["evidence_chain"] = chain
    dimensions[key] = updated


def _merge_evidence(base: list[dict[str, Any]], extra: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [row for row in base if isinstance(row, dict)]
    seen = {(str(row.get("source") or ""), row.get("message_index"), str(row.get("quote") or "")) for row in merged}
    for row in extra:
        if not isinstance(row, dict):
            continue
        key = (str(row.get("source") or ""), row.get("message_index"), str(row.get("quote") or ""))
        if key in seen:
            continue
        seen.add(key)
        merged.append(row)
    return merged


def _status(score: int) -> str:
    if score >= 80:
        return "pass"
    if score >= 50:
        return "partial"
    return "fail"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _failed_calls(audit: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        call
        for call in audit.get("tool_calls") or []
        if isinstance(call, dict) and str(call.get("status") or "").lower() in FAILED_STATUSES
    ]


def _tool_selection_rule(audit: dict[str, Any], artifacts: _ArtifactQuotes) -> dict[str, Any] | None:
    calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    if not calls:
        return None
    failed = _failed_calls(audit)
    if not failed:
        # Successful calls prove nothing about selection quality; leave the
        # dimension to the judge instead of auto-passing it.
        return None
    score = round(100 * (1 - len(failed) / len(calls)))
    evidence = [row for row in (artifacts.call_evidence(call) for call in failed[:5]) if row]
    return {
        "score": score,
        "summary": "%s/%s tool calls failed." % (len(failed), len(calls)),
        "recommendation": "Investigate failed or unnecessary tool calls.",
        "rule_id": "tool_selection_cap",
        "reason": "%s/%s 次工具调用失败，按失败比例确定性计算得分上限。" % (len(failed), len(calls)),
        "evidence": evidence,
        "checks": [
            {
                "id": "tool_failures_detected",
                "criterion": "是否存在失败或异常的工具调用",
                "result": "fail",
                "conclusion": "%s/%s 次工具调用失败。" % (len(failed), len(calls)),
                "type": "fact",
                "evidence": evidence,
                "counter_evidence": [],
            }
        ],
    }


def _execution_closure_rule(
    trajectory: list[dict[str, Any]], report: dict[str, Any], artifacts: _ArtifactQuotes
) -> dict[str, Any] | None:
    if not trajectory:
        return None
    final_row = None
    for row in reversed(trajectory[-3:]):
        if str(row.get("role") or "") == "assistant" and str(row.get("content") or "").strip():
            final_row = row
            break
    evidence = []
    if final_row is not None:
        row_evidence = artifacts.trajectory_evidence(final_row, final_row.get("content"))
        if row_evidence:
            evidence.append(row_evidence)
        score, summary, recommendation = (
            (90, "Run has a final assistant response and success status.", "Verify claimed deliverables when present.")
            if report.get("overall_status") == "success"
            else (65, "Run has a final assistant response but did not fully succeed.", "Tie final response to explicit completion checks.")
        )
        result = "pass"
        conclusion = "轨迹末尾存在非空的 assistant 最终回答。"
    else:
        last = trajectory[-1]
        row_evidence = artifacts.trajectory_evidence(last, last.get("content") or last.get("role"))
        if row_evidence:
            evidence.append(row_evidence)
        score, summary, recommendation = 20, "No final assistant response was captured.", "Require final response before closing a run."
        result = "fail"
        conclusion = "轨迹末尾没有非空的 assistant 最终回答。"
    return {
        "score": score,
        "summary": summary,
        "recommendation": recommendation,
        "rule_id": "execution_closure_set",
        "reason": "执行闭环由轨迹末尾是否存在最终回答确定性决定。",
        "evidence": evidence,
        "checks": [
            {
                "id": "final_response_present",
                "criterion": "轨迹末尾是否存在非空的 assistant 最终回答",
                "result": result,
                "conclusion": conclusion,
                "type": "fact",
                "evidence": evidence,
                "counter_evidence": [],
            }
        ],
    }


def _error_recovery_rule(audit: dict[str, Any], artifacts: _ArtifactQuotes) -> dict[str, Any] | None:
    failed = _failed_calls(audit)
    if not failed:
        # No failure: error recovery is not applicable and stays unknown.
        return None
    failure_evidence = [row for row in (artifacts.call_evidence(call) for call in failed[:5]) if row]
    # The recovery window opens after the FIRST failure: a retry that itself
    # failed still counts as an attempted correction, just an unverified one.
    first_failed_index = min((call.get("index") for call in failed if isinstance(call.get("index"), int)), default=10**9)
    later = [
        call
        for call in audit.get("tool_calls") or []
        if isinstance(call, dict) and isinstance(call.get("index"), int) and call["index"] > first_failed_index
    ]
    attempted = bool(later)
    verified_calls = [call for call in later if str(call.get("status") or "").lower() == "ok"]
    verified = bool(verified_calls)
    attempt_evidence = [row for row in (artifacts.call_evidence(call) for call in later[:5]) if row]
    verified_evidence = [row for row in (artifacts.call_evidence(call) for call in verified_calls[:5]) if row]

    if verified:
        score = 60
        summary = "Failures were followed by retry or additional action with a successful result."
        recommendation = "Make recovery outcome explicit in the final response."
        reason = "检测到失败和恢复动作且后续调用成功，按确定性规则最高为 partial/60。"
    elif attempted:
        score = 45
        summary = "Failures were followed by further actions, but no successful result verifies recovery."
        recommendation = "Verify the corrective action succeeds before claiming recovery."
        reason = "检测到失败和纠正动作，但没有成功结果证明恢复，不能声称恢复成功。"
    else:
        score = 30
        summary = "Failures were captured without clear recovery."
        recommendation = "Add retry, fallback, or explicit blocked state handling."
        reason = "检测到工具失败且没有任何后续纠正动作。"

    evidence = failure_evidence + [row for row in verified_evidence if row not in failure_evidence]
    return {
        "score": score,
        "summary": summary,
        "recommendation": recommendation,
        "rule_id": "error_recovery_cap",
        "reason": reason,
        "evidence": evidence,
        "checks": [
            {
                "id": "failure_detected",
                "criterion": "是否出现明确的工具执行失败",
                "result": "fail",
                "conclusion": "%s 次工具调用以失败状态结束。" % len(failed),
                "type": "fact",
                "evidence": failure_evidence,
                "counter_evidence": [],
            },
            {
                "id": "recovery_attempted",
                "criterion": "失败后是否执行了重试、替代方案或纠正操作",
                "result": "pass" if attempted else "fail",
                "conclusion": "失败后执行了后续工具调用。" if attempted else "失败后没有任何后续工具调用。",
                "type": "fact",
                "evidence": attempt_evidence,
                "counter_evidence": [],
            },
            {
                "id": "recovery_verified",
                "criterion": "纠正操作是否有成功结果证明",
                "result": "pass" if verified else "fail",
                "conclusion": "后续工具调用返回了成功结果。" if verified else "后续动作没有成功结果，恢复未被验证。",
                "type": "fact",
                "evidence": verified_evidence,
                "counter_evidence": [],
            },
        ],
    }


def _evidence_citation_rule(
    reliability: dict[str, Any], report: dict[str, Any], artifacts: _ArtifactQuotes
) -> dict[str, Any] | None:
    total = int(reliability.get("total_evidence_count") or 0)
    invalid = int(reliability.get("invalid_evidence_count") or 0)
    invalid_support_raw = reliability.get("invalid_support_evidence_count")
    if invalid_support_raw is None:
        # 旧可靠性数据没有 support/counter 拆分：保守地把全部无效引用计入
        # support 侧，维持"任一无效即清零"的旧行为。
        invalid_support = invalid
        invalid_counter = 0
    else:
        invalid_support = int(invalid_support_raw or 0)
        invalid_counter = int(reliability.get("invalid_counter_evidence_count") or 0)
    valid_rows = _valid_judge_evidence(report, reliability)

    checks = []
    gaps = []
    if total == 0:
        score = 0
        summary = "No judge evidence was provided."
        recommendation = "Require evidence for findings and semantic dimension scores."
        # Honest support for a report-level conclusion: the run artifacts that
        # were available to cite. Without this the score would be unevidenced.
        evidence = _artifact_availability_evidence(artifacts)
        checks.append(
            {
                "id": "judge_evidence_provided",
                "criterion": "裁判报告是否提供可校验的证据引用",
                "result": "fail",
                "conclusion": "裁判报告没有提供任何证据引用，尽管运行产物可供引用。",
                "type": "inference",
                "evidence": evidence,
                "counter_evidence": [],
            }
        )
        gaps.append("裁判报告缺少证据引用，无法对语义结论做机械校验。")
        reason = "裁判报告没有提供任何证据引用。"
    elif invalid_support:
        # 只有支撑结论的引用无效才清零；反证（counter_evidence）的引用质量
        # 是可靠性的次要信号，不清零、只在下方按 coverage 计分时记录 gap。
        score = 0
        summary = "Invalid evidence references were found."
        recommendation = "Fix source, message_index, or quote references."
        evidence = valid_rows[:3]
        if not evidence:
            # Counter-proof: what the misquoted artifact rows actually say.
            evidence = _misquote_evidence(reliability, artifacts)
        if not evidence:
            evidence = _artifact_availability_evidence(artifacts)
        support_locations = [
            str(row.get("location") or "")
            for row in reliability.get("invalid_evidence") or []
            if isinstance(row, dict) and not str(row.get("location") or "").endswith(".counter")
        ]
        checks.append(
            {
                "id": "invalid_references",
                "criterion": "裁判引用的 source/message_index/quote 是否通过逐字校验",
                "result": "fail",
                "conclusion": "%s 条支撑侧引用未通过逐字校验：%s。"
                % (invalid_support, ", ".join(support_locations[:5]) or "unknown"),
                "type": "fact",
                "evidence": evidence,
                "counter_evidence": [],
            }
        )
        if invalid_counter:
            gaps.append("%s 条反证引用未通过逐字校验（次要信号，不影响清零判定）。" % invalid_counter)
            reason = "发现 %s 条无效支撑证据引用，另 %s 条反证引用无效。" % (invalid_support, invalid_counter)
        else:
            reason = "发现 %s 条无效支撑证据引用。" % invalid_support
    else:
        score = round(float(reliability.get("evidence_coverage") or 0) * 100)
        summary = "%s/%s evidence references are valid." % (reliability.get("valid_evidence_count", 0), total)
        recommendation = "Maintain quote-level evidence validation."
        evidence = valid_rows[:3]
        checks.append(
            {
                "id": "evidence_coverage",
                "criterion": "裁判证据引用通过逐字校验的比例",
                "result": _status(score),
                "conclusion": summary,
                "type": "fact",
                "evidence": evidence,
                "counter_evidence": [],
            }
        )
        if invalid_counter:
            gaps.append("%s 条反证引用未通过逐字校验（次要信号，不清零、按 coverage 正常计分）。" % invalid_counter)
            reason = "证据覆盖率 %s%% 由逐字校验确定性计算；%s 条反证引用无效，仅记录不清零。" % (
                score,
                invalid_counter,
            )
        else:
            reason = "证据覆盖率 %s%% 由逐字校验确定性计算。" % score

    return {
        "score": score,
        "summary": summary,
        "recommendation": recommendation,
        "rule_id": "evidence_citation_set",
        "reason": reason,
        "evidence": evidence,
        "checks": checks,
        "gaps": gaps,
    }


def _intent_recognition_rule(run_dir: Path) -> dict[str, Any] | None:
    """Cap intent_recognition from evaluation.json expectation results (C4).

    A failed *required* expectation is a hard signal that the run did not do
    what was asked, so the judge's intent score may not exceed 50; optional
    failures cap at 70. The rule only caps — execution problems belong to
    other dimensions — and it never fires without a completed envelope, with
    all-unknown results (unknown is never a failure), or with no failures at
    all (a pass proves nothing beyond the envelope's own scope).
    """
    from ..evaluation import load_evaluation

    envelope = load_evaluation(run_dir)
    if not envelope or envelope.get("status") != "completed":
        return None
    results = [row for row in envelope.get("expectation_results") or [] if isinstance(row, dict)]
    if not results or all(row.get("status") == "unknown" for row in results):
        return None
    required_failed = [row for row in results if row.get("required") and row.get("status") == "fail"]
    optional_failed = [row for row in results if not row.get("required") and row.get("status") == "fail"]
    if required_failed:
        score, triggered, check_id, reason = (
            50,
            required_failed,
            "required_expectations_failed",
            "evaluation.json 必需验收项 %s 状态为 fail，意图识别确定性封顶 50。",
        )
    elif optional_failed:
        score, triggered, check_id, reason = (
            70,
            optional_failed,
            "optional_expectations_failed",
            "evaluation.json 可选验收项 %s 状态为 fail，意图识别确定性封顶 70。",
        )
    else:
        return None

    dump = json.dumps(envelope, ensure_ascii=False, default=str)
    evidence: list[dict[str, Any]] = []
    for entry in triggered[:5]:
        quote = verbatim_quote(str(entry.get("id") or ""), dump) or verbatim_quote(str(entry.get("message") or ""), dump)
        if quote:
            evidence.append({"source": "evaluation.json", "quote": quote})
    if not evidence:
        # ``"status": "fail"`` appears verbatim under json.dumps' default
        # separators whenever a failing entry exists; it is the minimal
        # honest quote when neither id nor message survives verbatim matching.
        evidence.append({"source": "evaluation.json", "quote": '"status": "fail"'})
    ids = ", ".join(str(entry.get("id")) for entry in triggered[:5])
    return {
        "score": score,
        "summary": "Required expectation(s) failed in evaluation.json." if required_failed else "Optional expectation(s) failed in evaluation.json.",
        "recommendation": "Treat failed acceptance expectations as a cap on intent recognition; explain remaining ambiguity in the summary.",
        "rule_id": "intent_recognition_cap",
        "reason": reason % ids,
        "evidence": evidence,
        "checks": [
            {
                "id": check_id,
                "criterion": "evaluation.json 验收项中是否存在失败的必需/可选条目",
                "result": "fail",
                "conclusion": "验收项 %s 状态为 fail。" % ids,
                "type": "fact",
                "evidence": evidence,
                "counter_evidence": [],
            }
        ],
    }


def _valid_judge_evidence(report: dict[str, Any], reliability: dict[str, Any]) -> list[dict[str, Any]]:
    """Sample the judge's own evidence rows that passed verbatim validation."""
    invalid_keys = set()
    for row in reliability.get("invalid_evidence") or []:
        if isinstance(row, dict):
            invalid_keys.add((str(row.get("source") or ""), row.get("message_index"), str(row.get("quote") or "")))
    rows: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    def _collect(container: dict[str, Any]) -> None:
        for evidence in container.get("evidence") or []:
            if not isinstance(evidence, dict):
                continue
            key = (str(evidence.get("source") or ""), evidence.get("message_index"), str(evidence.get("quote") or ""))
            if key in invalid_keys or key in seen:
                continue
            seen.add(key)
            rows.append(evidence)

    for finding in report.get("findings") or []:
        if isinstance(finding, dict):
            _collect(finding)
    for dimension in (report.get("dimensions") or {}).values():
        if isinstance(dimension, dict):
            _collect(dimension)
    return rows


def _misquote_evidence(reliability: dict[str, Any], artifacts: _ArtifactQuotes, limit: int = 3) -> list[dict[str, Any]]:
    """Evidence for invalid references: the actual content of the cited rows."""
    evidence: list[dict[str, Any]] = []
    for row in reliability.get("invalid_evidence") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("source") or "") != "trajectory.jsonl":
            continue
        index = row.get("message_index")
        if not isinstance(index, int) or not (0 <= index < len(artifacts.trajectory)):
            continue
        actual = artifacts.trajectory[index]
        quote = artifacts.trajectory_evidence(actual, actual.get("content") or actual.get("role"))
        if quote and quote not in evidence:
            evidence.append(quote)
        if len(evidence) >= limit:
            break
    return evidence


def _artifact_availability_evidence(artifacts: _ArtifactQuotes) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = []
    marker = artifacts.audit_evidence('"tool_calls"')
    if marker:
        evidence.append(marker)
    if artifacts.trajectory:
        row = artifacts.trajectory[0]
        row_evidence = artifacts.trajectory_evidence(row, row.get("content") or row.get("role"))
        if row_evidence:
            evidence.append(row_evidence)
    return evidence
