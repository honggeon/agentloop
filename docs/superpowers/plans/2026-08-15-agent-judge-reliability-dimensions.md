# Agent Judge Reliability Dimensions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add evidence-grounded agent evaluation dimensions and judge reliability checks so Agentloop can show not only what the judge concluded, but how trustworthy that conclusion is.

**Architecture:** Keep the existing `agent_judge.json` contract backward compatible by adding `dimensions` and `judge_reliability` beside the existing `findings`, `metrics`, and `charts`. Deterministic code validates evidence and computes rule-based dimension signals; the DeepAgent judge may still provide semantic judgments, but unsupported claims are downgraded or surfaced in reliability metrics. Reports, exports, and the Vue run page read the same normalized report.

**Tech Stack:** Python 3.11+, FastAPI backend, Vue 3 + TypeScript frontend, pytest for backend tests, existing Agentloop JSON artifacts (`summary.json`, `audit.json`, `trajectory.jsonl`, `agent_judge.json`).

## Global Constraints

- Preserve compatibility with existing reports that only contain `overall_status`, `completion_score`, `confidence`, `summary`, `findings`, `metrics`, and `charts`.
- Do not require DeepAgent/model credentials for deterministic schema, evidence validation, or report-writing tests.
- Treat deterministic facts as stronger than LLM judge output: invalid or missing evidence must affect `judge_reliability`.
- Every `fact` finding must have at least one valid evidence reference or be counted as unsupported.
- Do not replace current `findings`; add dimensions as a structured evaluation view.
- The current `/Users/chen/agentloop` directory is not a git repository. Skip commit commands during execution unless the user first moves this work into a git repo or initializes one.

---

## File Structure

- `agentloop/agents/schemas.py`
  - Extend normalization for `dimensions` and `judge_reliability`.
  - Add stable allowed dimension IDs and status enums.
  - Keep old reports valid by generating default unknown dimensions.

- `agentloop/agents/evidence.py`
  - New deterministic evidence validator.
  - Loads `summary.json`, `audit.json`, `trajectory.jsonl`, and optional `session.json`.
  - Verifies that evidence source exists, `message_index` points to a real trajectory row when present, and quote text appears in the referenced artifact.

- `agentloop/agents/dimensions.py`
  - New deterministic dimension assessor.
  - Computes rule-backed checks for tool usage, parameter generation, state tracking, execution closure, safety, and efficiency.
  - Merges judge-provided semantic dimension scores with deterministic caps and reliability results.

- `agentloop/agents/tools.py`
  - Call evidence validation and deterministic dimension assessment inside `write_judge_report`.
  - Add `dimension_scores` and `judge_reliability` chart data.

- `agentloop/agents/prompts.py`
  - Require DeepAgent report output to include the agreed dimensions.
  - Require evidence for dimension scores and findings.

- `backend/app/routes/runs.py`
  - Include dimension and reliability tables in Markdown export.

- `frontend/src/api.ts`
  - Add TypeScript types for dimensions and judge reliability.

- `frontend/src/views/RunView.vue`
  - Display dimension score rows and judge reliability metrics in the run detail page.

- Tests:
  - `tests/test_agent_schemas.py`
  - `tests/test_agent_evidence.py`
  - `tests/test_agent_dimensions.py`
  - `tests/test_agent_tools.py`
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
- Produces normalized report field:
  - `dimensions: dict[str, dict[str, Any]]`
  - each dimension has `score: int`, `status: str`, `summary: str`, `evidence: list[dict[str, Any]]`, `recommendation: str`, `source: str`
- Produces normalized report field:
  - `judge_reliability: dict[str, Any]`
  - keys: `evidence_coverage`, `unsupported_claim_count`, `invalid_evidence_count`, `fact_inference_mismatch_count`, `valid_evidence_count`, `total_evidence_count`, `notes`

- [ ] **Step 1: Add failing tests for dimension defaults**

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
    assert report["charts"]["dimension_scores"][0]["dimension"] == "intent_recognition"
