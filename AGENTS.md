# AGENTS.md - auditrail Agent 工程规范

本文档面向在 `auditrail` 仓库中工作的 AI 编码代理。它只记录长期稳定、跨任务适用的项目级规则；一次性实施计划、阶段性设计和详细背景放在 `docs/` 或具体任务说明中。

## Project Overview

`auditrail` 是面向 `xuanjian-agent` 的黑盒轨迹采集、确定性审计和评测看板工具。项目不侵入目标 agent 服务，而是通过其 HTTP API 采集会话，归一化运行轨迹，并基于真实运行产物进行工具审计、DeepAgent 裁判和 Web 展示。

一次运行以 `runs/<run_id>/` 下的文件为契约。常见产物包括：

- `session.json`：原始会话响应。
- `summary.json`：会话摘要与来源元数据。
- `trajectory.jsonl`：归一化消息轨迹。
- `audit.json`、`tool_audit.md`：确定性工具审计。
- `source.json`、`expectations.json`、`llm_calls.json`：来源、验收标准和 LLM 调用信息。
- `agent_judge.json`、`agent_judge.md`、`chart_data.json`：DeepAgent 裁判与图表数据。

核心评测原则：确定性检查优先于 LLM 语义判断。确定性代码负责验证证据、交付物、工具调用和可计算分数封顶；LLM 裁判负责语义分析，但不能覆盖确定性失败结论。

## Tech Stack And Commands

- Python：`>=3.11,<4.0`，包配置在 `pyproject.toml`。
- 后端：FastAPI + Uvicorn，配置集中在 `backend/app/config.py`。
- 前端：Vue 3 + TypeScript + Vite + Vue Router + ECharts / `vue-echarts`。
- 可选 DeepAgent 裁判依赖：`.[agent]` extra，包含 `deepagents`、`langchain`、`langchain-deepseek`、`langgraph`、`langgraph-checkpoint`。
- 测试：pytest，`pyproject.toml` 已配置 `testpaths = ["tests"]` 和 `pythonpath = ["."]`。
- 当前没有独立的 lint、ruff、mypy 命令；不要在规范中假设它们存在。

常用命令：

```bash
uv run pytest -q
python3 -m pytest -q
cd frontend && npm run build
./start.sh
./start.sh status
python3 -m auditrail.cli fetch-session --base-url http://HOST:8788 --user-id ID --session-id SID
python3 -m auditrail.cli smoke-chat --base-url http://HOST:8788 --user-id ID --message "..."
python3 -m auditrail.cli render-run --run runs/<run-dir>
python3 -m auditrail.cli judge-run --run runs/<run-dir>
python3 -m auditrail.cli metrics-proxy --upstream-base-url http://HOST:PORT/v1
python3 -m auditrail.cli import-llm-calls --run runs/<run-dir> --log runs/proxy-llm-calls.jsonl
```

`frontend/node_modules/` 已被忽略。若缺依赖且需要联网安装，先确认再执行 `npm install`。

## Repository Structure

```text
auditrail/
  auditrail/                 # 核心 Python 包
    cli.py                   # CLI：采集、冒烟、重渲染、裁判、LLM 指标代理、调用导入
    client.py                # 黑盒 HTTP 客户端 AgentClient，基于 urllib
    trajectory.py            # 会话响应到 summary / trajectory / JSON(JSONL)
    audit.py                 # 确定性工具审计和 Markdown 报告
    runtime.py               # 运行时图、trace、expectations、judge prompt
    llm_proxy.py             # OpenAI 兼容 LLM 指标代理，含 stdlib fallback
    agents/                  # DeepAgent 裁判、工具、schema、证据和维度逻辑
  backend/app/               # FastAPI 看板后端
    config.py                # Settings、.env 加载、run 路径解析
    main.py                  # app 装配、CORS、路由、/api/health
    routes/                  # runs / judge / live API
  frontend/                  # Vue 3 + TS 前端
    src/api.ts               # API 类型和 fetch 封装
    src/router.ts            # batch / run / live 路由
    src/views/               # BatchView / RunView / LiveEvalView
    src/styles.css           # 全局样式
  tests/                     # pytest 测试
  docs/                      # 设计、报告、实施计划
  runs/                      # 本地运行产物，gitignore
```

