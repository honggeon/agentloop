"""确定性重放校验（replay 车道入口，契约 C6）。

对一个已裁判的 run 目录重放确定性层并与已存 ``agent_judge.json`` 比对：

1. 重算 ``validate_judge_evidence``，比对 ``judge_reliability`` 的
   ``evidence_coverage`` / ``invalid_evidence_count``；
2. 重算 ``assess_dimensions``，比对 5 条确定性规则的
   ``score_trace.final_score``；
3. 重算 ``assess_run_quality``，比对 ``metrics.quality_score_cap``。

纯只读：不写 run 目录任何文件，无网络、无模型凭据。``source=judge`` 的
语义维度分数天然不可重放（需要重跑裁判），输出中显式标注为不在校验范围。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict, List

from .agents.dimensions import assess_dimensions
from .agents.evidence import validate_judge_evidence
from .agents.schemas import normalize_judge_report
from .agents.tools import assess_run_quality

#: 确定性规则覆盖的维度（auditrail/agents/dimensions.py 注册的 5 条规则）。
RULE_DIMENSIONS = ("tool_selection", "execution_closure", "error_recovery", "evidence_citation", "intent_recognition")

#: 确定性规则生成的 score_trace rule id，用于从已存报告中剥离确定性层
#: 叠加的内容，重建裁判原始提案视图。
_DETERMINISTIC_RULE_IDS = {
    "tool_selection_cap",
    "execution_closure_set",
    "error_recovery_cap",
    "evidence_citation_set",
    "intent_recognition_cap",
}

#: 确定性规则生成的 evidence_chain check id（同上）。
_DETERMINISTIC_CHECK_IDS = {
    "tool_failures_detected",
    "final_response_present",
    "failure_detected",
    "recovery_attempted",
    "recovery_verified",
    "judge_evidence_provided",
    "invalid_references",
    "evidence_coverage",
    "required_expectations_failed",
    "optional_expectations_failed",
}


def verify_run(run_path: str | Path) -> Dict[str, Any]:
    """重放一个已裁判 run 的确定性层并逐项比对已存报告。

    返回 ``{"run", "ok", "checks", "skipped"}``：``checks`` 每项为
    ``{"item", "status": match|mismatch, "expected", "actual"}``（expected 为
    报告中的已存值，actual 为重放值）；``skipped`` 每项为
    ``{"item", "reason"}``。``ok`` 为 True 当且仅当没有 mismatch。

    缺少 ``agent_judge.json`` 时抛 ``FileNotFoundError``（调用方负责转成
    明确的错误输出，而不是崩溃栈）。
    """
    run_dir = Path(run_path)
    report_path = run_dir / "agent_judge.json"
    if not report_path.exists():
        raise FileNotFoundError("missing %s; run judge-run first" % report_path)
    raw = json.loads(report_path.read_text(encoding="utf-8"))
    stored = normalize_judge_report(raw)

    checks: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []

    # 1. 证据可靠性：对已存报告整体重算（与写报告时的 Stage 2 同口径）。
    raw_reliability = raw.get("judge_reliability")
    if isinstance(raw_reliability, dict) and "evidence_coverage" in raw_reliability:
        replayed_reliability = validate_judge_evidence(run_dir, stored)
        stored_reliability = stored["judge_reliability"]
        for field in ("evidence_coverage", "invalid_evidence_count"):
            checks.append(
                _compare(
                    "judge_reliability.%s" % field,
                    stored_reliability.get(field),
                    replayed_reliability.get(field),
                )
            )
    else:
        for field in ("evidence_coverage", "invalid_evidence_count"):
            skipped.append(
                {
                    "item": "judge_reliability.%s" % field,
                    "reason": "报告无 judge_reliability（旧格式），跳过证据可靠性比对",
                }
            )

    # 2. 质量封顶：只依赖 run 产物，直接重算比对。
    raw_metrics = raw.get("metrics")
    if isinstance(raw_metrics, dict) and "quality_score_cap" in raw_metrics:
        quality = assess_run_quality(str(run_dir))
        checks.append(
            _compare(
                "metrics.quality_score_cap",
                (stored.get("metrics") or {}).get("quality_score_cap"),
                (quality.get("completion") or {}).get("score_cap"),
            )
        )
    else:
        quality = assess_run_quality(str(run_dir))
        skipped.append(
            {
                "item": "metrics.quality_score_cap",
                "reason": "报告无 metrics.quality_score_cap（旧格式），跳过质量封顶比对",
            }
        )

    # 3. 确定性维度规则：重建裁判原始提案视图后重放，比对 score_trace.final_score。
    judge_view = _judge_proposal_view(stored, quality)
    view_reliability = validate_judge_evidence(run_dir, judge_view)
    replayed = assess_dimensions(run_dir, judge_view, view_reliability)
    for key in RULE_DIMENSIONS:
        item = "dimensions.%s.score_trace.final_score" % key
        stored_trace = _score_trace((stored.get("dimensions") or {}).get(key))
        if stored_trace is None or stored_trace.get("final_score") is None:
            skipped.append(
                {
                    "item": item,
                    "reason": "报告无 score_trace（旧格式或规则未记录），跳过确定性比对",
                }
            )
            continue
        replayed_trace = _score_trace(replayed.get(key))
        actual = replayed_trace.get("final_score") if replayed_trace else None
        checks.append(_compare(item, stored_trace.get("final_score"), actual))

    # source=judge 的语义维度分数需要重跑裁判才能复核，不在确定性重放范围。
    for key, dimension in (stored.get("dimensions") or {}).items():
        if key in RULE_DIMENSIONS or not isinstance(dimension, dict):
            continue
        skipped.append(
            {
                "item": "dimensions.%s" % key,
                "reason": "source=judge，语义评分不可重放，不在校验范围",
            }
        )

    ok = all(check["status"] == "match" for check in checks)
    return {"run": str(run_dir), "ok": ok, "checks": checks, "skipped": skipped}


def _status_from_score(score: Any) -> str:
    """Standard score→status mapping (same bands as dimensions._status)."""
    value = int(score)
    if value >= 80:
        return "pass"
    if value >= 50:
        return "partial"
    return "fail"


def _compare(item: str, expected: Any, actual: Any) -> Dict[str, Any]:
    return {
        "item": item,
        "status": "match" if expected == actual else "mismatch",
        "expected": expected,
        "actual": actual,
    }


def _score_trace(dimension: Any) -> Dict[str, Any] | None:
    if not isinstance(dimension, dict):
        return None
    chain = dimension.get("evidence_chain")
    if not isinstance(chain, dict):
        return None
    trace = chain.get("score_trace")
    return trace if isinstance(trace, dict) else None


def _evidence_key(evidence: Any) -> tuple:
    row = evidence if isinstance(evidence, dict) else {}
    return (
        str(row.get("source") or ""),
        row.get("message_index") if isinstance(row.get("message_index"), int) else None,
        str(row.get("quote") or ""),
    )


def _judge_proposal_view(stored: Dict[str, Any], quality: Dict[str, Any]) -> Dict[str, Any]:
    """从已存报告重建裁判原始提案视图，供确定性层逐字重放。

    写报告时确定性层在裁判提案之上叠加了三类内容：quality finding、4 条
    规则写入的 score/trace 与合并进维度 evidence 的规则证据、以及规则
    改写的分数。逐项剥离（quality finding 按 (category, title) 匹配，规则
    证据按已知 rule/check id 匹配）后，重放 ``validate_judge_evidence`` /
    ``assess_dimensions`` 才与裁判评分时的输入等价。
    """
    view = copy.deepcopy(stored)
    quality_keys = {
        (finding.get("category"), finding.get("title"))
        for finding in quality.get("findings") or []
        if isinstance(finding, dict)
    }
    view["findings"] = [
        finding
        for finding in view.get("findings") or []
        if not isinstance(finding, dict) or (finding.get("category"), finding.get("title")) not in quality_keys
    ]
    for key in RULE_DIMENSIONS:
        dimension = (view.get("dimensions") or {}).get(key)
        if not isinstance(dimension, dict):
            continue
        chain = dimension.get("evidence_chain")
        if not isinstance(chain, dict):
            continue
        removed: List[tuple] = []
        kept_checks = []
        for check in chain.get("checks") or []:
            if isinstance(check, dict) and check.get("id") in _DETERMINISTIC_CHECK_IDS:
                for evidence in (check.get("evidence") or []) + (check.get("counter_evidence") or []):
                    removed.append(_evidence_key(evidence))
            else:
                kept_checks.append(check)
        trace = chain.get("score_trace")
        if isinstance(trace, dict):
            for rule in trace.get("rules") or []:
                if isinstance(rule, dict) and rule.get("id") in _DETERMINISTIC_RULE_IDS:
                    for evidence in rule.get("evidence") or []:
                        removed.append(_evidence_key(evidence))
            # 恢复裁判原始评分，供规则重放决定 set/cap/keep。旧格式 trace
            # 未记录 judge_status（为 null）但留有 judge_score：裁判显然给过
            # 分，按标准分数映射恢复 status，否则重放会把当时的 keep 错放成
            # set/cap，误报 mismatch。
            judge_status = trace.get("judge_status")
            judge_score = trace.get("judge_score")
            if judge_status is None and judge_score is not None:
                judge_status = _status_from_score(judge_score)
            if judge_status is None:
                dimension["score"] = None
                dimension["status"] = "unknown"
                dimension["source"] = "missing"
            else:
                dimension["score"] = judge_score
                dimension["status"] = judge_status
                dimension["source"] = "judge"
        if removed:
            removed_keys = set(removed)
            dimension["evidence"] = [
                evidence
                for evidence in dimension.get("evidence") or []
                if _evidence_key(evidence) not in removed_keys
            ]
        rebuilt: Dict[str, Any] = {}
        if kept_checks:
            rebuilt["checks"] = kept_checks
        if chain.get("gaps"):
            rebuilt["gaps"] = list(chain["gaps"])
        if rebuilt:
            dimension["evidence_chain"] = rebuilt
        else:
            dimension.pop("evidence_chain", None)
    return view


def render_verify_text(result: Dict[str, Any]) -> str:
    """人可读的比对清单（--json 之外的默认输出）。"""
    lines = ["verify-run: %s" % result["run"], ""]
    for check in result["checks"]:
        lines.append(
            "%-8s %s (stored=%r replayed=%r)"
            % (check["status"], check["item"], check["expected"], check["actual"])
        )
    for item in result["skipped"]:
        lines.append("%-8s %s (%s)" % ("skipped", item["item"], item["reason"]))
    lines.append("")
    lines.append("OK: all deterministic checks match" if result["ok"] else "MISMATCH: deterministic replay disagrees with the stored report")
    return "\n".join(lines)
