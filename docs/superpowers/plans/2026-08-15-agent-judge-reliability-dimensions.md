# Agent Judge Reliability Dimensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add evidence-grounded agent evaluation dimensions and judge reliability checks so Agentloop can show not only what the judge concluded, but how trustworthy that conclusion is.

**Architecture:** Keep the existing `agent_judge.json` contract backward compatible by adding `dimensions` and `judge_reliability` beside `findings`, `metrics`, and `charts`. Deterministic code validates evidence and applies a small set of downward caps; the DeepAgent judge still owns semantic dimensions. `write_judge_report` writes JSON, Markdown, and charts from the same normalized report. `assess_run_quality` remains the completion/deliverable cap and is not replaced by dimensions.

**Tech Stack:** Python 3.11+, FastAPI backend, Vue 3 + TypeScript frontend, pytest for backend tests, existing Agentloop JSON artifacts (`summary.json`, `audit.json`, `trajectory.jsonl`, `session.json`, `agent_judge.json`).

## Global Constraints

- Preserve compatibility with existing reports that only contain `overall_status`, `completion_score`, `confidence`, `summary`, `findings`, `metrics`, and `charts`.
- Do not require DeepAgent/model credentials for deterministic schema, evidence validation, Markdown rendering, or report-writing tests.
- Treat deterministic facts as stronger than LLM judge output: invalid or missing evidence must affect `judge_reliability`. Deterministic code may write scores only for directly observable dimensions (`evidence_citation`, `execution_closure`, and failure-only `error_recovery`) and may cap judge scores downward for failed-tool `tool_selection`; it must not upgrade semantic-only dimensions.
- Every `fact` finding must have at least one valid evidence reference or be counted as unsupported.
- Do not replace current `findings`; add dimensions as a structured evaluation view.
- `unknown` is a first-class status. Default dimensions use `status: "unknown"` and `source: "missing"`. UI, Markdown, and charts must not present unknown as fail.
- This round's deterministic assessor only writes or caps: `evidence_citation`, `execution_closure`, `error_recovery` when a failure actually occurred, and a failure cap on `tool_selection`. Do not auto-pass `state_tracking`, `parameter_generation`, `safety_permission`, `efficiency`, or `error_recovery` when no recovery situation occurred.
- `assess_run_quality` still owns completion score caps and missing-deliverable findings. Dimensions must not override `quality_score_cap` or duplicate delivery findings.
- `judge_reliability` must keep `invalid_evidence` detail through `normalize_judge_report`. Notes from the validator are appended to, not replaced by, judge-provided notes.
- `agent_judge.md` is written by `render_judge_markdown` in `agentloop/agents/schemas.py`. Backend export must reuse those helpers, not a second incompatible renderer.
- LiveEvalView is out of scope. Only `RunView.vue` gains the new panels.
- Keep the first implementation intentionally narrow. Do not add new chart components, new routes, database persistence, historical calibration UI, or golden-set management in this plan.
- This directory is a git repository. Commit after each task. Do not push.

---

## File Structure

- `agentloop/agents/schemas.py`
  - Normalize `dimensions` and `judge_reliability`, including `invalid_evidence`.
  - Add stable dimension IDs, status/source enums, and source aliases for evidence.
  - Extend `render_judge_markdown` so `agent_judge.md` includes dimensions and reliability.
  - Export Markdown helpers the backend can reuse.

- `agentloop/agents/evidence.py`
  - New deterministic evidence validator.
  - Loads `summary.json`, `audit.json`, `trajectory.jsonl`, and optional `session.json`.
  - Rejects missing source, unknown source, missing quote, quote-not-found, and bad `message_index`.
  - Maps source aliases (`trajectory`, `agent`, `audit`, `summary`, `session`) onto artifact filenames.

- `agentloop/agents/dimensions.py`
  - New deterministic dimension assessor.
  - Always scores `evidence_citation` from reliability.
  - Scores `execution_closure` and `error_recovery` from trajectory/audit.
  - Leaves `error_recovery` as unknown when no tool failure occurred.
  - Caps `tool_selection` downward when tool calls failed; does not auto-pass on success.
  - Leaves semantic-only dimensions as judge or `unknown`.

- `agentloop/agents/tools.py`
  - Validate judge evidence before quality merge, then merge quality and compute dimensions.
  - Append validator notes; keep `invalid_evidence`.
  - Write `charts.dimension_scores` and `charts.judge_reliability`.

- `agentloop/agents/prompts.py`
  - Require DeepAgent output to include the agreed dimensions and evidence.
  - Tell the judge that deterministic code will recompute reliability and may cap some scores.

- `backend/app/routes/runs.py`
  - Reuse schema Markdown helpers in the export path.

- `frontend/src/api.ts`
  - Add TypeScript types for dimensions, evidence, and judge reliability.

- `frontend/src/views/RunView.vue`
  - Display Chinese dimension labels, unknown vs fail styling, and reliability metrics.

- Tests:
  - `tests/test_agent_schemas.py`
  - `tests/test_agent_evidence.py`
  - `tests/test_agent_dimensions.py`
  - `tests/test_agent_tools.py`
  - `tests/test_agent_graph.py`
  - `tests/test_backend_routes.py`
  - `tests/test_frontend_app_state.py`

---

### Task 1: Normalize Evaluation Dimensions in the Report Schema

**Files:**
- Modify: `agentloop/agents/schemas.py`
- Test: `tests/test_agent_schemas.py`

**Interfaces:**
- Produces: `DIMENSION_DEFINITIONS: dict[str, str]`
- Produces: `ALLOWED_DIMENSION_STATUS = {"pass", "partial", "fail", "unknown"}`
- Produces: `ALLOWED_DIMENSION_SOURCE = {"deterministic", "judge", "deterministic+judge", "missing"}`
- Produces: `EVIDENCE_SOURCE_ALIASES: dict[str, str]`
- Produces normalized `dimensions: dict[str, dict[str, Any]]` with `score`, `status`, `summary`, `evidence`, `recommendation`, `source`
- Produces normalized `judge_reliability` with `evidence_coverage`, `unsupported_claim_count`, `invalid_evidence_count`, `fact_inference_mismatch_count`, `valid_evidence_count`, `total_evidence_count`, `notes`, `invalid_evidence`

- [ ] **Step 1: Add failing tests for dimension defaults and reliability detail**

Add to `tests/test_agent_schemas.py`:

```python
def test_normalize_judge_report_adds_default_dimensions_and_reliability():
    report = normalize_judge_report(
        {
            "overall_status": "success",
            "completion_score": 90,
            "confidence": "high",
            "summary": "ok",
            "findings": [],
        }
    )

    assert "intent_recognition" in report["dimensions"]
    assert "verification" in report["dimensions"]
    assert report["dimensions"]["intent_recognition"] == {
        "score": 0,
        "status": "unknown",
        "summary": "",
        "evidence": [],
        "recommendation": "",
        "source": "missing",
    }
    assert report["judge_reliability"]["evidence_coverage"] == 0
    assert report["judge_reliability"]["unsupported_claim_count"] == 0
    assert report["judge_reliability"]["invalid_evidence"] == []
    assert report["charts"]["dimension_scores"][0]["dimension"] == "intent_recognition"
    assert report["charts"]["dimension_scores"][0]["label"] == "意图识别"
    assert report["charts"]["dimension_scores"][0]["status"] == "unknown"
```

- [ ] **Step 2: Add failing tests for aliases, score bounds, and invalid_evidence preservation**

