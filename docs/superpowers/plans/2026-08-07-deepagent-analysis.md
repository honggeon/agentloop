# Agentloop DeepAgent Analysis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a DeepAgent-powered analysis module that judges existing Agentloop run directories and writes structured JSON, Markdown, and chart data.

**Architecture:** Keep the existing deterministic audit pipeline as the factual preprocessing layer. Add `agentloop.agents` as a separate module with controlled file-based tools, specialist DeepAgent subagents, and a `judge-run` CLI entrypoint. DeepAgent dependencies must be optional for existing commands.

**Tech Stack:** Python 3.11+, DeepAgents, LangChain chat model initialization, LangGraph MemorySaver, pytest, JSON run artifacts.

## Global Constraints

- First milestone only: DeepAgent module, local run analysis, `judge-run` CLI, structured output files.
- Do not implement Web UI, database persistence, login, remote deployment, or live instrumentation in this plan.
- DeepAgent may only access controlled tools over Agentloop run artifacts.
- Existing commands `fetch-session`, `smoke-chat`, and `render-run` must keep working when DeepAgent dependencies or model credentials are missing.
- Every semantic conclusion must be classified as `fact` or `inference`.
- Outputs per judged run: `agent_judge.json`, `agent_judge.md`, and `chart_data.json`.

---

## File Structure

- Create `agentloop/agents/__init__.py`: package exports for the analysis module.
- Create `agentloop/agents/schemas.py`: validation, normalization, and Markdown rendering for judge reports.
- Create `agentloop/agents/tools.py`: controlled tools for reading run artifacts and writing judge outputs.
- Create `agentloop/agents/prompts.py`: main prompt and subagent prompts.
- Create `agentloop/agents/subagents.py`: `build_subagents(model_spec)` modeled after the reference project.
- Create `agentloop/agents/agent.py`: lazy DeepAgent graph creation, model validation, invoke and stream helpers.
- Modify `agentloop/cli.py`: add `judge-run`.
- Modify `pyproject.toml`: add optional DeepAgent dependencies under an extra named `agent`.
- Create `langgraph.json`: expose `agentloop.agents.agent:graph`.
- Create `tests/test_agent_schemas.py`: schema and Markdown tests.
- Create `tests/test_agent_tools.py`: controlled tool tests.
- Create `tests/test_cli_judge_run.py`: fake-agent CLI test.
- Modify `README.md`: document `judge-run`, dependency installation, and env vars.

---

### Task 1: Report Schema And Markdown Renderer

**Files:**
- Create: `agentloop/agents/__init__.py`
- Create: `agentloop/agents/schemas.py`
- Test: `tests/test_agent_schemas.py`

**Interfaces:**
- Produces: `normalize_judge_report(raw: object) -> dict`
- Produces: `render_judge_markdown(report: dict) -> str`
- Produces: `extract_json_object(value: object) -> dict`
- Consumes: no prior task outputs.

- [ ] **Step 1: Write failing schema tests**

Create `tests/test_agent_schemas.py` with:

```python
import json

from agentloop.agents.schemas import extract_json_object, normalize_judge_report, render_judge_markdown


def test_normalize_judge_report_fills_defaults_and_counts_findings():
    report = normalize_judge_report(
        {
            "overall_status": "failed",
            "completion_score": 12,
            "confidence": "high",
            "summary": "未交付 pptx。",
            "findings": [
                {
                    "severity": "critical",
                    "category": "delivery",
                    "title": "没有生成目标文件",
                    "type": "fact",
                    "evidence": [{"source": "trajectory.jsonl", "message_index": 10, "quote": "ModuleNotFoundError"}],
                    "recommendation": "补齐依赖后重跑导出。",
                }
            ],
            "metrics": {"message_count": 3, "tool_call_count": 1, "tool_error_count": 1},
        }
    )

    assert report["overall_status"] == "failed"
    assert report["metrics"]["completion_score"] == 12
    assert report["charts"]["finding_severity"] == [{"severity": "critical", "count": 1}]


def test_normalize_judge_report_rejects_invalid_conclusion_type():
    try:
        normalize_judge_report(
            {
                "overall_status": "unknown",
                "completion_score": 0,
                "confidence": "low",
                "summary": "x",
                "findings": [{"severity": "low", "category": "tool", "title": "x", "type": "guess"}],
            }
        )
    except ValueError as exc:
        assert "type" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_extract_json_object_from_agent_message_content():
    payload = {"overall_status": "success", "completion_score": 90, "confidence": "medium", "summary": "ok"}
    message = {"messages": [{"role": "assistant", "content": "prefix\n```json\n%s\n```" % json.dumps(payload)}]}
    assert extract_json_object(message)["overall_status"] == "success"


def test_render_judge_markdown_includes_evidence():
    report = normalize_judge_report(
        {
            "overall_status": "partial",
            "completion_score": 55,
            "confidence": "medium",
            "summary": "部分完成。",
            "findings": [
                {
                    "severity": "high",
                    "category": "completion",
                    "title": "最终回答没有交付物",
                    "type": "fact",
                    "evidence": [{"source": "session.json", "message_index": 20, "quote": "请安装依赖"}],
                    "recommendation": "将交付文件作为完成条件。",
                }
            ],
        }
    )

    markdown = render_judge_markdown(report)
    assert "# Agentloop Agent Judge" in markdown
    assert "最终回答没有交付物" in markdown
    assert "session.json#20" in markdown
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_agent_schemas.py -q`

Expected: import failure because `agentloop.agents.schemas` does not exist.

- [ ] **Step 3: Implement schema helpers**

Create `agentloop/agents/__init__.py`:

```python
"""DeepAgent-based analysis module for Agentloop runs."""
```

Create `agentloop/agents/schemas.py` with:

```python
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, Iterable, List


ALLOWED_STATUS = {"success", "partial", "failed", "unknown"}
ALLOWED_CONFIDENCE = {"high", "medium", "low"}
ALLOWED_SEVERITY = {"critical", "high", "medium", "low"}
ALLOWED_CATEGORY = {"completion", "tool", "latency", "context", "delivery", "safety"}
ALLOWED_TYPE = {"fact", "inference"}


def extract_json_object(value: object) -> Dict[str, Any]:
    if isinstance(value, dict) and "overall_status" in value:
        return dict(value)
    if isinstance(value, dict) and value.get("messages"):
        messages = value.get("messages") or []
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("content"):
                return extract_json_object(message["content"])
            content = getattr(message, "content", None)
            if content:
                return extract_json_object(content)
    if isinstance(value, str):
        text = value.strip()
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
        if fenced:
            return json.loads(fenced.group(1))
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
    raise ValueError("agent response did not contain a JSON object")


def normalize_judge_report(raw: object) -> Dict[str, Any]:
    report = extract_json_object(raw)
    status = _choice(report.get("overall_status"), ALLOWED_STATUS, "unknown", "overall_status")
    confidence = _choice(report.get("confidence"), ALLOWED_CONFIDENCE, "low", "confidence")
    score = _score(report.get("completion_score", 0))
    findings = [_normalize_finding(item) for item in report.get("findings") or []]
    metrics = dict(report.get("metrics") or {})
    metrics["completion_score"] = score
    metrics.setdefault("message_count", 0)
    metrics.setdefault("tool_call_count", 0)
    metrics.setdefault("tool_error_count", 0)
    charts = dict(report.get("charts") or {})
    charts.setdefault("finding_severity", _count_by(findings, "severity"))
    charts.setdefault("tool_distribution", [])
    charts.setdefault("phase_timeline", [])
    return {
        "overall_status": status,
        "completion_score": score,
        "confidence": confidence,
        "summary": str(report.get("summary") or ""),
        "findings": findings,
        "metrics": metrics,
        "charts": charts,
    }


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


def _normalize_finding(item: object) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("finding must be a dict")
    finding_type = _choice(item.get("type"), ALLOWED_TYPE, "", "type")
    evidence = item.get("evidence") or []
    if not isinstance(evidence, list):
        raise ValueError("finding evidence must be a list")
    return {
        "severity": _choice(item.get("severity"), ALLOWED_SEVERITY, "low", "severity"),
        "category": _choice(item.get("category"), ALLOWED_CATEGORY, "tool", "category"),
        "title": str(item.get("title") or ""),
        "type": finding_type,
        "evidence": [_normalize_evidence(row) for row in evidence],
        "recommendation": str(item.get("recommendation") or ""),
    }


def _normalize_evidence(item: object) -> Dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("evidence must be a dict")
    result = {"source": str(item.get("source") or ""), "quote": str(item.get("quote") or "")[:500]}
    if item.get("message_index") is not None:
        result["message_index"] = int(item["message_index"])
    return result


def _choice(value: object, allowed: set[str], default: str, field: str) -> str:
    text = str(value or default)
    if text not in allowed:
        raise ValueError("%s must be one of %s" % (field, sorted(allowed)))
    return text


def _score(value: object) -> int:
    return max(0, min(100, int(value)))


def _count_by(rows: Iterable[Dict[str, Any]], key: str) -> List[Dict[str, Any]]:
    counts = Counter(str(row.get(key) or "") for row in rows)
    return [{key: name, "count": count} for name, count in sorted(counts.items()) if name]
```