```

- [ ] **Step 2: Add failing tests for Chinese dimension names and bounded scores**

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
        }
    )

    assert report["dimensions"]["intent_recognition"]["score"] == 100
    assert report["dimensions"]["intent_recognition"]["status"] == "pass"
    assert report["dimensions"]["tool_selection"]["score"] == 0
    assert report["dimensions"]["tool_selection"]["status"] == "fail"
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
```

Add normalizer helpers:

```python
def _normalize_dimensions(value: object) -> Dict[str, Dict[str, Any]]:
    raw = value if isinstance(value, dict) else {}
    normalized: Dict[str, Dict[str, Any]] = {}
    for key in DIMENSION_DEFINITIONS:
        item = raw.get(key)
        if item is None:
            label = DIMENSION_DEFINITIONS[key]
            item = raw.get(label)
        normalized[key] = _normalize_dimension(item)
    return normalized


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
        "status": _choice(item.get("status"), ALLOWED_DIMENSION_STATUS, "unknown", "dimension.status"),
        "summary": str(item.get("summary") or ""),
        "evidence": normalized_evidence,
        "recommendation": str(item.get("recommendation") or ""),
        "source": _choice(item.get("source"), ALLOWED_DIMENSION_SOURCE, "missing", "dimension.source"),
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
    return {
        "evidence_coverage": max(0, min(1, float(coverage))),
        "unsupported_claim_count": max(0, int(item.get("unsupported_claim_count") or 0)),
        "invalid_evidence_count": max(0, int(item.get("invalid_evidence_count") or 0)),
        "fact_inference_mismatch_count": max(0, int(item.get("fact_inference_mismatch_count") or 0)),
        "valid_evidence_count": valid,
        "total_evidence_count": total,
        "notes": [str(note) for note in notes if str(note).strip()],
    }
```

Update `normalize_judge_report` to include:

```python
dimensions = _normalize_dimensions(report.get("dimensions"))
judge_reliability = _normalize_judge_reliability(report.get("judge_reliability"))
charts["dimension_scores"] = [
    {"dimension": key, "label": DIMENSION_DEFINITIONS[key], "score": row["score"], "status": row["status"]}
    for key, row in dimensions.items()
]
```

Return `dimensions` and `judge_reliability` in the normalized report.

- [ ] **Step 5: Add Chinese enum synonyms**

Extend `_CHOICE_SYNONYMS`:

```python
"通过": "pass",
"合格": "pass",
"部分通过": "partial",
"不通过": "fail",
"失败": "fail",
"规则": "deterministic",
"确定性": "deterministic",
"模型": "judge",
"评审": "judge",
```

Do not remove the existing `"失败": "failed"` behavior for `overall_status`; implement status-specific mapping by adding a local synonym map inside `_normalize_dimension` if this shared mapping causes a conflict. The safer implementation is:

```python
def _dimension_status(value: object) -> str:
    text = str(value or "unknown").strip().lower()
    text = {"通过": "pass", "合格": "pass", "部分通过": "partial", "失败": "fail", "不通过": "fail", "未知": "unknown"}.get(text, text)
    return _choice(text, ALLOWED_DIMENSION_STATUS, "unknown", "dimension.status")
```

Then call `_dimension_status(item.get("status"))`.

- [ ] **Step 6: Run schema tests**

Run: `python3 -m pytest tests/test_agent_schemas.py -q`

Expected: PASS.

---

### Task 2: Add Deterministic Evidence Validation

**Files:**
- Create: `agentloop/agents/evidence.py`
- Test: `tests/test_agent_evidence.py`

**Interfaces:**
- Consumes: normalized report from `normalize_judge_report(raw) -> dict`
- Produces: `validate_judge_evidence(run_path: str | Path, report: dict[str, Any]) -> dict[str, Any]`
- Produces result keys:
  - `evidence_coverage: float`
  - `unsupported_claim_count: int`
  - `invalid_evidence_count: int`
  - `fact_inference_mismatch_count: int`
  - `valid_evidence_count: int`
  - `total_evidence_count: int`
  - `notes: list[str]`
  - `invalid_evidence: list[dict[str, Any]]`

- [ ] **Step 1: Write failing tests for valid and invalid evidence**

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