Add to `tests/test_agent_schemas.py`:

```python
def test_normalize_judge_report_maps_dimension_aliases_and_bounds_scores():
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "completion_score": 60,
            "confidence": "medium",
            "summary": "x",
            "dimensions": {
                "意图识别": {"score": 120, "status": "通过", "summary": "识别正确", "source": "judge"},
                "工具选择": {"score": -3, "status": "失败", "summary": "工具错了", "source": "deterministic"},
            },
            "judge_reliability": {
                "evidence_coverage": 0.5,
                "unsupported_claim_count": 1,
                "invalid_evidence_count": 1,
                "fact_inference_mismatch_count": 2,
                "valid_evidence_count": 1,
                "total_evidence_count": 2,
                "notes": ["one invalid quote"],
                "invalid_evidence": [{"location": "findings.0", "reason": "quote_not_found", "quote": "nope"}],
            },
        }
    )

    assert report["dimensions"]["intent_recognition"]["score"] == 100
    assert report["dimensions"]["intent_recognition"]["status"] == "pass"
    assert report["dimensions"]["tool_selection"]["score"] == 0
    assert report["dimensions"]["tool_selection"]["status"] == "fail"
    assert report["judge_reliability"]["invalid_evidence"][0]["reason"] == "quote_not_found"
    assert report["judge_reliability"]["fact_inference_mismatch_count"] == 2
```

- [ ] **Step 3: Run tests and verify failure**

Run: `python3 -m pytest tests/test_agent_schemas.py -q`

Expected: FAIL because `dimensions` and `judge_reliability` are not normalized yet.

- [ ] **Step 4: Implement schema constants and normalizers**

In `agentloop/agents/schemas.py`, add near the existing allowed constants:

```python
DIMENSION_DEFINITIONS = {
    "intent_recognition": "意图识别",
    "task_planning": "任务规划",
    "tool_selection": "工具选择",
    "parameter_generation": "参数生成",
    "evidence_citation": "证据引用",
    "state_tracking": "状态更新",
    "final_response": "最终表达",
    "context_management": "上下文管理",
    "execution_closure": "执行闭环",
    "error_recovery": "错误恢复",
    "verification": "验证能力",
    "safety_permission": "安全与权限",
    "efficiency": "成本与效率",
}
ALLOWED_DIMENSION_STATUS = {"pass", "partial", "fail", "unknown"}
ALLOWED_DIMENSION_SOURCE = {"deterministic", "judge", "deterministic+judge", "missing"}
EVIDENCE_SOURCE_ALIASES = {
    "trajectory": "trajectory.jsonl",
    "trajectory.jsonl": "trajectory.jsonl",
    "agent": "trajectory.jsonl",
    "messages": "trajectory.jsonl",
    "audit": "audit.json",
    "audit.json": "audit.json",
    "tools": "audit.json",
    "summary": "summary.json",
    "summary.json": "summary.json",
    "session": "session.json",
    "session.json": "session.json",
}
```

Add normalizer helpers:

```python
def _normalize_dimensions(value: object) -> Dict[str, Dict[str, Any]]:
    raw = value if isinstance(value, dict) else {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for key in DIMENSION_DEFINITIONS:
        item = raw.get(key)
        if item is None:
            item = raw.get(DIMENSION_DEFINITIONS[key])
        normalized[key] = _normalize_dimension(item)
    return normalized


def _dimension_status(value: object) -> str:
    text = str(value or "unknown").strip().lower()
    text = {
        "通过": "pass",
        "合格": "pass",
        "部分通过": "partial",
        "失败": "fail",
        "不通过": "fail",
        "未知": "unknown",
    }.get(text, text)
    return _choice(text, ALLOWED_DIMENSION_STATUS, "unknown", "dimension.status")


def _dimension_source(value: object) -> str:
    text = str(value or "missing").strip().lower()
    text = {"规则": "deterministic", "确定性": "deterministic", "模型": "judge", "评审": "judge"}.get(text, text)
    return _choice(text, ALLOWED_DIMENSION_SOURCE, "missing", "dimension.source")


def _normalize_dimension(value: object) -> Dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    evidence = item.get("evidence") or []
    if isinstance(evidence, dict):
        evidence = [evidence]
    elif isinstance(evidence, str):
        evidence = [{"source": "agent", "quote": evidence}]
    if not isinstance(evidence, list):
        evidence = []
    normalized_evidence: List[Dict[str, Any]] = []
    for row in evidence:
        normalized_evidence.extend(_normalize_evidence(row))
    return {
        "score": _score(item.get("score", 0)),
        "status": _dimension_status(item.get("status")),
        "summary": str(item.get("summary") or ""),
        "evidence": normalized_evidence,
        "recommendation": str(item.get("recommendation") or ""),
        "source": _dimension_source(item.get("source")),
    }


def _normalize_judge_reliability(value: object) -> Dict[str, Any]:
    item = value if isinstance(value, dict) else {}
    total = max(0, int(item.get("total_evidence_count") or 0))
    valid = max(0, int(item.get("valid_evidence_count") or 0))
    coverage = item.get("evidence_coverage")
    if coverage is None:
        coverage = round(valid / total, 4) if total else 0
    notes = item.get("notes") or []
    if isinstance(notes, str):
        notes = [notes]
    if not isinstance(notes, list):
        notes = []
    invalid = item.get("invalid_evidence") or []
    if not isinstance(invalid, list):
        invalid = []
    return {
        "evidence_coverage": max(0, min(1, float(coverage))),
        "unsupported_claim_count": max(0, int(item.get("unsupported_claim_count") or 0)),
        "invalid_evidence_count": max(0, int(item.get("invalid_evidence_count") or 0)),
        "fact_inference_mismatch_count": max(0, int(item.get("fact_inference_mismatch_count") or 0)),
        "valid_evidence_count": valid,
        "total_evidence_count": total,
        "notes": [str(note) for note in notes if str(note).strip()],
        "invalid_evidence": [row for row in invalid if isinstance(row, dict)],
    }
```

Update `_normalize_evidence` so aliased sources become artifact filenames:

```python
source = str(item.get("source") or "")
aliased = EVIDENCE_SOURCE_ALIASES.get(source.strip().lower(), source)
base = {"source": aliased, "quote": str(item.get("quote") or "")[:500]}
```

Keep the existing `message_index` expansion behavior.

Update `normalize_judge_report` to include:

```python
dimensions = _normalize_dimensions(report.get("dimensions"))
judge_reliability = _normalize_judge_reliability(report.get("judge_reliability"))
charts["dimension_scores"] = [
    {
        "dimension": key,
        "label": DIMENSION_DEFINITIONS[key],
        "score": row["score"],
        "status": row["status"],
        "source": row["source"],
    }
    for key, row in dimensions.items()
]
```

Return `dimensions` and `judge_reliability` in the normalized report. Do not add `"失败": "fail"` to the shared `_CHOICE_SYNONYMS`; overall_status must keep `"失败": "failed"`.

- [ ] **Step 5: Run schema tests**

Run: `python3 -m pytest tests/test_agent_schemas.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agentloop/agents/schemas.py tests/test_agent_schemas.py
git commit -m "$(cat <<'EOF'
Add judge dimension and reliability schema defaults.

EOF
)"
```

---

### Task 2: Add Deterministic Evidence Validation

**Files:**
- Create: `agentloop/agents/evidence.py`
- Test: `tests/test_agent_evidence.py`