- [ ] **Step 4: Run schema tests**

Run: `python3 -m pytest tests/test_agent_schemas.py -q`

Expected: all tests pass.

---

### Task 2: Controlled Run Tools And Chart Data

**Files:**
- Create: `agentloop/agents/tools.py`
- Test: `tests/test_agent_tools.py`

**Interfaces:**
- Consumes: `normalize_judge_report(raw: object) -> dict`
- Consumes: `render_judge_markdown(report: dict) -> str`
- Produces: `list_runs(root: str = "runs") -> list[dict]`
- Produces: `load_run_summary(run_path: str) -> dict`
- Produces: `load_audit(run_path: str) -> dict`
- Produces: `load_trajectory_slice(run_path: str, offset: int = 0, limit: int = 50) -> list[dict]`
- Produces: `search_trajectory(run_path: str, keyword: str) -> list[dict]`
- Produces: `get_tool_calls(run_path: str, tool_name: str | None = None, status: str | None = None) -> list[dict]`
- Produces: `build_chart_data(run_path: str) -> dict`
- Produces: `write_judge_report(run_path: str, report: object) -> dict`

- [ ] **Step 1: Write failing tool tests**

Create `tests/test_agent_tools.py` with:

```python
import json

from agentloop.agents.tools import (
    build_chart_data,
    get_tool_calls,
    load_trajectory_slice,
    search_trajectory,
    write_judge_report,
)


def _write_run(tmp_path):
    run = tmp_path / "runs" / "20260807-test"
    run.mkdir(parents=True)
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
    audit = {
        "message_count": 3,
        "tool_call_count": 2,
        "tool_result_count": 2,
        "findings": [{"severity": "error", "code": "tool_error", "message": "failed"}],
        "tool_calls": [
            {"index": 1, "tool": "terminal", "status": "error", "args_preview": "pip", "result_preview": "timeout"},
            {"index": 2, "tool": "read_file", "status": "ok", "args_preview": "a", "result_preview": "b"},
        ],
        "phases": [{"phase": "execute", "tool_count": 2, "tools": ["terminal", "read_file"]}],
    }
    (run / "audit.json").write_text(json.dumps(audit), encoding="utf-8")
    rows = [
        {"index": 0, "role": "user", "content": "make ppt"},
        {"index": 1, "role": "tool", "content": "ModuleNotFoundError"},
    ]
    (run / "trajectory.jsonl").write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return run


def test_load_and_search_trajectory(tmp_path):
    run = _write_run(tmp_path)
    assert load_trajectory_slice(str(run), 0, 1)[0]["role"] == "user"
    matches = search_trajectory(str(run), "ModuleNotFoundError")
    assert matches[0]["index"] == 1


def test_get_tool_calls_filters_by_tool_and_status(tmp_path):
    run = _write_run(tmp_path)
    calls = get_tool_calls(str(run), tool_name="terminal", status="error")
    assert len(calls) == 1
    assert calls[0]["result_preview"] == "timeout"


def test_build_chart_data_uses_audit_details(tmp_path):
    run = _write_run(tmp_path)
    charts = build_chart_data(str(run))
    assert charts["tool_distribution"] == [{"tool": "read_file", "count": 1}, {"tool": "terminal", "count": 1}]
    assert charts["phase_timeline"][0]["phase"] == "execute"


def test_write_judge_report_writes_three_files(tmp_path):
    run = _write_run(tmp_path)
    result = write_judge_report(
        str(run),
        {
            "overall_status": "failed",
            "completion_score": 20,
            "confidence": "high",
            "summary": "failed",
            "findings": [],
        },
    )
    assert result["json_path"].endswith("agent_judge.json")
    assert (run / "agent_judge.json").exists()
    assert (run / "agent_judge.md").exists()
    assert (run / "chart_data.json").exists()
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_agent_tools.py -q`