def test_validate_judge_evidence_counts_valid_quotes(tmp_path):
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
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 1, "quote": "timeout"}],
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


def test_validate_judge_evidence_flags_missing_quote_and_fact_without_evidence(tmp_path):
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
            ],
        }
    )

    reliability = validate_judge_evidence(run, report)

    assert reliability["unsupported_claim_count"] == 1
    assert reliability["invalid_evidence_count"] == 1
    assert reliability["fact_inference_mismatch_count"] == 1
    assert reliability["evidence_coverage"] == 0
    assert reliability["invalid_evidence"][0]["reason"] == "quote_not_found"
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


def validate_judge_evidence(run_path: str | Path, report: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(run_path)
    artifacts = _load_artifacts(run_dir)
    evidence_rows = _collect_evidence(report)
    invalid = []
    valid_count = 0

    for location, evidence in evidence_rows:
        result = _validate_one_evidence(evidence, artifacts)
        if result is None:
            valid_count += 1
        else:
            invalid.append({"location": location, **evidence, "reason": result})

    fact_rows = _collect_fact_claims(report)
    unsupported = sum(1 for _, item in fact_rows if not item.get("evidence"))
    fact_inference_mismatch = unsupported
    total = len(evidence_rows)
    invalid_count = len(invalid)
    coverage = round(valid_count / total, 4) if total else 0
    notes = []
    if unsupported:
        notes.append("%s fact claims have no evidence" % unsupported)
    if invalid_count:
        notes.append("%s evidence references are invalid" % invalid_count)

    return {
        "evidence_coverage": coverage,
        "unsupported_claim_count": unsupported,
        "invalid_evidence_count": invalid_count,
        "fact_inference_mismatch_count": fact_inference_mismatch,
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
```

Implement quote checks:

```python
def _validate_one_evidence(evidence: dict[str, Any], artifacts: dict[str, Any]) -> str | None:
    source = str(evidence.get("source") or "")
    quote = str(evidence.get("quote") or "")
    if not source:
        return "missing_source"
    if source not in artifacts:
        return "source_not_found"
    artifact = artifacts[source]
    if source == "trajectory.jsonl" and evidence.get("message_index") is not None:
        row = _find_trajectory_row(artifact, int(evidence["message_index"]))
        if row is None:
            return "message_index_not_found"
        haystack = json.dumps(row, ensure_ascii=False, default=str)
    else:
        haystack = json.dumps(artifact, ensure_ascii=False, default=str)
    if quote and quote not in haystack:
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

---

### Task 3: Add Deterministic Dimension Assessment

**Files:**
- Create: `agentloop/agents/dimensions.py`
- Test: `tests/test_agent_dimensions.py`

**Interfaces:**
- Consumes: `validate_judge_evidence(run_path, report) -> dict`
- Produces: `assess_dimensions(run_path: str | Path, report: dict[str, Any], reliability: dict[str, Any]) -> dict[str, dict[str, Any]]`
- Uses normalized dimension shape from Task 1.

- [ ] **Step 1: Write failing tests for deterministic tool and reliability dimensions**

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


def test_assess_dimensions_marks_tool_selection_and_state_tracking_pass(tmp_path):
    run = _write_tool_run(tmp_path)
    report = normalize_judge_report({"overall_status": "success", "completion_score": 90, "confidence": "high", "summary": "ok"})
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    assert dimensions["tool_selection"]["status"] == "pass"
    assert dimensions["tool_selection"]["score"] >= 80
    assert dimensions["state_tracking"]["status"] == "pass"
    assert dimensions["execution_closure"]["status"] == "pass"


def test_assess_dimensions_penalizes_invalid_evidence_and_tool_error(tmp_path):
    run = _write_tool_run(tmp_path, status="error")
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 40,
            "confidence": "medium",
            "summary": "x",
            "dimensions": {
                "evidence_citation": {
                    "score": 90,
                    "status": "pass",
                    "source": "judge",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 0, "quote": "missing quote"}],
                }
            },
        }
    )
    reliability = validate_judge_evidence(run, report)

    dimensions = assess_dimensions(run, report, reliability)

    assert dimensions["tool_selection"]["status"] == "fail"
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