**Interfaces:**
- Consumes: normalized report from `normalize_judge_report(raw) -> dict`
- Produces: `validate_judge_evidence(run_path: str | Path, report: dict[str, Any]) -> dict[str, Any]`
- Metric meanings:
  - `unsupported_claim_count`: `type=fact` findings with an empty evidence list
  - `invalid_evidence_count`: evidence rows that fail source/quote/`message_index` checks
  - `fact_inference_mismatch_count`: `type=fact` findings with zero valid evidence after validation (empty or all invalid). This is not a copy of `unsupported_claim_count`.
  - Empty `quote` is invalid (`missing_quote`), never valid coverage.

- [ ] **Step 1: Write failing tests for valid, invalid, empty-quote, and alias evidence**

Create `tests/test_agent_evidence.py`:

```python
import json

from agentloop.agents.evidence import validate_judge_evidence
from agentloop.agents.schemas import normalize_judge_report


def _write_evidence_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"title": "生成报告"}, ensure_ascii=False), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps({"tool_calls": [{"index": 1, "tool": "terminal", "status": "error", "result_preview": "timeout"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    rows = [
        {"index": 0, "role": "user", "content": "请生成报告"},
        {"index": 1, "role": "tool", "content": "timeout"},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return run


def test_validate_judge_evidence_counts_valid_quotes_and_aliases(tmp_path):
    run = _write_evidence_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "工具超时",
            "findings": [
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "工具超时",
                    "type": "fact",
                    "evidence": [{"source": "trajectory", "message_index": 1, "quote": "timeout"}],
                    "recommendation": "重试或换工具",
                }
            ],
        }
    )

    reliability = validate_judge_evidence(run, report)

    assert reliability["valid_evidence_count"] == 1
    assert reliability["total_evidence_count"] == 1
    assert reliability["evidence_coverage"] == 1
    assert reliability["invalid_evidence_count"] == 0
    assert reliability["unsupported_claim_count"] == 0
    assert reliability["fact_inference_mismatch_count"] == 0


def test_validate_judge_evidence_flags_missing_quote_empty_quote_and_unsupported_fact(tmp_path):
    run = _write_evidence_run(tmp_path)
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "x",
            "findings": [
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "无证据事实",
                    "type": "fact",
                    "evidence": [],
                    "recommendation": "补证据",
                },
                {
                    "severity": "medium",
                    "category": "context",
                    "title": "引用不存在内容",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "not in row"}],
                    "recommendation": "修正引用",
                },
                {
                    "severity": "low",
                    "category": "tool",
                    "title": "空引用",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 1, "quote": ""}],
                    "recommendation": "补 quote",
                },
            ],
        }
    )

    reliability = validate_judge_evidence(run, report)

    assert reliability["unsupported_claim_count"] == 1
    assert reliability["invalid_evidence_count"] == 2
    assert reliability["fact_inference_mismatch_count"] == 3
    assert reliability["evidence_coverage"] == 0
    reasons = {row["reason"] for row in reliability["invalid_evidence"]}
    assert reasons == {"quote_not_found", "missing_quote"}
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python3 -m pytest tests/test_agent_evidence.py -q`

Expected: FAIL because `agentloop.agents.evidence` does not exist.

- [ ] **Step 3: Implement evidence validator**

Create `agentloop/agents/evidence.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import EVIDENCE_SOURCE_ALIASES


def validate_judge_evidence(run_path: str | Path, report: dict[str, Any]) -> dict[str, Any]:
    artifacts = _load_artifacts(Path(run_path))
    evidence_rows = _collect_evidence(report)
    invalid = []
    valid_count = 0
    valid_by_location: dict[str, int] = {}

    for location, evidence in evidence_rows:
        result = _validate_one_evidence(evidence, artifacts)
        if result is None:
            valid_count += 1
            valid_by_location[location] = valid_by_location.get(location, 0) + 1
        else:
            invalid.append({"location": location, **evidence, "reason": result})

    fact_rows = _collect_fact_claims(report)
    unsupported = 0
    mismatch = 0
    for location, item in fact_rows:
        has_evidence = bool(item.get("evidence"))
        if not has_evidence:
            unsupported += 1
            mismatch += 1
            continue
        if valid_by_location.get(location, 0) == 0:
            mismatch += 1

    total = len(evidence_rows)
    notes = []
    if unsupported:
        notes.append("%s fact claims have no evidence" % unsupported)
    if invalid:
        notes.append("%s evidence references are invalid" % len(invalid))
    if mismatch:
        notes.append("%s fact claims have no valid evidence" % mismatch)

    return {
        "evidence_coverage": round(valid_count / total, 4) if total else 0,
        "unsupported_claim_count": unsupported,
        "invalid_evidence_count": len(invalid),
        "fact_inference_mismatch_count": mismatch,
        "valid_evidence_count": valid_count,
        "total_evidence_count": total,
        "notes": notes,
        "invalid_evidence": invalid,
    }
```

Implement helpers in the same file:

```python
def _load_artifacts(run_dir: Path) -> dict[str, Any]:
    artifacts: dict[str, Any] = {}
    for name in ("summary.json", "audit.json", "session.json"):
        path = run_dir / name
        if path.exists():
            artifacts[name] = json.loads(path.read_text(encoding="utf-8"))
    trajectory_path = run_dir / "trajectory.jsonl"
    if trajectory_path.exists():
        artifacts["trajectory.jsonl"] = [
            json.loads(line) for line in trajectory_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
    return artifacts


def _collect_evidence(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for index, finding in enumerate(report.get("findings") or []):
        if isinstance(finding, dict):
            for evidence in finding.get("evidence") or []:
                if isinstance(evidence, dict):
                    rows.append(("findings.%s" % index, evidence))
    for key, dimension in (report.get("dimensions") or {}).items():
        if isinstance(dimension, dict):
            for evidence in dimension.get("evidence") or []:
                if isinstance(evidence, dict):
                    rows.append(("dimensions.%s" % key, evidence))
    return rows


def _collect_fact_claims(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rows: list[tuple[str, dict[str, Any]]] = []
    for index, finding in enumerate(report.get("findings") or []):
        if isinstance(finding, dict) and finding.get("type") == "fact":
            rows.append(("findings.%s" % index, finding))
    return rows


def _canonical_source(source: str) -> str:
    return EVIDENCE_SOURCE_ALIASES.get(str(source or "").strip().lower(), str(source or ""))


def _validate_one_evidence(evidence: dict[str, Any], artifacts: dict[str, Any]) -> str | None:
    source = _canonical_source(str(evidence.get("source") or ""))
    quote = str(evidence.get("quote") or "").strip()
    if not source:
        return "missing_source"
    if source not in artifacts:
        return "source_not_found"
    if not quote:
        return "missing_quote"
    artifact = artifacts[source]
    if source == "trajectory.jsonl" and evidence.get("message_index") is not None:
        row = _find_trajectory_row(artifact, int(evidence["message_index"]))
        if row is None:
            return "message_index_not_found"
        haystack = json.dumps(row, ensure_ascii=False, default=str)
    else:
        haystack = json.dumps(artifact, ensure_ascii=False, default=str)
    if quote not in haystack:
        return "quote_not_found"
    return None


def _find_trajectory_row(rows: Any, message_index: int) -> dict[str, Any] | None:
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("index") == message_index:
            return row
    if 0 <= message_index < len(rows) and isinstance(rows[message_index], dict):
        return rows[message_index]
    return None
```

- [ ] **Step 4: Run evidence tests**

