# Agentloop DeepAgent Analysis Design

## Goal

Agentloop needs a clear Agent analysis module that can inspect exported xuanjian-agent runs and produce a professional, evidence-backed judgment report. The first milestone is a local DeepAgent-powered analysis core and CLI command. A FastAPI backend and Vue dashboard will consume the generated files later.

## Scope

In scope for the first milestone:

- Add an explicit `agentloop.agents` module using DeepAgent.
- Analyze existing run directories that already contain `session.json`, `summary.json`, `audit.json`, and `trajectory.jsonl`.
- Generate structured outputs for later frontend charts.
- Add a CLI command named `judge-run`.
- Keep deterministic auditing as the factual preprocessing layer.

Out of scope for the first milestone:

- Web UI.
- Database persistence.
- User login or dashboard permission management.
- Remote deployment on `192.168.1.238`.
- Live instrumentation inside xuanjian-agent.

## Architecture

The Agent module will follow the style of `/Users/chen/Downloads/code-analysis-platform`:

- `agent.py` owns DeepAgent lifecycle and exposes `get_agent()`, `graph()`, `invoke_agent()`, and `stream_agent()`.
- `subagents.py` defines specialist subagents through `build_subagents(model_spec)`.
- `prompts.py` contains the main system prompt and subagent prompts.
- `tools.py` exposes narrow, deterministic tools over local run artifacts.
- `schemas.py` defines the report structure and validation helpers.
- `langgraph.json` exposes the graph entrypoint for LangGraph-compatible runners.

The main Agent is named `AgentLoop Analyst`. It routes work to specialist subagents and merges their conclusions. It should not directly read arbitrary files or call shell commands.

## Subagents

`trajectory_analyst` analyzes message flow, phase progression, completion signals, and context breaks.

`tool_auditor` analyzes tool usage, tool errors, retry loops, sensitive operations, missing tool results, and failed recovery behavior.

`root_cause_analyst` combines the trajectory and tool findings into root-cause chains. It must distinguish direct facts from inferred conclusions.

`report_writer` creates the final JSON, Markdown, and chart-ready data.

## Tool Boundary

DeepAgent will only receive controlled tools:

- `list_runs()`
- `load_run_summary(run_path)`
- `load_audit(run_path)`
- `load_trajectory_slice(run_path, offset, limit)`
- `search_trajectory(run_path, keyword)`
- `get_tool_calls(run_path, tool_name=None, status=None)`
- `build_chart_data(run_path)`
- `write_judge_report(run_path, report)`

These tools only read or write Agentloop run artifacts. They do not expose shell execution, arbitrary filesystem access, or network calls.

## Output Contract

Each judged run writes:

- `agent_judge.json`
- `agent_judge.md`
- `chart_data.json`

`agent_judge.json` must follow this shape:

```json
{
  "overall_status": "success|partial|failed|unknown",
  "completion_score": 0,
  "confidence": "high|medium|low",
  "summary": "",
  "findings": [
    {
      "severity": "critical|high|medium|low",
      "category": "completion|tool|latency|context|delivery|safety",
      "title": "",
      "type": "fact|inference",
      "evidence": [
        {
          "source": "trajectory.jsonl",
          "message_index": 0,
          "quote": ""
        }
      ],
      "recommendation": ""
    }
  ],
  "metrics": {
    "message_count": 0,
    "tool_call_count": 0,
    "tool_error_count": 0,
    "completion_score": 0
  },
  "charts": {
    "tool_distribution": [],
    "phase_timeline": [],
    "finding_severity": []
  }
}
```

Conclusions must be typed:

- `fact`: directly supported by run artifacts.
- `inference`: reasoned from multiple evidence points and not directly proven.

This prevents semantic reports from overstating evidence, especially for latency and completion-quality conclusions.

## CLI

Add:

```bash
python3 -m agentloop.cli judge-run --run /Users/chen/agentloop/runs/<run>
```

The command will:

1. Validate required run files.
2. Load deterministic summary and audit artifacts.
3. Invoke the DeepAgent module.
4. Validate and normalize the returned report.
5. Write `agent_judge.json`, `agent_judge.md`, and `chart_data.json`.

## Configuration

Environment variables:

- `MODEL_PROVIDER`, default `openai`.
- `MODEL_NAME`, default project-selected model.
- `CHECKPOINTER_TYPE`, default `memory`.
- Python `>=3.11,<4.0`.
- Provider API key such as `OPENAI_API_KEY` or `DEEPSEEK_API_KEY`.

If DeepAgent dependencies or model credentials are unavailable, deterministic commands such as `fetch-session` and `render-run` must continue to work.

## Later Web Platform

FastAPI will call the same analysis module instead of reimplementing judgment logic:

- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `POST /api/runs/{run_id}/judge`
- `GET /api/runs/{run_id}/judge`
- `GET /api/runs/{run_id}/charts`

Vue will render run lists, scorecards, tool distribution charts, phase timeline charts, root-cause findings, evidence links, and Markdown report previews.

## Test Strategy

Unit tests will cover schema normalization, chart-data generation, run-file validation, and deterministic tool wrappers.

Agent invocation tests will use monkeypatching or a fake agent response so the default test suite does not require live model credentials.

Manual verification will run `judge-run` against the existing PPT run after dependencies and credentials are configured.
