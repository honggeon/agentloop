from __future__ import annotations

import functools
from typing import Any

from .prompts import (
    REPORT_REJECTED_FEEDBACK,
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


@functools.wraps(write_judge_report)
def _write_judge_report_with_feedback(*args: Any, **kwargs: Any) -> Any:
    """write_judge_report 的工具级包装：发布门禁拒绝时返回修正反馈而非抛出。

    模型提交的 proposal 缺维度时，直接抛出会让异常穿透整张裁判图、
    整个裁判轮次失败；作为工具结果返回拒绝原因，report_writer 可以在
    同一轮内补全报告重新提交。路由层和 CLI 直接调用的 write_judge_report
    保持抛出，发布门禁语义不变。
    """
    try:
        return write_judge_report(*args, **kwargs)
    except ValueError as exc:
        # ValueError 覆盖 JudgeProposalError（缺维度门禁）和提案归一化失败。
        return {
            "status": "rejected",
            "error": str(exc),
            "instruction": REPORT_REJECTED_FEEDBACK,
        }


def build_subagents(model_spec: Any) -> list[dict]:
    """构建子智能体声明。``model_spec`` 为 "provider:model" 字符串或
    已初始化的 chat model 实例（profile 配置路径，见 agents/model_config.py）。"""
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
            "tools": [assess_run_quality, build_chart_data, _write_judge_report_with_feedback],
            "model": model_spec,
        },
    ]