Run: `python3 -m pytest tests/test_agent_evidence.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentloop/agents/evidence.py tests/test_agent_evidence.py
git commit -m "$(cat <<'EOF'
Validate judge evidence against run artifacts.

EOF
)"
```

---

### Task 3: Add Deterministic Dimension Assessment

**Files:**
- Create: `agentloop/agents/dimensions.py`
- Test: `tests/test_agent_dimensions.py`

**Interfaces:**
- Consumes: `validate_judge_evidence(run_path, report) -> dict`
- Produces: `assess_dimensions(run_path: str | Path, report: dict[str, Any], reliability: dict[str, Any]) -> dict[str, dict[str, Any]]`
- Deterministic this round:
  - `evidence_citation`: from reliability; no evidence or any invalid evidence is fail/0
  - `execution_closure`: final assistant text plus `overall_status`
  - `error_recovery`: failed tools plus retry/additional-call signals; stays unknown when no failure occurred
  - `tool_selection`: downward cap only when a tool failed; success must not become an automatic pass
- Leave `intent_recognition`, `task_planning`, `parameter_generation`, `state_tracking`, `final_response`, `context_management`, `verification`, `safety_permission`, and `efficiency` as judge or `unknown`.

- [ ] **Step 1: Write failing tests for caps, unknown defaults, and evidence citation**

Create `tests/test_agent_dimensions.py`:

```python
import json

from agentloop.agents.dimensions import assess_dimensions
from agentloop.agents.evidence import validate_judge_evidence
from agentloop.agents.schemas import normalize_judge_report


def _write_tool_run(tmp_path, status="ok"):
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"message_count": 3, "tool_call_count": 1}), encoding="utf-8")
    (run / "audit.json").write_text(
        json.dumps(
            {
                "message_count": 3,
                "tool_call_count": 1,
                "tool_result_count": 1,
                "tool_calls": [{"index": 1, "tool": "search", "status": status, "args_preview": '{"q":"agent"}'}],
                "findings": [],
            }
        ),
        encoding="utf-8",
    )
    rows = [
        {"index": 0, "role": "user", "content": "查一下 agent 评测"},
        {"index": 1, "role": "assistant", "content": "调用搜索工具"},
        {"index": 2, "role": "assistant", "content": "最终回答"},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return run


def test_assess_dimensions_keeps_semantic_unknown_and_does_not_auto_pass_tool_selection(tmp_path):
    run = _write_tool_run(tmp_path)
    report = normalize_judge_report({"overall_status": "success", "completion_score": 90, "confidence": "high", "summary": "ok"})
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    assert dimensions["tool_selection"]["status"] == "unknown"
    assert dimensions["state_tracking"]["status"] == "unknown"
    assert dimensions["safety_permission"]["status"] == "unknown"
    assert dimensions["efficiency"]["status"] == "unknown"
    assert dimensions["execution_closure"]["status"] == "pass"
    assert dimensions["error_recovery"]["status"] == "unknown"
    assert dimensions["evidence_citation"]["status"] == "fail"


def test_assess_dimensions_caps_tool_selection_and_invalid_evidence(tmp_path):
    run = _write_tool_run(tmp_path, status="error")
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 40,
            "confidence": "medium",
            "summary": "x",
            "dimensions": {
                "tool_selection": {"score": 90, "status": "pass", "source": "judge"},
                "evidence_citation": {
                    "score": 90,
                    "status": "pass",
                    "source": "judge",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "missing quote"}],
                },
            },
        }
    )
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    assert dimensions["tool_selection"]["status"] == "fail"
    assert dimensions["tool_selection"]["score"] < 90
    assert dimensions["error_recovery"]["status"] in {"partial", "fail"}
    assert dimensions["evidence_citation"]["status"] == "fail"
    assert dimensions["evidence_citation"]["score"] == 0
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python3 -m pytest tests/test_agent_dimensions.py -q`

Expected: FAIL because `agentloop.agents.dimensions` does not exist.

- [ ] **Step 3: Implement deterministic dimension assessor**

Create `agentloop/agents/dimensions.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .schemas import normalize_judge_report


def assess_dimensions(run_path: str | Path, report: dict[str, Any], reliability: dict[str, Any]) -> dict[str, dict[str, Any]]:
    run_dir = Path(run_path)
    normalized = normalize_judge_report(report)
    dimensions = {key: dict(value) for key, value in normalized["dimensions"].items()}
    audit = _read_json(run_dir / "audit.json")
    trajectory = _read_jsonl(run_dir / "trajectory.jsonl")

    _cap_dimension(dimensions, "tool_selection", *_tool_selection_cap(audit))
    _set_dimension(dimensions, "execution_closure", *_execution_closure_score(trajectory, normalized))
    _cap_dimension(dimensions, "error_recovery", *_error_recovery_score(audit, trajectory))
    _set_dimension(dimensions, "evidence_citation", *_evidence_citation_score(reliability))
    return dimensions
```

Add helpers:

```python
def _set_dimension(dimensions: dict[str, dict[str, Any]], key: str, score: int, status: str, summary: str, recommendation: str) -> None:
    current = dimensions.get(key) or {}
    judge_score = int(current.get("score") or 0)
    if current.get("source") == "judge" and judge_score < score:
        return
    source = "deterministic" if current.get("source") in {"missing", None, ""} else "deterministic+judge"
    dimensions[key] = {
        "score": score,
        "status": status,
        "summary": summary,
        "evidence": current.get("evidence") or [],
        "recommendation": recommendation,
        "source": source,
    }


def _cap_dimension(dimensions: dict[str, dict[str, Any]], key: str, score: int | None, status: str, summary: str, recommendation: str) -> None:
    if score is None:
        return
    current = dimensions.get(key) or {}
    current_score = int(current.get("score") or 0)
    if current.get("source") in {"missing", None, ""}:
        dimensions[key] = {
            "score": score,
            "status": status,
            "summary": summary,
            "evidence": current.get("evidence") or [],
            "recommendation": recommendation,
            "source": "deterministic",
        }
        return
    if current_score <= score:
        return
    dimensions[key] = {
        "score": score,
        "status": status,
        "summary": summary,
        "evidence": current.get("evidence") or [],
        "recommendation": recommendation,
        "source": "deterministic+judge" if current.get("source") == "judge" else "deterministic",
    }


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
```

Add scoring rules:

```python
def _tool_selection_cap(audit: dict[str, Any]) -> tuple[int | None, str, str, str]:
    calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    if not calls:
        return None, "unknown", "No tool calls were captured.", "Leave tool selection to the judge when no tools ran."
    failed = sum(1 for call in calls if str(call.get("status") or "").lower() in {"error", "failed", "missing"})
    if not failed:
        return None, "unknown", "Tool calls succeeded; selection quality is not determined by success rate.", "Keep judge evidence for whether the chosen tools were appropriate."
    score = round(100 * (1 - failed / len(calls)))
    return score, _status(score), "%s/%s tool calls failed." % (failed, len(calls)), "Investigate failed or unnecessary tool calls."


def _execution_closure_score(trajectory: list[dict[str, Any]], report: dict[str, Any]) -> tuple[int, str, str, str]:
    has_final = any(str(row.get("role") or "") == "assistant" and str(row.get("content") or "").strip() for row in trajectory[-3:])
    if report.get("overall_status") == "success" and has_final:
        return 90, "pass", "Run has a final assistant response and success status.", "Verify claimed deliverables when present."
    if has_final:
        return 65, "partial", "Run has a final assistant response but did not fully succeed.", "Tie final response to explicit completion checks."
    return 20, "fail", "No final assistant response was captured.", "Require final response before closing a run."


def _error_recovery_score(audit: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[int | None, str, str, str]:
    calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    failed = [call for call in calls if str(call.get("status") or "").lower() in {"error", "failed", "missing"}]
    if not failed:
        return None, "unknown", "No tool failures were captured.", "Error recovery is not applicable for this run."
    retry_text = json.dumps(trajectory, ensure_ascii=False).lower()
    recovered = "retry" in retry_text or "重试" in retry_text or len(calls) > len(failed)
    if recovered:
        return 60, "partial", "Failures were followed by retry or additional action.", "Make recovery outcome explicit in the final response."
    return 30, "fail", "Failures were captured without clear recovery.", "Add retry, fallback, or explicit blocked state handling."


def _evidence_citation_score(reliability: dict[str, Any]) -> tuple[int, str, str, str]:
    if reliability.get("total_evidence_count", 0) == 0:
        return 0, "fail", "No judge evidence was provided.", "Require evidence for findings and semantic dimension scores."
    if reliability.get("invalid_evidence_count", 0):
        return 0, "fail", "Invalid evidence references were found.", "Fix source, message_index, or quote references."
    score = round(float(reliability.get("evidence_coverage") or 0) * 100)
    return score, _status(score), "%s/%s evidence references are valid." % (reliability.get("valid_evidence_count", 0), reliability.get("total_evidence_count", 0)), "Maintain quote-level evidence validation."
```

- [ ] **Step 4: Run dimension tests**

Run: `python3 -m pytest tests/test_agent_dimensions.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agentloop/agents/dimensions.py tests/test_agent_dimensions.py
git commit -m "$(cat <<'EOF'
Score only evidence-backed deterministic dimensions.

EOF
)"
```

---

### Task 4: Merge Reliability and Dimensions During Report Writing

**Files:**
- Modify: `agentloop/agents/tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `validate_judge_evidence(run_path, report) -> dict`
- Consumes: `assess_dimensions(run_path, report, reliability) -> dict`
- Produces `agent_judge.json` with `dimensions`, `judge_reliability` (including `invalid_evidence`), and `charts.dimension_scores`.
- Validates judge evidence before `_merge_quality_report`, so `judge_reliability` measures only the judge output and is not polluted by `assess_run_quality` system findings.
- Merges quality after evidence validation. Quality still owns `overall_status`, `completion_score`, `quality_score_cap`, deliverable metrics, and delivery findings.
- Appends validator notes to any existing judge-provided notes.

- [ ] **Step 1: Add failing report-writing test**

Add to `tests/test_agent_tools.py`:

```python
def test_write_judge_report_adds_dimensions_and_reliability(tmp_path):
    run = _write_run(tmp_path)
    write_judge_report(
        str(run),
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "工具失败",
            "findings": [
                {
                    "severity": "high",
                    "category": "tool",
                    "title": "terminal failed",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 1, "quote": "ModuleNotFoundError"}],
                    "recommendation": "fix dependency",
                }
            ],
        },
    )

    report = json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))
    markdown = (run / "agent_judge.md").read_text(encoding="utf-8")
    charts = json.loads((run / "chart_data.json").read_text(encoding="utf-8"))

    assert "tool_selection" in report["dimensions"]
    assert report["judge_reliability"]["valid_evidence_count"] == 1
    assert report["judge_reliability"]["invalid_evidence_count"] == 0
    assert report["judge_reliability"]["invalid_evidence"] == []
    assert any(finding["title"] == "Requested deliverable was not produced" for finding in report["findings"])
    assert any(row["dimension"] == "tool_selection" for row in report["charts"]["dimension_scores"])
    assert any(row["dimension"] == "tool_selection" for row in charts["dimension_scores"])
    assert "### Evaluation Dimensions" in markdown
    assert "### Judge Reliability" in markdown
```

Existing `_write_run` already writes a trajectory row with `ModuleNotFoundError` at index 1. It also contains `make ppt`, which may trigger `assess_run_quality` delivery findings; the reliability assertion must still stay at zero invalid evidence because reliability is computed before quality findings are merged.

This proves quality findings can still be merged into the final report without being counted as judge evidence failures.

- [ ] **Step 2: Run test and verify failure**

Run: `python3 -m pytest tests/test_agent_tools.py::test_write_judge_report_adds_dimensions_and_reliability -q`

Expected: FAIL because report writing does not merge reliability or dimensions, and `render_judge_markdown` does not yet emit the new sections.

- [ ] **Step 3: Update `write_judge_report`**

In `agentloop/agents/tools.py`, import:

```python
from .dimensions import assess_dimensions
from .evidence import validate_judge_evidence
from .schemas import DIMENSION_DEFINITIONS, normalize_judge_report, render_judge_markdown
```

Inside `write_judge_report`, validate the normalized judge report before quality merge:

```python
    reliability = validate_judge_evidence(str(run_dir), normalized)
    existing_notes = list((normalized.get("judge_reliability") or {}).get("notes") or [])
    merged_notes = existing_notes + [note for note in reliability.get("notes") or [] if note not in existing_notes]
    reliability["notes"] = merged_notes
```

Then merge quality and add reliability/dimensions:

```python
    quality = assess_run_quality(str(run_dir))
    normalized = _merge_quality_report(normalized, quality)
    normalized["judge_reliability"] = reliability
    normalized["dimensions"] = assess_dimensions(str(run_dir), normalized, reliability)
    normalized = normalize_judge_report(normalized)
```

Do not validate evidence again after `_merge_quality_report`; quality findings are system-generated checks, not judge claims.

After chart merge:

```python
    charts["dimension_scores"] = [
        {
            "dimension": key,
            "label": DIMENSION_DEFINITIONS[key],
            "score": value["score"],
            "status": value["status"],
            "source": value["source"],
        }
        for key, value in normalized.get("dimensions", {}).items()
    ]
    charts["judge_reliability"] = {
        "evidence_coverage": normalized["judge_reliability"]["evidence_coverage"],
        "unsupported_claim_count": normalized["judge_reliability"]["unsupported_claim_count"],
        "invalid_evidence_count": normalized["judge_reliability"]["invalid_evidence_count"],
        "fact_inference_mismatch_count": normalized["judge_reliability"]["fact_inference_mismatch_count"],
    }
```

Do not drop `invalid_evidence` from `normalized["judge_reliability"]`. `normalize_judge_report` from Task 1 must keep it.

- [ ] **Step 4: Extend `render_judge_markdown` in `schemas.py`**

This is required in this task because `write_judge_report` writes `agent_judge.md` through `render_judge_markdown`, not the backend export helper.

Replace the current findings-only Markdown with helpers used by both the file writer and backend export:

```python
def render_judge_markdown(report: Dict[str, Any]) -> str:
    normalized = normalize_judge_report(report)
    lines = [
        "# Agentloop Agent Judge",
        "",
        "## Summary",
        "",
        "- status: `%s`" % normalized["overall_status"],
        "- completion_score: `%s`" % normalized["completion_score"],
        "- confidence: `%s`" % normalized["confidence"],
        "",
        normalized["summary"] or "No summary provided.",
        "",
        render_dimensions_markdown(normalized.get("dimensions")),
        "",
        render_reliability_markdown(normalized.get("judge_reliability")),
        "",
        "## Findings",
        "",
    ]
    findings = normalized.get("findings") or []
    if not findings:
        lines.append("- No findings.")
    for item in findings:
        lines.append(
            "- **%s** `%s/%s/%s`: %s"
            % (item["severity"], item["category"], item["type"], item["title"], item["recommendation"])
        )
        for evidence in item.get("evidence") or []:
            source = evidence.get("source", "")
            index = evidence.get("message_index")
            quote = evidence.get("quote", "")
            label = "%s#%s" % (source, index) if index is not None else source
            lines.append("  - evidence `%s`: %s" % (label, quote))
    lines.append("")
    return "\n".join(lines)


