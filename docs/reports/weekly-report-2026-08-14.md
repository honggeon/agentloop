# Agentloop 项目周报

报告周期：2026-08-10 至 2026-08-14  
项目路径：`/Users/chen/agentloop`  
报告日期：2026-08-14

## 一、本周目标

本周围绕 Agentloop 从“离线 run 分析 Dashboard”推进到“运行时评测与指标闭环”。核心目标包括：

- 支持在页面上直接发起远程 Agent 评测，实时观察技能、工具、交互与最终审计结果。
- 将自然语言验收标准纳入 Agent Judge，使评测结果不仅看工具调用事实，也能判断任务交付是否达标。
- 补齐运行时健康度、LLM 调用、Token、延迟、成本和错误分析等指标。
- 在不能改造远端 Agent 源码的情况下，通过代理方式采集 LiteLLM / vLLM 的精确调用指标。
- 支持单个 run 导出 Markdown 报告，便于复盘、归档和对外同步。

## 二、本周完成事项

### 1. 运行时评测能力

已新增运行时评测链路，支持从 Dashboard 直接创建远端会话、发送任务、消费远端 SSE 事件并持久化 run。

后端新增能力：

- `POST /api/live/evaluate`：发起运行时评测并返回 SSE 事件流。
- `POST /api/live/evaluate-with-files`：支持携带文件发起评测。
- `POST /api/live/message/respond`：在已有会话上继续发送消息。
- `POST /api/live/clarify/respond`：响应远端澄清请求。
- `POST /api/live/approval/respond`：响应远端审批请求。

前端新增 `/live` 页面，已支持：

- 输入远端服务地址、用户、模型、任务内容和验收标准。
- 实时展示远端阶段、事件日志、运行图和 trace。
- 展示最终 run、确定性审计和 Agent Judge 入口。
- 上传文件并随任务传给远端 Agent。

### 2. Runtime Graph 与 Trace 建模

已新增 `agentloop/runtime.py`，将会话消息投影为运行时结构化视图。

当前支持：

- `build_runtime_graph(messages)`：生成节点和边，用于页面实时图展示。
- `build_runtime_trace(...)`：生成 Agent / Tool / Skill / LLM 风格的 span trace。
- `save_expectations(...)` / `load_expectations(...)`：保存和读取评测验收标准。
- 对工具错误、缺失结果、未完成工具调用和最终 assistant 回复进行状态判定。

该模块让 CLI、后端和前端可以共享同一套运行状态解释，不再只依赖页面临时拼装。

### 3. Agent Judge 验收标准集成

本周把自然语言验收标准接入 Judge 流程：

- 运行时评测会将用户输入的 acceptance criteria 写入 `expectations.json`。
- `judge-run` 和后端 Judge 接口会读取该文件，并把验收标准纳入评审 prompt。
- 新增测试覆盖验收标准被正确写入、读取和传递给 Judge 的场景。

这使 Agentloop 可以评估“是否符合业务要求”，而不仅是“有没有工具错误”。

### 4. 运行健康度与指标 Dashboard

后端 `runs` 路由新增运行指标构建能力，单个 run 当前可以输出：

- 生命周期阶段和整体状态。
- 执行步数、完成步数、失败步数、重试和超时统计。
- LLM 调用次数、输入 / 输出 / 总 Token、平均延迟和最大延迟。
- 工具调用成功 / 失败数量、平均耗时和超时数量。
- 错误分类、健康分、性能拆解和趋势摘要。

前端 `RunView` 已增加对应的运行健康度、LLM 指标、Token 分析、性能拆解、工具分布和错误诊断展示。

批量总览也补充了成功率、工具错误率、平均工具调用、根因分布和 run 分组能力，方便横向比较多次评测结果。

### 5. LLM Metrics Proxy

已新增 `agentloop/llm_proxy.py`，用于无侵入采集 OpenAI-compatible 模型调用指标。

实现内容：

- 支持启动本地 metrics proxy，转发 `/v1/chat/completions` 等 POST 请求到上游模型服务。
- 记录每次调用的模型、开始 / 结束时间、延迟、Token usage、状态和错误。
- 日志写入 JSONL，且不会保存 API Key。
- 对 streaming 请求自动补充 `stream_options.include_usage=true`，并解析最终 usage chunk。
- 支持通过 `agentloop import-llm-calls` 将代理日志导入 run，生成 `llm_calls.json`。
- 支持通过 `AGENTLOOP_LLM_PROXY_LOG` 在导入 / 运行时评测结束后自动按会话时间窗口导入调用记录。

