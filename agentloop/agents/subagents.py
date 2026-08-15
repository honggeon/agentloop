from __future__ import annotations

from .prompts import (
    REPORT_WRITER_PROMPT,
    ROOT_CAUSE_ANALYST_PROMPT,
    TOOL_AUDITOR_PROMPT,
    TRAJECTORY_ANALYST_PROMPT,
)
from .tools import (
    assess_run_quality,
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
            "tools": [load_run_summary, load_audit, load_trajectory_slice, search_trajectory, get_tool_calls, assess_run_quality],
            "model": model_spec,
        },
        {
            "name": "report_writer",
            "description": "生成结构化 JSON、Markdown 报告和图表数据。",
            "system_prompt": REPORT_WRITER_PROMPT,
            "tools": [assess_run_quality, build_chart_data, write_judge_report],
            "model": model_spec,
        },
    ]