def render_dimensions_markdown(dimensions: Any) -> str:
    if not isinstance(dimensions, dict) or not dimensions:
        return "### Evaluation Dimensions\n\nNo dimension scores."
    rows = []
    for key, item in dimensions.items():
        if not isinstance(item, dict):
            continue
        label = DIMENSION_DEFINITIONS.get(key, key)
        rows.append(
            [
                label,
                item.get("score", ""),
                item.get("status", ""),
                item.get("source", ""),
                item.get("summary", ""),
                item.get("recommendation", ""),
            ]
        )
    header = "Dimension | Score | Status | Source | Summary | Recommendation"
    sep = "--- | --- | --- | --- | --- | ---"
    body = "\n".join("|".join(_markdown_cell(value) for value in row) for row in rows)
    return "### Evaluation Dimensions\n\n%s\n%s\n%s" % (header, sep, body)


def render_reliability_markdown(reliability: Any) -> str:
    if not isinstance(reliability, dict) or not reliability:
        return "### Judge Reliability\n\nNo reliability metrics."
    rows = [
        ["Evidence Coverage", reliability.get("evidence_coverage", "")],
        ["Unsupported Claims", reliability.get("unsupported_claim_count", "")],
        ["Invalid Evidence", reliability.get("invalid_evidence_count", "")],
        ["Fact/Inference Mismatch", reliability.get("fact_inference_mismatch_count", "")],
        ["Valid Evidence", reliability.get("valid_evidence_count", "")],
        ["Total Evidence", reliability.get("total_evidence_count", "")],
    ]
    body = "Metric | Value\n--- | ---\n" + "\n".join("|".join(_markdown_cell(value) for value in row) for row in rows)
    invalid = reliability.get("invalid_evidence") or []
    if invalid:
        body += "\n\nInvalid evidence:\n"
        for row in invalid:
            if isinstance(row, dict):
                body += "- `%s`: %s\n" % (_markdown_inline(row.get("location", "")), _markdown_inline(row.get("reason", "")))
    return "### Judge Reliability\n\n%s" % body


def _markdown_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("|", "\\|")