## Architecture Rules

- `auditrail/` 是核心逻辑层。不要从核心包 import `backend`，不要让确定性采集、审计、schema 归一化依赖 Web app 状态。
- 后端只做 HTTP 装配、输入校验和核心函数复用。不要在 `backend/` 重写采集、审计、裁判或 Markdown 渲染规则。
- 按 `run_id` 访问运行目录时，必须通过 `backend.app.config.resolve_run_path` 或同等路径校验，拒绝 `..`、路径分隔符和越界访问。
- `runs/<run_id>/` 文件是跨 CLI、后端、前端和裁判的共享契约。新增字段必须向后兼容已有报告。
- 确定性逻辑不得要求模型凭据，也不得要求安装 `.[agent]`。
- `llm_proxy.py` 可以在运行时使用 FastAPI/Uvicorn，也必须保留缺少这些依赖时的 stdlib fallback 行为。
- 外部 HTTP 调用失败要显式暴露错误。后端路由将外部服务失败转换为合适的 `HTTPException` 或 SSE `error` 事件，不要静默吞错。

## Core Module Responsibilities

- `auditrail.client.AgentClient`：封装目标 agent API，包括 session、chat SSE、文件上传、澄清和审批响应。保持黑盒 HTTP 客户端定位。
- `auditrail.trajectory`：从原始 session 响应提取消息、LLM 调用、摘要和归一化轨迹。写 JSON/JSONL 时使用结构化序列化。
- `auditrail.audit`：基于消息和工具调用做确定性审计，包括缺失 tool result、孤儿 tool result、工具错误、危险工具和 final answer 检查。
- `auditrail.runtime`：构建运行时图和 trace，保存验收标准，组装 judge prompt。
- `auditrail.agents.schemas`：维护裁判报告 schema、维度定义、归一化和 Markdown 渲染。
- `auditrail.agents.evidence`、`dimensions`：确定性验证裁判证据和维度评分/封顶。
- `auditrail.agents.tools`：DeepAgent 可调用工具，工具函数要有稳定签名和 docstring。

## Agent / Judge Contract

DeepAgent 裁判由 1 个主 agent 和 4 个子智能体组成，定义在 `auditrail/agents/agent.py` 与 `auditrail/agents/subagents.py`：

- `trajectory_analyst`：轨迹、阶段流转、完成信号、上下文断点。
- `tool_auditor`：工具调用、错误、重试、危险操作、恢复情况。
- `root_cause_analyst`：根因链路，区分事实和推断。
- `report_writer`：结构化 JSON、Markdown 报告和图表数据。

约束：

- 子智能体描述保持简短，`tests/test_agent_graph.py` 断言长度小于 120。
- 提示词集中在 `auditrail/agents/prompts.py`，当前项目使用中文提示词。
- 13 个评测维度 key 和中文标签以 `DIMENSION_DEFINITIONS` 为准。前端可保留本地标签映射，但 key 必须一致。
- 维度状态只能使用 `pass`、`partial`、`fail`、`unknown`；无证据时使用 `unknown`，不要用 `fail` 或 0 分冒充失败。
- 维度来源只能使用 `deterministic`、`judge`、`deterministic+judge`、`missing`。
- finding 的事实性结论必须能回指 `summary.json`、`audit.json`、`trajectory.jsonl` 或 `session.json`；证据不足时使用 `type=inference`。
- 写裁判报告必须通过 `write_judge_report`，由它归一化并写出 `agent_judge.json`、`agent_judge.md` 和 `chart_data.json`。
- 完成度、交付物缺失、`completion_score` 封顶和 quality finding 由 `assess_run_quality` 负责，维度逻辑不要重复或推翻这类结论。

## Backend Rules

