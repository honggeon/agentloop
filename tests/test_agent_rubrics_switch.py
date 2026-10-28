"""AUDITRAIL_JUDGE_RUBRICS 消融开关测试（契约 C3 rubric 注入）。

默认行为必须保持不变：缺省（未设置环境变量）时 report_writer 提示词注入
DIMENSION_RUBRICS 锚点，与 REPORT_WRITER_PROMPT 常量逐字节一致；仅当显式
置为 off/0/false/no 时才回到 rubric 生效前的口径。
"""

from __future__ import annotations

import pytest

from auditrail.agents.prompts import (
    DIMENSION_RUBRICS,
    EVIDENCE_FORMAT_REQUIREMENT,
    JUDGE_RUBRICS_ENV,
    REPORT_WRITER_PROMPT,
    build_report_writer_prompt,
    judge_rubrics_enabled,
)


def test_default_injects_rubrics_byte_identical(monkeypatch):
    """缺省环境（未设置开关）提示词与默认常量逐字节一致。"""
    monkeypatch.delenv(JUDGE_RUBRICS_ENV, raising=False)
    assert build_report_writer_prompt() == REPORT_WRITER_PROMPT
    assert judge_rubrics_enabled({}) is True


def test_default_prompt_contains_all_required_check_ids():
    """默认提示词注入全部维度的锚点与 required_checks（钉住交付口径）。"""
    assert "维度评分锚点（rubric）" in REPORT_WRITER_PROMPT
    for key, rubric in DIMENSION_RUBRICS.items():
        for check in rubric["required_checks"]:
            assert check["id"] in REPORT_WRITER_PROMPT, (key, check["id"])


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
def test_off_skips_rubric_injection(monkeypatch, value):
    """off 时省略锚点、绑定语句与 required_checks，保留基础契约与证据格式。"""
    monkeypatch.setenv(JUDGE_RUBRICS_ENV, value)
    prompt = build_report_writer_prompt()
    assert prompt != REPORT_WRITER_PROMPT
    assert "维度评分锚点（rubric）" not in prompt
    assert "维度评分必须遵守文末的 rubric" not in prompt
    for key, rubric in DIMENSION_RUBRICS.items():
        for check in rubric["required_checks"]:
            assert check["id"] not in prompt, (key, check["id"])
    # 基础契约语句与证据格式要求不受开关影响。
    assert "没有证据时 status 必须是 unknown" in prompt
    assert "dimensions 缺少任何一个维度 key 都是结构不完整" in prompt
    assert EVIDENCE_FORMAT_REQUIREMENT.strip() in prompt
    assert judge_rubrics_enabled() is False


def test_build_subagents_reads_switch_at_build_time(monkeypatch):
    """build_subagents 在构建时读取开关：off 时 report_writer 提示词无锚点。"""
    from auditrail.agents.subagents import build_subagents

    monkeypatch.delenv(JUDGE_RUBRICS_ENV, raising=False)
    on_prompt = next(
        s["system_prompt"] for s in build_subagents("openai:test-model") if s["name"] == "report_writer"
    )
    assert on_prompt == REPORT_WRITER_PROMPT

    monkeypatch.setenv(JUDGE_RUBRICS_ENV, "off")
    off_prompt = next(
        s["system_prompt"] for s in build_subagents("openai:test-model") if s["name"] == "report_writer"
    )
    assert "维度评分锚点（rubric）" not in off_prompt
    assert "没有证据时 status 必须是 unknown" in off_prompt
