# tests/ — pytest

本目录是仓库唯一的自动化测试入口。`pyproject.toml` 配置 `testpaths = ["tests"]`、`pythonpath = ["."]`。运行：`python3 -m pytest -q` 或 `uv run pytest -q`。没有 ruff/mypy 测试命令。跨层总规则见仓库根 `AGENTS.md`。

## 分层对照

- 核心包：`test_audit.py`、`test_trajectory.py`、`test_runtime.py`、`test_llm_proxy.py`、`test_cli_judge_run.py`
- 裁判：`test_agent_schemas.py`、`test_agent_evidence.py`、`test_agent_dimensions.py`、`test_agent_tools.py`、`test_agent_graph.py`
- 后端：`test_backend_routes.py`、`test_backend_config.py`、`test_backend_live.py`
- 前端源码断言：`test_frontend_app_state.py`

优先跑与改动相关的文件；跨模块或契约变更再跑全量 pytest。

## 确定性约束

确定性测试不得：

- 发起真实外网 / 真实 agent 服务调用
- 依赖 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY` 等模型凭据
- 要求安装 `.[agent]`（`deepagents` / `langchain` / `langgraph`）

需要裁判调用时 monkeypatch `invoke_agent`（见 `test_cli_judge_run.py`）。live SSE 用假 `AgentClient` 和 `Settings`（见 `test_backend_live.py`）。`test_llm_proxy.py` 可以起本地 stdlib/FastAPI 服务，不要打真实上游模型。

## 夹具与兼容

- 用 `tmp_path` 和手写小 JSON/JSONL 构造 run 目录。
- 不要硬编码仓库里真实的 `runs/2026...` 路径。
- 后端测试用 `fastapi.testclient.TestClient`。
- 涉及 `agent_judge.json`、`dimensions`、`judge_reliability`、`chart_data.json` 或 Markdown 渲染的改动，必须覆盖：
  1. schema 归一化（缺字段、非法枚举、旧报告）
  2. 旧报告兼容
  3. `write_judge_report` 写路径（文件确实写出且可再归一化）

`test_agent_graph.py` 会断言子智能体 `description` 长度 `< 120`，以及 prompt 中的完成度/交付物/dimensions 要求。改 `subagents.py` / `prompts.py` 时一起改测试。

## 前端源码断言

`test_frontend_app_state.py` 读取 `frontend/src` 源码做字符串断言，不是浏览器 E2E。改动以下内容时必须同步更新该文件：

- 路由 `path` / `name` / 组件名
- 侧栏和页面关键中文文案
- 面板名（如 Runtime Health、评测维度、Judge 可信度）
- `api.ts` 类型名（如 `JudgeDimension`、`JudgeReliability`、`RuntimeMetrics`）
- 状态展示（尤其 `unknown` →「未评估」）

前端类型检查和打包不在本目录执行，用 `cd frontend && npm run build`。

## 禁止

- 不要把测试改成依赖真实 `runs/` 产物或模型 API。
- 不要为了让测试通过而放宽生产契约（维度状态、证据 source、路径校验）。
- 不要把一次性计划写进本文件。