- `backend/app/main.py` 只负责创建 app、CORS、路由挂载和健康检查。
- 环境变量读取集中在 `backend/app/config.py`，默认读取项目根目录 `.env`。
- API 前缀统一为 `/api`，健康检查为 `/api/health`，返回 `{"status": "ok"}`。
- live 评测使用 `StreamingResponse` 输出 SSE。流式过程中的业务错误通过 `event: error` 返回。
- 路由层复用 `auditrail.cli._write_run_outputs`、`AgentClient`、`auditrail.agents.tools` 和 schema 渲染函数，不要复制核心逻辑。

## Frontend Rules

- 使用 Vue 3 `<script setup lang="ts">`、Composition API 和命名路由。
- API 类型、请求函数和 SSE 解析集中在 `frontend/src/api.ts`。组件内不要直接散落 `fetch`。
- 新增或修改后端返回字段时，先更新 `api.ts` 类型，再更新视图。
- 路由定义在 `frontend/src/router.ts`；新增页面使用命名路由和 `meta.title`，`afterEach` 负责设置 `document.title`。
- UI 文案保持中文。`unknown` 维度在界面显示为“未评估”，并与失败状态区分。
- 前端环境变量使用 `import.meta.env.VITE_API_BASE_URL`；`frontend/vite.config.ts` 的 `envDir: ".."` 允许复用根目录 `.env`。
- 前端变更后运行 `cd frontend && npm run build`，该命令包含 `vue-tsc --noEmit` 和 `vite build`。

## Testing And Verification

- 后端和核心变更优先运行相关 pytest；跨模块或契约变更运行 `python3 -m pytest -q` 或 `uv run pytest -q`。
- 前端变更运行 `cd frontend && npm run build`。
- 确定性测试不得发起真实网络调用，不得依赖模型 API key，不得要求安装 `.[agent]`。
- 后端测试使用 `fastapi.testclient.TestClient`；live SSE 测试通过 monkeypatch fake 客户端和设置。
- `tests/test_frontend_app_state.py` 是源码字符串断言。改动路由、关键文案、面板名、类型名或状态展示时，同步更新相关断言。
- 测试夹具使用 `tmp_path` 和手写小 JSON；不要硬编码真实 `runs/2026...` 运行目录。
- 涉及 `agent_judge.json`、`dimensions`、`judge_reliability`、`chart_data.json` 或 Markdown 渲染的改动，必须覆盖 schema 归一化、旧报告兼容和写报告路径。

## Engineering Rules

- 先阅读现有实现和测试，再修改。优先沿用当前模块边界、命名和数据结构。
- 改动范围保持克制，只触碰任务要求涉及的文件。
- 不要引入未配置的新工具链、格式化器、框架或运行时依赖；确有必要时先说明原因。
- 不提交 `.env`、凭据、`.logs/`、`runs/`、`frontend/dist/`、`frontend/node_modules/` 或 `.superpowers/` 产物。
- 仓库可能有他人未提交改动。不要回滚、重排或清理与当前任务无关的变更。
- 完成代码或规范变更后，提交仅包含本次任务相关文件；除非用户明确要求，不要 push。
- 若任务只修改文档，可不跑完整测试，但需要说明未运行测试及原因。
- 若修改影响用户命令、外部契约、环境变量或运行方式，同步更新 `README.md` 或对应 `docs/`。

## Prohibited Changes

- 不要把一次性计划、某个日期的实施步骤、临时验收清单或任务 prompt 写进本文件。
- 不要让核心确定性逻辑依赖 DeepAgent、模型凭据、真实网络或 Web app 运行状态。
- 不要绕过 `resolve_run_path` 直接拼接用户传入的 `run_id` 访问文件。
- 不要手写另一套 judge report schema、维度 key 或 Markdown 渲染规则。
- 不要用缺少证据的 LLM 判断拔高确定性失败结论。
- 不要为了未来假设增加抽象、flag 或兼容层。

## Definition Of Done

任务完成前确认：

- 代码或文档改动符合当前目录职责和依赖边界。
- 相关测试或构建命令已运行；未运行时给出具体原因。
- 涉及报告、运行产物或 API 返回值时，保持旧数据兼容。
- 未引入凭据、真实运行产物、构建产物或无关文件。
- Git 状态中只包含本次任务应有变更，或已明确说明已有无关改动。

