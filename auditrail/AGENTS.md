# auditrail/ — 核心逻辑层

本目录是黑盒采集、确定性审计、运行时图/trace、LLM proxy 和 DeepAgent 裁判的实现层。看板 HTTP 装配在 `backend/`，Web UI 在 `frontend/`。跨层总规则见仓库根 `AGENTS.md`。

## 职责

- `client.py`：`AgentClient` 用 stdlib `urllib` 调用目标 agent HTTP API（session、chat SSE、上传、澄清、审批）。保持黑盒客户端，不要改成对 agent 内部的侵入式依赖。
- `trajectory.py`：从 session 响应提取消息 / LLM 调用 / 摘要，归一化轨迹；写 JSON/JSONL 用结构化序列化。
- `audit.py`：确定性工具审计（缺失 tool result、孤儿 tool result、工具错误、危险工具、final answer）并生成 Markdown。
- `runtime.py`：运行时图、trace、验收标准（expectations）、judge prompt 组装。
- `llm_proxy.py`：OpenAI 兼容 LLM 指标代理。运行时可用 FastAPI/Uvicorn，但缺少这些依赖时必须保留 stdlib fallback。
- `cli.py`：CLI 入口（`fetch-session`、`smoke-chat`、`render-run`、`judge-run`、`metrics-proxy`、`import-llm-calls`）。写 run 产物走 `_write_run_outputs`。
- `agents/`：DeepAgent 裁判。`schemas.py` / `evidence.py` / `dimensions.py` / `tools.py` 是确定性契约；`agent.py` 才加载 `.[agent]`。

`runs/<run_id>/` 文件是跨 CLI、后端、前端和裁判的共享契约。新增字段必须向后兼容已有报告。

## 依赖边界

- 禁止 `import backend` 或依赖 Web app / FastAPI 路由状态。
- 确定性路径（`client`、`trajectory`、`audit`、`runtime`、`llm_proxy` 的记录/转发逻辑、`agents/schemas|evidence|dimensions|tools`）不得要求模型凭据，不得要求安装 `.[agent]`。
- `agents/agent.py` 可延迟导入 `deepagents` / `langchain` / `langgraph`。缺依赖时抛明确 `RuntimeError`，不要静默降级成假报告。
- 外部 HTTP 失败必须显式暴露（如 `AuditrailHTTPError`），不要吞掉。

## 裁判契约

- 主 agent + 4 个子智能体：`trajectory_analyst`、`tool_auditor`、`root_cause_analyst`、`report_writer`。描述保持简短（`tests/test_agent_graph.py` 断言 `< 120`）。
- 提示词只放 `agents/prompts.py`，使用中文。
- 13 个维度 key / 中文标签以 `DIMENSION_DEFINITIONS` 为准。状态只能是 `pass` / `partial` / `fail` / `unknown`；无证据用 `unknown`，不要用 `fail` 或 0 分冒充失败。来源只能是 `deterministic` / `judge` / `deterministic+judge` / `missing`。
- finding 的事实结论必须能回指 `summary.json`、`audit.json`、`trajectory.jsonl` 或 `session.json`；证据不足用 `type=inference`。
- 写报告必须走 `write_judge_report`：归一化后写出 `agent_judge.json`、`agent_judge.md`、`chart_data.json`。不要另写一套 schema 或 Markdown 渲染。
- 完成度、交付物缺失、`completion_score` 封顶和 quality finding 由 `assess_run_quality` 负责。维度逻辑只做封顶/补全，不得推翻缺失交付物的失败结论。
- 确定性检查优先于 LLM 语义判断；不要用缺少证据的 LLM 判断拔高确定性失败。

## 测试

- 本层变更优先跑对应 pytest：`tests/test_audit.py`、`test_trajectory.py`、`test_runtime.py`、`test_llm_proxy.py`、`test_cli_judge_run.py`、`test_agent_*.py`。
- 确定性测试不得真实联网、不得依赖模型 API key、不得要求 `.[agent]`。需要 judge 调用时用 monkeypatch 假 `invoke_agent`。
- 涉及 `agent_judge.json`、`dimensions`、`judge_reliability`、`chart_data.json` 或 Markdown 渲染时，必须覆盖 schema 归一化、旧报告兼容和 `write_judge_report` 写路径。

## 禁止

- 不要在本包重写看板路由或 Vue 展示逻辑。
- 不要为未来假设增加抽象、flag 或兼容层。
- 不要把一次性计划写进本文件。