Expected: import failure because `agentloop.agents.tools` does not exist.

- [ ] **Step 3: Implement controlled tools**

Create `agentloop/agents/tools.py` with focused JSON readers, JSONL readers, audit filters, chart counters, and report writer. Reject missing required files with `FileNotFoundError("missing <path>")`.

Use this chart behavior:

```python
tool_distribution = sorted Counter(audit["tool_calls"][*]["tool"]) by tool name
phase_timeline = audit.get("phases", [])
finding_severity = sorted Counter(report["findings"][*]["severity"]) by severity name
```

`write_judge_report` must call `normalize_judge_report`, merge `build_chart_data(run_path)` into `report["charts"]` without deleting `finding_severity`, then write all three output files with `ensure_ascii=False` and `indent=2`.

- [ ] **Step 4: Run tool tests**

Run: `python3 -m pytest tests/test_agent_tools.py -q`

Expected: all tests pass.

---

### Task 3: DeepAgent Prompts, Subagents, And Graph Entrypoint

**Files:**
- Create: `agentloop/agents/prompts.py`
- Create: `agentloop/agents/subagents.py`
- Create: `agentloop/agents/agent.py`
- Create: `langgraph.json`
- Modify: `pyproject.toml`
- Test: `tests/test_agent_graph.py`

**Interfaces:**
- Consumes all functions from `agentloop.agents.tools`.
- Produces: `build_subagents(model_spec: str) -> list[dict]`
- Produces: `get_agent() -> Any`
- Produces: `graph() -> Any`
- Produces: `invoke_agent(messages: list[dict], config: dict | None = None) -> dict`
- Produces: `stream_agent(messages: list[dict], config: dict | None = None) -> AsyncIterator[dict]`

- [ ] **Step 1: Write failing import and configuration tests**

Create `tests/test_agent_graph.py` with:

```python
import importlib


def test_subagents_have_short_descriptions():
    module = importlib.import_module("agentloop.agents.subagents")
    subagents = module.build_subagents("openai:gpt-test")
    names = [item["name"] for item in subagents]
    assert names == ["trajectory_analyst", "tool_auditor", "root_cause_analyst", "report_writer"]
    assert all(len(item["description"]) < 120 for item in subagents)


def test_agent_module_imports_without_deepagent_installed():
    module = importlib.import_module("agentloop.agents.agent")
    assert hasattr(module, "get_agent")
    assert hasattr(module, "invoke_agent")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `python3 -m pytest tests/test_agent_graph.py -q`

Expected: import failure because DeepAgent module files do not exist.

- [ ] **Step 3: Implement prompts and subagents**

Create `prompts.py` with Chinese prompts that require evidence-backed output and `fact`/`inference` separation.

Create `subagents.py` using the reference style:

```python
from __future__ import annotations

