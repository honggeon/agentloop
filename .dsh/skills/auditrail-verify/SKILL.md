---
name: auditrail-verify
description: Use when finishing or changing code in the auditrail repository — pick the right pytest subset for backend/core changes, npm run build for frontend changes, and know when tests may be skipped.
---

# auditrail 验证流程

完成代码或规范变更后，按改动范围选择验证命令。这是 auditrail 仓库 Definition of Done 的可执行清单。

## 后端 / 核心逻辑变更

- 先跑相关模块测试：`python3 -m pytest tests/test_xxx.py -q`（或 `uv run pytest -q`）
- 跨模块或契约变更（`runs/<run_id>/` 产物、judge schema、API 返回值）：`python3 -m pytest -q` 全量

## 前端变更

- `cd frontend && npm run build`（命令内含 `vue-tsc --noEmit` 与 `vite build`）
- 改动路由、关键文案、面板名、类型名或状态展示时，同步更新 `tests/test_frontend_app_state.py` 的源码字符串断言

## 硬约束

- 确定性测试不得发起真实网络调用，不得依赖模型 API key，不得要求安装 `.[agent]`
- 涉及 `agent_judge.json`、`dimensions`、`judge_reliability`、`chart_data.json` 或 Markdown 渲染的改动，必须覆盖 schema 归一化、旧报告兼容和写报告路径
- 测试夹具用 `tmp_path` 和手写小 JSON，不要硬编码真实 `runs/2026...` 目录

## 可以不跑的情形

- 仅文档改动：可以不跑测试，但必须在回复中明确说明未运行及原因

## 失败处理

测试或构建失败时，修复后再交付。不要通过注释断言、跳过测试、降低阈值等方式让输出变绿。