配套文档已写入 `docs/litellm-llm-call-instrumentation.md`，包含启动、远端配置、校验、回滚和导入命令。

### 6. 单 run Markdown 导出

已新增单次评测报告导出能力：

- 后端新增 `GET /api/runs/{run_id}/export.md`。
- 后端根据 `summary.json`、`audit.json`、`agent_judge.json`、`chart_data.json` 和 runtime metrics 生成 Markdown。
- 缺少 Agent Judge 结果时不阻塞导出。
- 前端 `RunView` 新增“导出 Markdown”按钮，通过浏览器下载 `{run_id}-evaluation.md`。

导出内容覆盖 run 元信息、运行健康度、LLM 指标、工具指标、Judge 结论、发现项和图表摘要。

### 7. 前后端工程补强

本周前后端还完成了多处可用性补强：

- 后端 `main.py` 已注册 `runs`、`judge`、`live` 三组路由。
- 前端路由已包含批量总览 `/`、单 run 详情 `/runs/:runId`、运行时评测 `/live`。
- 前端 API 类型补齐了 RuntimeTrace、RuntimeMetrics、LLM Dashboard、BatchSummary、LiveEvaluatePayload 等结构。
- 运行列表按服务来源分组，支持删除 run、导入远端 session、查看趋势和返回总览。
- 新增多组前端源码状态测试，防止关键页面入口和操作回退。

## 三、运行与验证结果

本周本地 `runs/` 下共保留 12 个 run，其中本周新增或导入 6 个：

- `20260810-111525-db526f18-ae88-4d37-8b2f-7de0fdd9b348`：33 条消息、20 次工具调用、12 条 assistant 消息。
- `20260814-135434-0ee2b708-8ceb-49c5-b653-fe84453d7aae`：2 条消息。
- `20260814-145727-ac8b1012-d745-445b-a047-7dce76e02939`：2 条消息。
- `20260814-150802-52256d46-ec5a-4744-8a3d-be8a6227bfc3`：2 条消息。
- `20260814-173046-05ff258d-f0ae-4ac5-a648-370eb51135cc`：11 条消息、9 次工具调用。
- `20260814-175921-89433aa8-6993-47b3-ad81-c4c923348e5d`：6 条消息、4 次工具调用。

Python 测试：

```text
uv run pytest -q
95 passed, 1 warning
```

前端构建：

```text
npm run build
vue-tsc --noEmit && vite build
build succeeded
```

当前剩余提示：

- Python 测试存在 1 个 `StarletteDeprecationWarning`，不影响当前功能。
- 前端构建有 chunk 超过 500 kB 的 Vite 提示，当前不影响使用，后续可通过动态 import 或 manualChunks 优化。

## 四、当前风险与问题

1. 远端服务仍依赖人工配置

   LLM metrics proxy 已具备无侵入采集能力，但远端 Agent 需要把 OpenAI-compatible base URL 指向 proxy 后才能采集精确 LLM usage。当前文档已经给出操作步骤，正式接入前仍需确认远端环境和回滚窗口。

2. 实时评测链路依赖远端事件格式

   当前已适配远端 SSE 和常见交互阻塞提示，但如果远端事件字段继续变化，前端图、trace 或自动续跑逻辑需要同步调整。

3. Agent Judge 仍依赖模型授权和数据边界

   Judge 会把 run 轨迹和验收标准发送给配置的模型服务。正式用于业务数据前，需要明确模型提供商、API Key 管理和数据出境边界。

4. 指标成本目前仍是估算

   已能读取精确 Token usage；但成本估算依赖本地模型价格映射。后续如果要作为财务或资源看板使用，需要按实际模型供应商和计费规则补齐价格表。

5. 项目仍不是 Git 仓库

   当前无法通过 commit 历史精确追踪每个阶段改动。建议后续初始化 Git 或纳入正式仓库，便于代码评审、版本回滚和发布管理。

## 五、下周计划

- 将 LLM metrics proxy 接入远端测试环境，跑通真实模型调用采集、导入和 Dashboard 展示闭环。
- 优化运行时评测页面的 trace 可读性，重点增强失败定位、阻塞交互和续跑入口。
- 为批量评测增加更明确的筛选、排序和按服务来源对比能力。
- 补充 Markdown 导出样式和字段，形成可直接给业务或研发同步的评测报告模板。
- 根据 Starlette / Vite 提示处理依赖警告和前端包体拆分。