from .prompts import (
    REPORT_WRITER_PROMPT,
    ROOT_CAUSE_ANALYST_PROMPT,
    TOOL_AUDITOR_PROMPT,
    TRAJECTORY_ANALYST_PROMPT,
)
from .tools import (
    build_chart_data,
    get_tool_calls,
    load_audit,
    load_run_summary,
    load_trajectory_slice,
    search_trajectory,
    write_judge_report,
)


def build_subagents(model_spec: str) -> list[dict]:
    return [
        {
            "name": "trajectory_analyst",
            "description": "分析运行轨迹、阶段流转、完成信号和上下文断点。",
            "system_prompt": TRAJECTORY_ANALYST_PROMPT,
            "tools": [load_run_summary, load_trajectory_slice, search_trajectory],
            "model": model_spec,
        },
        {
            "name": "tool_auditor",
            "description": "分析工具调用、错误、重试、危险操作和恢复失败。",
            "system_prompt": TOOL_AUDITOR_PROMPT,
            "tools": [load_audit, get_tool_calls, search_trajectory],
            "model": model_spec,
        },
        {
            "name": "root_cause_analyst",
            "description": "归纳根因链路，并区分事实结论和推断结论。",
            "system_prompt": ROOT_CAUSE_ANALYST_PROMPT,
            "tools": [load_run_summary, load_audit, load_trajectory_slice, search_trajectory, get_tool_calls],
            "model": model_spec,
        },
        {
            "name": "report_writer",
            "description": "生成结构化 JSON、Markdown 报告和图表数据。",
            "system_prompt": REPORT_WRITER_PROMPT,
            "tools": [build_chart_data, write_judge_report],
            "model": model_spec,
        },
    ]
```

- [ ] **Step 4: Implement optional DeepAgent imports**

Create `agent.py` so importing the module does not require installed DeepAgent. Import `deepagents`, `langchain`, and `langgraph` inside `get_agent()` or helper functions. If missing, raise a clear `RuntimeError` only when invoking DeepAgent:

```python
RuntimeError("DeepAgent analysis requires installing the optional agent dependencies: pip install -e '.[agent]'")
```

Use the reference lazy cache pattern with `_agent_graph` and `_agent_lock`.

- [ ] **Step 5: Add LangGraph config**

Create `langgraph.json`:

```json
{
  "dependencies": ["."],
  "graphs": {
    "agent": "./agentloop/agents/agent.py:graph"
  },
  "env": ".env"
}
```

- [ ] **Step 6: Add optional dependencies**

Modify `pyproject.toml` with:

```toml
[project.optional-dependencies]
agent = [
  "deepagents>=0.6.12",
  "langchain>=0.3",
  "langgraph>=0.2",
  "langgraph-checkpoint>=2",
]
```

If `[project.optional-dependencies]` already exists, add only the `agent` key.

- [ ] **Step 7: Run graph tests**

Run: `python3 -m pytest tests/test_agent_graph.py -q`

Expected: all tests pass without requiring model credentials.

---

### Task 4: `judge-run` CLI Integration

**Files:**
- Modify: `agentloop/cli.py`
- Test: `tests/test_cli_judge_run.py`

**Interfaces:**
- Consumes: `invoke_agent(messages: list[dict], config: dict | None = None) -> dict`
- Consumes: `normalize_judge_report(raw: object) -> dict`
- Consumes: `write_judge_report(run_path: str, report: object) -> dict`
- Produces CLI command: `python3 -m agentloop.cli judge-run --run <run_dir>`

- [ ] **Step 1: Write failing CLI test with fake agent**

Create `tests/test_cli_judge_run.py`:

```python
import json

from agentloop import cli