def _markdown_inline(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\n", " ").replace("`", "'")
```

Add a schema test that `render_judge_markdown` includes the new headings, so Task 4's Markdown assertion has a direct unit test:

```python
def test_render_judge_markdown_includes_dimensions_and_reliability():
    markdown = render_judge_markdown(
        {
            "overall_status": "partial",
            "completion_score": 60,
            "confidence": "medium",
            "summary": "部分完成",
            "dimensions": {
                "intent_recognition": {
                    "score": 80,
                    "status": "pass",
                    "summary": "理解目标",
                    "recommendation": "保持",
                    "source": "judge",
                }
            },
            "judge_reliability": {
                "evidence_coverage": 0.75,
                "unsupported_claim_count": 1,
                "invalid_evidence_count": 0,
                "fact_inference_mismatch_count": 1,
            },
            "findings": [],
        }
    )
    assert "### Evaluation Dimensions" in markdown
    assert "意图识别" in markdown
    assert "### Judge Reliability" in markdown
    assert "0.75" in markdown
```

Keep `test_render_judge_markdown_includes_evidence` passing; do not remove the findings/evidence lines.

- [ ] **Step 5: Run schema and tool tests**

Run:

```bash
python3 -m pytest tests/test_agent_schemas.py tests/test_agent_tools.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add agentloop/agents/tools.py agentloop/agents/schemas.py tests/test_agent_tools.py tests/test_agent_schemas.py
git commit -m "$(cat <<'EOF'
Merge reliability into judge reports and Markdown.

EOF
)"
```

---

### Task 5: Update DeepAgent Prompts to Produce Evidence-Grounded Dimensions

**Files:**
- Modify: `agentloop/agents/prompts.py`
- Test: `tests/test_agent_graph.py`

**Interfaces:**
- Produces prompt requirement for `dimensions`.
- Produces prompt requirement that `judge_reliability` may be empty because deterministic code recalculates it.
- Semantic-only dimensions may be `unknown` when the judge has no evidence.

- [ ] **Step 1: Add failing prompt test**

Add to `tests/test_agent_graph.py`:

```python
from agentloop.agents.prompts import REPORT_WRITER_PROMPT, MAIN_SYSTEM_PROMPT


def test_report_writer_prompt_requires_dimensions_and_reliability():
    assert "dimensions" in REPORT_WRITER_PROMPT
    assert "judge_reliability" in REPORT_WRITER_PROMPT
    assert "intent_recognition" in REPORT_WRITER_PROMPT
    assert "evidence_citation" in REPORT_WRITER_PROMPT
    assert "没有证据" in REPORT_WRITER_PROMPT
    assert "unknown" in REPORT_WRITER_PROMPT
    assert "dimensions" in MAIN_SYSTEM_PROMPT
```

- [ ] **Step 2: Run prompt test and verify failure**

Run: `python3 -m pytest tests/test_agent_graph.py::test_report_writer_prompt_requires_dimensions_and_reliability -q`

Expected: FAIL because prompt does not require the new fields.

- [ ] **Step 3: Update prompts**

In `REPORT_WRITER_PROMPT`, add this JSON contract section:

```text
- dimensions: object，必须包含以下 key：
  - intent_recognition: 意图识别
  - task_planning: 任务规划
  - tool_selection: 工具选择
  - parameter_generation: 参数生成
  - evidence_citation: 证据引用
  - state_tracking: 状态更新
  - final_response: 最终表达
  - context_management: 上下文管理
  - execution_closure: 执行闭环
  - error_recovery: 错误恢复
  - verification: 验证能力
  - safety_permission: 安全与权限
  - efficiency: 成本与效率

每个 dimension 必须包含 score、status、summary、evidence、recommendation、source。
status 只能是 pass|partial|fail|unknown。
没有证据时 status 必须是 unknown，不得写成 fail 或 pass，也不得用 0 分冒充已评估失败。
source 只能先填 judge；确定性校验会在写入报告时覆盖 evidence_citation、execution_closure；只有发生工具失败时才会向下封顶 error_recovery；只有发生工具失败时才会封顶 tool_selection。
没有工具失败时，error_recovery 必须保持 unknown，不能因为“没有失败”写成 pass。
不要根据工具成功次数给 tool_selection 打高分；成功只说明调用没有报错，不说明选对了工具。
没有证据的语义判断不得给 pass；没有证据的判断必须在 summary 中说明证据不足。

- judge_reliability: object，可先填写空对象；系统会基于 evidence 重新计算，并保留 invalid_evidence 明细。
```

In `MAIN_SYSTEM_PROMPT`, add:

```text
- Agent 评测不是只看最终结论，还必须输出 dimensions 和 evidence。
- 维度评分中的 fact 必须能回指 trajectory、audit、summary 或 session 中的证据。
- 完成度仍以 assess_run_quality 的交付物检查为准；dimensions 不能推翻缺失交付物的失败结论。
```

- [ ] **Step 4: Run prompt tests**

Run: `python3 -m pytest tests/test_agent_graph.py -q`

Expected: PASS. Existing completion/deliverable/evidence-chain assertions must still pass.

- [ ] **Step 5: Commit**

```bash
git add agentloop/agents/prompts.py tests/test_agent_graph.py
git commit -m "$(cat <<'EOF'
Require evidence-grounded dimensions in judge prompts.

EOF
)"
```

---

### Task 6: Reuse Schema Markdown in Backend Export

**Files:**
- Modify: `backend/app/routes/runs.py`
- Test: `tests/test_backend_routes.py`

**Interfaces:**
- Consumes `render_dimensions_markdown` and `render_reliability_markdown` from `agentloop.agents.schemas`
- Produces export sections `### Evaluation Dimensions` and `### Judge Reliability`

- [ ] **Step 1: Add failing backend render test**

Add to `tests/test_backend_routes.py`:

```python
from backend.app.routes.runs import _render_judge_markdown


def test_render_judge_markdown_includes_dimensions_and_reliability():
    markdown = _render_judge_markdown(
        {
            "overall_status": "partial",
            "completion_score": 60,
            "confidence": "medium",
            "summary": "部分完成",
            "dimensions": {
                "intent_recognition": {
                    "score": 80,
                    "status": "pass",
                    "summary": "理解目标",
                    "recommendation": "保持",
                    "source": "judge",
                }
            },
            "judge_reliability": {
                "evidence_coverage": 0.75,
                "unsupported_claim_count": 1,
                "invalid_evidence_count": 0,
                "fact_inference_mismatch_count": 1,
            },
            "findings": [],
        }
    )

    assert "### Evaluation Dimensions" in markdown
    assert "意图识别" in markdown
    assert "### Judge Reliability" in markdown
    assert "0.75" in markdown
```

- [ ] **Step 2: Run test and verify failure**

Run: `python3 -m pytest tests/test_backend_routes.py::test_render_judge_markdown_includes_dimensions_and_reliability -q`

Expected: FAIL because backend export does not yet call the schema helpers.

- [ ] **Step 3: Reuse schema helpers in backend export**

In `backend/app/routes/runs.py`, import:

```python
from agentloop.agents.schemas import render_dimensions_markdown, render_reliability_markdown
```

Update `_render_judge_markdown` to include these sections before findings:

```python
            "### Summary",
            str(judge.get("summary") or ""),
            "",
            render_dimensions_markdown(judge.get("dimensions")),
            "",
            render_reliability_markdown(judge.get("judge_reliability")),
            "",
            _render_findings_markdown("### Judge Findings", judge.get("findings") or []),
```

Do not reimplement table rendering in this file.

- [ ] **Step 4: Run backend tests**

Run: `python3 -m pytest tests/test_backend_routes.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routes/runs.py tests/test_backend_routes.py
git commit -m "$(cat <<'EOF'
Include judge dimensions in Markdown export.

EOF
)"
```

---

### Task 7: Display Dimensions and Reliability in the Run Page

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/views/RunView.vue`
- Test: `tests/test_frontend_app_state.py`

**Interfaces:**
- Consumes `JudgeReport.dimensions`
- Consumes `JudgeReport.judge_reliability`
- Shows Chinese labels from a local copy of the dimension map
- `unknown` rows render as `未评估`, not as fail
- Dimension rows are not clickable metric targets; `selectMetricTarget` has no matching anchors

- [ ] **Step 1: Add failing frontend source tests**

Add to `tests/test_frontend_app_state.py`:

```python
def test_run_view_shows_evaluation_dimensions_and_judge_reliability():
    source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "export interface JudgeDimension" in api_source
    assert "export interface JudgeReliability" in api_source
    assert "invalid_evidence" in api_source
    assert "评测维度" in source
    assert "Judge 可信度" in source
    assert "dimensionRows" in source
    assert "reliabilityRows" in source
    assert "未评估" in source
    assert "意图识别" in source
```

- [ ] **Step 2: Run the frontend source test and verify failure**

Run: `python3 -m pytest tests/test_frontend_app_state.py::test_run_view_shows_evaluation_dimensions_and_judge_reliability -q`

Expected: FAIL because the run page and types do not include the new fields.

- [ ] **Step 3: Add TypeScript interfaces**

In `frontend/src/api.ts`, add:

```ts
export interface JudgeEvidence {
  source: string;
  message_index?: number;
  quote: string;
  reason?: string;
  location?: string;
}

export interface JudgeDimension {
  score: number;
  status: string;
  summary: string;
  evidence: JudgeEvidence[];
  recommendation: string;
  source: string;
}

export interface JudgeReliability {
  evidence_coverage: number;
  unsupported_claim_count: number;
  invalid_evidence_count: number;
  fact_inference_mismatch_count: number;
  valid_evidence_count: number;
  total_evidence_count: number;
  notes: string[];
  invalid_evidence?: JudgeEvidence[];
}
```

Update `Finding.evidence` to use `JudgeEvidence[]`.

Update `JudgeReport`:

```ts
  dimensions?: Record<string, JudgeDimension>;
  judge_reliability?: JudgeReliability;
  metrics?: Record<string, unknown>;
```

`metrics` becomes `Record<string, unknown>` because dimension metrics may contain arrays and nested objects. If `npm run build` finds call sites that assumed only `number | string`, narrow those reads locally with `Number(...)`, `String(...)`, or explicit array checks instead of weakening the new type.

- [ ] **Step 4: Add computed rows in `RunView.vue`**

Near the other frontend constants, add:

```ts
const DIMENSION_LABELS: Record<string, string> = {
  intent_recognition: "意图识别",
  task_planning: "任务规划",
  tool_selection: "工具选择",
  parameter_generation: "参数生成",
  evidence_citation: "证据引用",
  state_tracking: "状态更新",
  final_response: "最终表达",
  context_management: "上下文管理",
  execution_closure: "执行闭环",
  error_recovery: "错误恢复",
  verification: "验证能力",
  safety_permission: "安全与权限",
  efficiency: "成本与效率"
};
```

In the `<script setup>` section, after `findingSeverityRows`, add:

```ts
const dimensionRows = computed(() =>
  Object.entries(judge.value?.dimensions || {}).map(([key, item]) => ({
    key,
    label: DIMENSION_LABELS[key] || key,
    score: Number(item.score || 0),
    status: item.status || "unknown",
    source: item.source || "missing",
    summary: item.summary || "",
    recommendation: item.recommendation || ""
  }))
);

const reliabilityRows = computed(() => {
  const reliability = judge.value?.judge_reliability;
  if (!reliability) return [];
  return [
    { label: "Evidence Coverage", value: formatPercent(reliability.evidence_coverage) },
    { label: "Unsupported Claims", value: formatNumber(reliability.unsupported_claim_count) },
    { label: "Invalid Evidence", value: formatNumber(reliability.invalid_evidence_count) },
    { label: "Fact/Inference Mismatch", value: formatNumber(reliability.fact_inference_mismatch_count) },
    { label: "Valid Evidence", value: formatNumber(reliability.valid_evidence_count) },
    { label: "Total Evidence", value: formatNumber(reliability.total_evidence_count) }
  ];
});
```

`formatPercent` in `RunView.vue` multiplies by 100. `evidence_coverage` is 0-1, so `0.75` renders as `75%`. Do not pass a pre-multiplied percent.

- [ ] **Step 5: Render dimensions and reliability**

In the `report-grid` section before the existing `Agent 判断` panel, add:

```vue
        <div class="panel">
          <div class="panel-head">
            <h3>评测维度</h3>
            <span class="chip quiet">{{ dimensionRows.length }}</span>
          </div>
          <div v-if="dimensionRows.length" class="dimension-list">
            <div
              v-for="row in dimensionRows"
              :key="row.key"
              class="dimension-row"
            >
              <span>{{ row.label }}</span>
              <strong v-if="row.status !== 'unknown'">{{ row.score }}</strong>
              <strong v-else>—</strong>
              <em :data-status="row.status">{{ row.status === "unknown" ? "未评估" : row.status }}</em>
              <small>{{ row.summary || row.recommendation || row.source }}</small>
            </div>
          </div>
          <p v-else class="empty">暂无维度评分。</p>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>Judge 可信度</h3>
            <span class="chip quiet">evidence</span>
          </div>
          <div v-if="reliabilityRows.length" class="metric-list">
            <div v-for="row in reliabilityRows" :key="row.label">
              <span>{{ row.label }}</span>
              <strong>{{ row.value }}</strong>
            </div>
          </div>
          <p v-else class="empty">暂无可信度指标。</p>
        </div>
```

Use `div.dimension-row`, not a button that calls `selectMetricTarget`.

- [ ] **Step 6: Add minimal CSS if needed**

If `dimension-list` has no styles, add near existing report styles:

```css
.dimension-list {
  display: grid;
  gap: 8px;
}

.dimension-row {
  display: grid;
  grid-template-columns: minmax(130px, 1fr) 56px 76px;
  gap: 10px;
  align-items: center;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid rgba(20, 32, 51, 0.1);
  border-radius: 6px;
  background: #fff;
  color: inherit;
  text-align: left;
}

.dimension-row small {
  grid-column: 1 / -1;
  color: #5a6b7d;
}

.dimension-row em[data-status="unknown"] {
  color: #5a6b7d;
  font-style: normal;
}
```

These colors match existing RunView report styles.

- [ ] **Step 7: Run frontend tests and build**

Run:

```bash
python3 -m pytest tests/test_frontend_app_state.py -q
cd frontend
npm run build
```

Expected: PASS.

If build fails because dependencies are missing, run `npm install` only after asking for network approval.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api.ts frontend/src/views/RunView.vue tests/test_frontend_app_state.py
git commit -m "$(cat <<'EOF'
Show judge dimensions and reliability on the run page.

EOF
)"
```

---

### Task 8: End-to-End Regression and Documentation

**Files:**
- Modify: `README.md`
- Test: all touched tests

**Interfaces:**
- Produces documentation for dimensions and judge reliability.
- Smoke test uses `write_judge_report` on a temporary run fixture, not a hard-coded run id.

- [ ] **Step 1: Update README DeepAgent Analysis section**

Add after the output file list:

```markdown
`agent_judge.json` includes two reliability-focused sections:

- `dimensions`: structured scores for intent recognition, task planning, tool selection, parameter generation, evidence citation, state tracking, final response, context management, execution closure, error recovery, verification, safety/permission, and efficiency. Missing evidence uses `unknown`, not fail.
- `judge_reliability`: deterministic checks over the judge output, including evidence coverage, unsupported claims, invalid evidence references, fact claims with no valid evidence, and `invalid_evidence` detail.

Deterministic checks validate evidence against `summary.json`, `audit.json`, `trajectory.jsonl`, and `session.json` when available. This round scores `evidence_citation` and `execution_closure`, caps `error_recovery` only when a failure occurred, and caps `tool_selection` only when a tool failed. If no failure occurred, `error_recovery` remains `unknown`. LLM judge conclusions remain useful for semantic assessment, but unsupported factual claims are surfaced in reliability metrics. Completion still follows `assess_run_quality` deliverable checks.
```

- [ ] **Step 2: Run backend unit tests**

Run:

```bash
python3 -m pytest tests/test_agent_schemas.py tests/test_agent_evidence.py tests/test_agent_dimensions.py tests/test_agent_tools.py tests/test_agent_graph.py tests/test_backend_routes.py tests/test_frontend_app_state.py -q
```

Expected: PASS.

- [ ] **Step 3: Run full pytest suite**

Run:

```bash
python3 -m pytest -q
```

Expected: PASS.

- [ ] **Step 4: Run frontend build**

Run:

```bash
cd frontend
npm run build
```

Expected: PASS.

- [ ] **Step 5: Fixture smoke test instead of a live run directory**

Do not call `judge-run` against `runs/20260814-...`. That directory is not in the repo and may need model credentials.

Use this snippet during execution review:

```python
import json
from pathlib import Path

from agentloop.agents.tools import write_judge_report

run = Path("runs") / "_judge-reliability-smoke"
run.mkdir(parents=True, exist_ok=True)
(run / "summary.json").write_text(json.dumps({"title": "smoke"}, ensure_ascii=False), encoding="utf-8")
(run / "audit.json").write_text(
    json.dumps({"tool_calls": [{"index": 1, "tool": "terminal", "status": "error", "result_preview": "timeout"}]}),
    encoding="utf-8",
)
(run / "trajectory.jsonl").write_text(
    "\n".join(
        json.dumps(row, ensure_ascii=False)
        for row in [
            {"index": 0, "role": "user", "content": "请生成报告"},
            {"index": 1, "role": "tool", "content": "timeout"},
            {"index": 2, "role": "assistant", "content": "未能完成"},
        ]
    ),
    encoding="utf-8",
)
write_judge_report(
    str(run),
    {
        "overall_status": "failed",
        "completion_score": 20,
        "confidence": "high",
        "summary": "工具超时",
        "findings": [
            {
                "severity": "high",
                "category": "tool",
                "title": "timeout",
                "type": "fact",
                "evidence": [{"source": "trajectory.jsonl", "message_index": 1, "quote": "timeout"}],
                "recommendation": "retry",
            }
        ],
    },
)
```

Expected:

- `agent_judge.json` contains `dimensions` and `judge_reliability.invalid_evidence`.
- `chart_data.json` contains `dimension_scores`.
- `agent_judge.md` contains `### Evaluation Dimensions` and `### Judge Reliability`.
- `dimensions.state_tracking.status` is `unknown` unless a judge score was supplied.
- Delete `runs/_judge-reliability-smoke` after the check. Do not commit that directory.

Cleanup command:

```bash
rm -rf runs/_judge-reliability-smoke
```

- [ ] **Step 6: Commit documentation**

```bash
git add README.md
git commit -m "$(cat <<'EOF'
Document judge dimensions and reliability checks.

EOF
)"
```

---

## Self-Review

**Spec coverage:** This plan now encodes the missing design decisions in Global Constraints: unknown vs fail, which dimensions are deterministic this round, `invalid_evidence` retention, `assess_run_quality` ownership of completion, shared Markdown helpers, and LiveEval out of scope.

**Placeholder scan:** Smoke test includes a complete `write_judge_report` snippet. Frontend install still asks for network approval if `node_modules` is missing. No unfinished placeholder markers are left.

**Type consistency:** `dimensions`, `judge_reliability`, `invalid_evidence`, `render_dimensions_markdown`, `render_reliability_markdown`, `validate_judge_evidence`, and `assess_dimensions` are named consistently across schema, evidence, dimensions, tools, backend, frontend, and tests.

**Metric consistency:** `unsupported_claim_count` is empty-evidence facts; `fact_inference_mismatch_count` is facts with zero valid evidence after validation; empty quotes are `missing_quote`.

**Execution note:** `/Users/chen/agentloop` is a git repository. Commit after each task. Do not push unless the user asks.