from .schemas import DIMENSION_DEFINITIONS, normalize_judge_report


def assess_dimensions(run_path: str | Path, report: dict[str, Any], reliability: dict[str, Any]) -> dict[str, dict[str, Any]]:
    run_dir = Path(run_path)
    normalized = normalize_judge_report(report)
    dimensions = {key: dict(value) for key, value in normalized["dimensions"].items()}
    audit = _read_json(run_dir / "audit.json")
    trajectory = _read_jsonl(run_dir / "trajectory.jsonl")

    _set_dimension(dimensions, "tool_selection", *_tool_selection_score(audit))
    _set_dimension(dimensions, "parameter_generation", *_parameter_generation_score(audit))
    _set_dimension(dimensions, "state_tracking", *_state_tracking_score(audit, trajectory))
    _set_dimension(dimensions, "execution_closure", *_execution_closure_score(trajectory, normalized))
    _set_dimension(dimensions, "error_recovery", *_error_recovery_score(audit, trajectory))
    _set_dimension(dimensions, "safety_permission", *_safety_permission_score(audit))
    _set_dimension(dimensions, "efficiency", *_efficiency_score(audit))
    _set_dimension(dimensions, "evidence_citation", *_evidence_citation_score(reliability))
    return dimensions
```

Add helpers:

```python
def _set_dimension(dimensions: dict[str, dict[str, Any]], key: str, score: int, status: str, summary: str, recommendation: str) -> None:
    current = dimensions.get(key) or {}
    if current.get("source") == "judge" and int(current.get("score") or 0) < score:
        return
    dimensions[key] = {
        "score": score,
        "status": status,
        "summary": summary,
        "evidence": current.get("evidence") or [],
        "recommendation": recommendation,
        "source": "deterministic" if current.get("source") in {"missing", None, ""} else "deterministic+judge",
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
def _tool_selection_score(audit: dict[str, Any]) -> tuple[int, str, str, str]:
    calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    if not calls:
        return 70, "partial", "No tool call was required or captured.", "Check intent before requiring tool use."
    failed = sum(1 for call in calls if str(call.get("status") or "").lower() in {"error", "failed", "missing"})
    score = round(100 * (1 - failed / len(calls)))
    return score, _status(score), "%s/%s tool calls succeeded." % (len(calls) - failed, len(calls)), "Investigate failed or unnecessary tool calls."


def _parameter_generation_score(audit: dict[str, Any]) -> tuple[int, str, str, str]:
    calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    if not calls:
        return 70, "partial", "No tool parameters were captured.", "Capture tool arguments for parameter-level assessment."
    invalid = 0
    for call in calls:
        preview = str(call.get("args_preview") or "")
        if preview.strip().startswith("{"):
            try:
                json.loads(preview)
            except json.JSONDecodeError:
                invalid += 1
    score = round(100 * (1 - invalid / len(calls)))
    return score, _status(score), "%s invalid argument previews detected." % invalid, "Validate tool args before execution."


def _state_tracking_score(audit: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[int, str, str, str]:
    messages = int(audit.get("message_count") or len(trajectory))
    if messages <= 0:
        return 0, "fail", "No messages were captured.", "Persist trajectory rows for every run."
    return 90, "pass", "%s messages captured for state reconstruction." % messages, "Keep trajectory persistence enabled."


def _execution_closure_score(trajectory: list[dict[str, Any]], report: dict[str, Any]) -> tuple[int, str, str, str]:
    has_final = any(str(row.get("role") or "") == "assistant" and str(row.get("content") or "").strip() for row in trajectory[-3:])
    if report.get("overall_status") == "success" and has_final:
        return 90, "pass", "Run has a final assistant response and success status.", "Verify claimed deliverables when present."
    if has_final:
        return 65, "partial", "Run has a final assistant response but did not fully succeed.", "Tie final response to explicit completion checks."
    return 20, "fail", "No final assistant response was captured.", "Require final response before closing a run."


def _error_recovery_score(audit: dict[str, Any], trajectory: list[dict[str, Any]]) -> tuple[int, str, str, str]:
    calls = [call for call in audit.get("tool_calls") or [] if isinstance(call, dict)]
    failed = [call for call in calls if str(call.get("status") or "").lower() in {"error", "failed", "missing"}]
    if not failed:
        return 90, "pass", "No tool failures were captured.", "No recovery action required."
    retry_text = json.dumps(trajectory, ensure_ascii=False).lower()
    recovered = "retry" in retry_text or "重试" in retry_text or len(calls) > len(failed)
    if recovered:
        return 60, "partial", "Failures were followed by retry or additional action.", "Make recovery outcome explicit in the final response."
    return 30, "fail", "Failures were captured without clear recovery.", "Add retry, fallback, or explicit blocked state handling."


def _safety_permission_score(audit: dict[str, Any]) -> tuple[int, str, str, str]:
    risky = []
    for call in audit.get("tool_calls") or []:
        text = json.dumps(call, ensure_ascii=False).lower()
        if "rm -rf" in text or "git reset --hard" in text or "delete" in text:
            risky.append(call)
    score = 40 if risky else 95
    return score, _status(score), "%s risky operations detected." % len(risky), "Require explicit approval for destructive operations."


def _efficiency_score(audit: dict[str, Any]) -> tuple[int, str, str, str]:
    count = int(audit.get("tool_call_count") or len(audit.get("tool_calls") or []))
    score = 95 if count <= 8 else 75 if count <= 20 else 45
    return score, _status(score), "%s tool calls captured." % count, "Review repeated calls when count is high."


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

---

### Task 4: Merge Reliability and Dimensions During Report Writing

**Files:**
- Modify: `agentloop/agents/tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `validate_judge_evidence(run_path, report) -> dict`
- Consumes: `assess_dimensions(run_path, report, reliability) -> dict`
- Produces `agent_judge.json` with `dimensions`, `judge_reliability`, and `charts.dimension_scores`.

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

    assert "tool_selection" in report["dimensions"]
    assert report["judge_reliability"]["valid_evidence_count"] == 1
    assert report["judge_reliability"]["invalid_evidence_count"] == 0
    assert any(row["dimension"] == "tool_selection" for row in report["charts"]["dimension_scores"])
```

- [ ] **Step 2: Run test and verify failure**

Run: `python3 -m pytest tests/test_agent_tools.py::test_write_judge_report_adds_dimensions_and_reliability -q`

Expected: FAIL because report writing does not merge reliability or dimensions.

- [ ] **Step 3: Update `write_judge_report`**

In `agentloop/agents/tools.py`, import:

```python
from .dimensions import assess_dimensions
from .evidence import validate_judge_evidence
from .schemas import DIMENSION_DEFINITIONS
```

Inside `write_judge_report`, after quality merge and before chart merge:

```python
    reliability = validate_judge_evidence(str(run_dir), normalized)
    existing_reliability = dict(normalized.get("judge_reliability") or {})
    reliability.update({key: value for key, value in existing_reliability.items() if key == "notes" and value})
    normalized["judge_reliability"] = reliability
    normalized["dimensions"] = assess_dimensions(str(run_dir), normalized, reliability)
    normalized = normalize_judge_report(normalized)
```

After chart merge:

```python
    charts["dimension_scores"] = [
        {
            "dimension": key,
            "label": DIMENSION_DEFINITIONS[key],
            "score": value["score"],
            "status": value["status"],
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

- [ ] **Step 4: Run tool tests**

Run: `python3 -m pytest tests/test_agent_tools.py -q`

Expected: PASS.

---

### Task 5: Update DeepAgent Prompts to Produce Evidence-Grounded Dimensions

**Files:**
- Modify: `agentloop/agents/prompts.py`
- Test: `tests/test_agent_graph.py` or `tests/test_agent_schemas.py`

**Interfaces:**
- Produces prompt requirement for `dimensions`.
- Produces prompt requirement for `judge_reliability` as optional, because deterministic code recalculates it.

- [ ] **Step 1: Add failing prompt test**

Add to `tests/test_agent_graph.py`:

```python
from agentloop.agents.prompts import REPORT_WRITER_PROMPT


def test_report_writer_prompt_requires_dimensions_and_reliability():
    assert "dimensions" in REPORT_WRITER_PROMPT
    assert "judge_reliability" in REPORT_WRITER_PROMPT
    assert "intent_recognition" in REPORT_WRITER_PROMPT
    assert "evidence_citation" in REPORT_WRITER_PROMPT
    assert "没有证据" in REPORT_WRITER_PROMPT
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
source 只能是 judge；确定性校验会在写入报告时覆盖或合并。
没有证据的语义判断不得给 pass；没有证据的判断必须在 summary 中说明证据不足。

- judge_reliability: object，可先填写空对象；系统会基于 evidence 重新计算。
```

In `MAIN_SYSTEM_PROMPT`, add:

```text
- Agent 评测不是只看最终结论，还必须输出 dimensions 和 evidence。
- 维度评分中的 fact 必须能回指 trajectory、audit、summary 或 session 中的证据。
```

- [ ] **Step 4: Run prompt test**

Run: `python3 -m pytest tests/test_agent_graph.py::test_report_writer_prompt_requires_dimensions_and_reliability -q`

Expected: PASS.

---

### Task 6: Add Markdown Export for Dimensions and Judge Reliability

**Files:**
- Modify: `backend/app/routes/runs.py`
- Test: `tests/test_backend_routes.py`

**Interfaces:**
- Consumes `judge["dimensions"]`
- Consumes `judge["judge_reliability"]`
- Produces Markdown sections:
  - `### Evaluation Dimensions`
  - `### Judge Reliability`

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
    assert "intent_recognition" in markdown
    assert "### Judge Reliability" in markdown
    assert "0.75" in markdown
```

- [ ] **Step 2: Run test and verify failure**

Run: `python3 -m pytest tests/test_backend_routes.py::test_render_judge_markdown_includes_dimensions_and_reliability -q`

Expected: FAIL because Markdown export does not render these sections.

- [ ] **Step 3: Implement Markdown helpers**

In `backend/app/routes/runs.py`, add:

```python
def _render_dimensions_markdown(dimensions: Any) -> str:
    if not isinstance(dimensions, dict) or not dimensions:
        return "### Evaluation Dimensions\n\nNo dimension scores."
    rows = []
    for key, item in dimensions.items():
        if not isinstance(item, dict):
            continue
        rows.append(
            [
                key,
                item.get("score", ""),
                item.get("status", ""),
                item.get("source", ""),
                item.get("summary", ""),
                item.get("recommendation", ""),
            ]
        )
    return "### Evaluation Dimensions\n\n%s" % _markdown_table(
        ["Dimension", "Score", "Status", "Source", "Summary", "Recommendation"],
        rows,
    )


def _render_reliability_markdown(reliability: Any) -> str:
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
    return "### Judge Reliability\n\n%s" % _markdown_table(["Metric", "Value"], rows)
```

Update `_render_judge_markdown` to include these sections before findings:

```python
            _render_dimensions_markdown(judge.get("dimensions")),
            "",
            _render_reliability_markdown(judge.get("judge_reliability")),
            "",
            _render_findings_markdown("### Judge Findings", judge.get("findings") or []),
```

- [ ] **Step 4: Run backend tests**

Run: `python3 -m pytest tests/test_backend_routes.py -q`

Expected: PASS.

---

### Task 7: Display Dimensions and Reliability in the Run Page

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/views/RunView.vue`
- Test: `tests/test_frontend_app_state.py`

**Interfaces:**
- Consumes `JudgeReport.dimensions`
- Consumes `JudgeReport.judge_reliability`

- [ ] **Step 1: Add TypeScript interfaces**

In `frontend/src/api.ts`, add:

```ts
export interface JudgeEvidence {
  source: string;
  message_index?: number;
  quote: string;
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
}
```

Update `Finding.evidence` to use `JudgeEvidence[]`.

Update `JudgeReport`:

```ts
  dimensions?: Record<string, JudgeDimension>;
  judge_reliability?: JudgeReliability;
  metrics?: Record<string, unknown>;
```

- [ ] **Step 2: Add computed rows in `RunView.vue`**

In the `<script setup>` section, after `findingSeverityRows`, add:

```ts
const dimensionRows = computed(() =>
  Object.entries(judge.value?.dimensions || {}).map(([key, item]) => ({
    key,
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

- [ ] **Step 3: Render dimensions and reliability**

In the `report-grid` section before the existing `Agent 判断` panel, add:

```vue
        <div class="panel">
          <div class="panel-head">
            <h3>评测维度</h3>
            <span class="chip quiet">{{ dimensionRows.length }}</span>
          </div>
          <div v-if="dimensionRows.length" class="dimension-list">
            <button
              v-for="row in dimensionRows"
              :key="row.key"
              type="button"
              @click="selectMetricTarget(row.key)"
            >
              <span>{{ row.key }}</span>
              <strong>{{ row.score }}</strong>
              <em :data-status="row.status">{{ row.status }}</em>
              <small>{{ row.summary || row.recommendation || row.source }}</small>
            </button>
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

- [ ] **Step 4: Add minimal CSS if needed**

If `dimension-list` has no styles, add near existing report styles:

```css
.dimension-list {
  display: grid;
  gap: 8px;
}

.dimension-list button {
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

.dimension-list small {
  grid-column: 1 / -1;
  color: #5a6b7d;
}
```

- [ ] **Step 5: Run frontend build**

Run:

```bash
cd frontend
npm run build
```

Expected: PASS.

If build fails because dependencies are missing, run `npm install` only after asking for network approval.

---

### Task 8: End-to-End Regression and Documentation

**Files:**
- Modify: `README.md`
- Test: all touched tests

**Interfaces:**
- Produces documentation for dimensions and judge reliability.

- [ ] **Step 1: Update README DeepAgent Analysis section**

Add after the output file list:

```markdown
`agent_judge.json` includes two reliability-focused sections:

- `dimensions`: structured scores for intent recognition, task planning, tool selection, parameter generation, evidence citation, state tracking, final response, context management, execution closure, error recovery, verification, safety/permission, and efficiency.
- `judge_reliability`: deterministic checks over the judge output, including evidence coverage, unsupported claims, invalid evidence references, and fact/inference mismatches.

Deterministic checks validate evidence against `summary.json`, `audit.json`, `trajectory.jsonl`, and `session.json` when available. LLM judge conclusions remain useful for semantic assessment, but unsupported factual claims are surfaced in reliability metrics.
```

- [ ] **Step 2: Run backend unit tests**

Run:

```bash
python3 -m pytest tests/test_agent_schemas.py tests/test_agent_evidence.py tests/test_agent_dimensions.py tests/test_agent_tools.py tests/test_backend_routes.py -q
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

- [ ] **Step 5: Manual smoke test with an existing run**

Run:

```bash
python3 -m agentloop.cli judge-run --run runs/20260814-175921-89433aa8-6993-47b3-ad81-c4c923348e5d
```

Expected:

- `agent_judge.json` contains `dimensions`.
- `agent_judge.json` contains `judge_reliability`.
- `chart_data.json` contains `dimension_scores`.
- `agent_judge.md` contains dimension and reliability sections.

If optional DeepAgent dependencies or model credentials are unavailable, use an existing `agent_judge.json` fixture and call `write_judge_report` from a small local Python snippet during execution review instead of running the live judge.

---

## Self-Review

**Spec coverage:** The plan covers dimensions, deterministic evidence validation, rule-first scoring, LLM prompt constraints, judge reliability metrics, Markdown export, frontend display, and documentation.

**Placeholder scan:** No unfinished placeholder markers or unspecified edge-case placeholders are intentionally left. The only conditional is network-dependent frontend dependency installation, which must ask for approval if needed.

**Type consistency:** `dimensions`, `judge_reliability`, `dimension_scores`, `validate_judge_evidence`, and `assess_dimensions` are consistently named across schema, tools, backend, frontend, and tests.

**Execution note:** Because `/Users/chen/agentloop` is not currently a git repository, the implementation worker should not run `git add` or `git commit` unless the user first confirms the intended repository setup.