def test_judge_run_uses_agent_and_writes_report(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "summary.json").write_text(json.dumps({"session_id": "s1", "model": "m1"}), encoding="utf-8")
    (run / "audit.json").write_text(json.dumps({"message_count": 1, "tool_call_count": 0, "tool_calls": [], "findings": []}), encoding="utf-8")
    (run / "trajectory.jsonl").write_text(json.dumps({"index": 0, "role": "user", "content": "hi"}), encoding="utf-8")

    async def fake_invoke(messages, config=None):
        assert "run_path" in messages[0]["content"]
        return {
            "overall_status": "success",
            "completion_score": 95,
            "confidence": "high",
            "summary": "ok",
            "findings": [],
        }

    monkeypatch.setattr("agentloop.agents.agent.invoke_agent", fake_invoke)
    code = cli.main(["judge-run", "--run", str(run)])
    assert code == 0
    assert json.loads((run / "agent_judge.json").read_text(encoding="utf-8"))["overall_status"] == "success"
```

- [ ] **Step 2: Run test to verify failure**

Run: `python3 -m pytest tests/test_cli_judge_run.py -q`

Expected: argparse failure because `judge-run` is not registered.

- [ ] **Step 3: Implement CLI command**

Modify `agentloop/cli.py`:

- Add `import asyncio`.
- Register `judge-run` parser with `--run`, `--thread-id`, and `--output-format`.
- Add `_judge_run(args) -> int`.
- Validate that `summary.json`, `audit.json`, and `trajectory.jsonl` exist.
- Build one user message containing run path and required output contract.
- Call `asyncio.run(invoke_agent(messages, config=config))`.
- Normalize and write report through `write_judge_report`.
- Print output paths.

The default `thread_id` should be `agentloop-judge-<run_dir.name>`.

- [ ] **Step 4: Run CLI test**

Run: `python3 -m pytest tests/test_cli_judge_run.py -q`

Expected: pass.

---

### Task 5: Documentation And Full Verification

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes all prior tasks.
- Produces documented usage for optional DeepAgent setup and `judge-run`.

- [ ] **Step 1: Update README**

Add a section:

````markdown
## DeepAgent analysis

Install optional dependencies:

```bash
python3 -m pip install -e '.[agent]'
```

Configure a model provider:

```bash
export MODEL_PROVIDER=openai
export MODEL_NAME=gpt-4.1
export OPENAI_API_KEY=...
```

Judge an exported run:

```bash
python3 -m agentloop.cli judge-run --run runs/<run-dir>
```

The command writes:

- `agent_judge.json`
- `agent_judge.md`
- `chart_data.json`
````

- [ ] **Step 2: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_agent_schemas.py tests/test_agent_tools.py tests/test_agent_graph.py tests/test_cli_judge_run.py -q
```

Expected: all focused tests pass.

- [ ] **Step 3: Run full tests**

Run:

```bash
python3 -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 4: Manual deterministic verification**

Run:

```bash
python3 -m agentloop.cli render-run --run runs/20260807-124623-5ab7b3b0-1659-4c30-9735-1d54da1548fd
```

Expected: existing deterministic reports are regenerated successfully.

- [ ] **Step 5: Manual DeepAgent verification when credentials are available**

Run:

```bash
python3 -m agentloop.cli judge-run --run runs/20260807-124623-5ab7b3b0-1659-4c30-9735-1d54da1548fd
```

Expected: `agent_judge.json`, `agent_judge.md`, and `chart_data.json` are created in the run directory. If optional dependencies or credentials are missing, the command must fail with the clear DeepAgent dependency or model configuration message while other CLI commands still work.

---

## Self-Review

Spec coverage:

- DeepAgent module: Task 3.
- Controlled tools: Task 2.
- `judge-run` CLI: Task 4.
- Output files: Task 1, Task 2, Task 4.
- Optional dependencies and existing command compatibility: Task 3, Task 5.
- Web platform deferred: Global Constraints and README scope.

Placeholder scan:

- No placeholder markers are intentionally left in this plan.
- Each task includes concrete files, interfaces, tests, and verification commands.

Type consistency:

- `normalize_judge_report`, `render_judge_markdown`, `write_judge_report`, and `invoke_agent` signatures are consistent across tasks.
- CLI task consumes the same interfaces produced by schema, tools, and agent tasks.
