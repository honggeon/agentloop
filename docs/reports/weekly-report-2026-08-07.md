# Agentloop 项目周报

报告周期：2026-08-03 至 2026-08-07  
项目路径：`/Users/chen/agentloop`  
报告日期：2026-08-07

## 一、本周目标

本周围绕 Agentloop 项目完成从“轨迹采集与确定性审计工具”到“可视化评测平台雏形”的建设。核心目标包括：

- 在没有标准测试集的情况下，支持通过历史会话回放轨迹进行 Agent 行为分析。
- 建立工具复核能力，识别工具调用异常、敏感工具使用、缺失结果和任务完成风险。
- 将 Agent 语义分析能力做成独立模块，并使用 DeepAgent 框架组织主 Agent 与子 Agent。
- 建设前后端分离的可视化服务，使团队成员可以通过页面查看 run、审计结果和图表。

## 二、本周完成事项

### 1. Agentloop 基础 CLI 与轨迹分析

已完成黑盒模式的会话导出和本地分析能力，不依赖对 xuanjian-agent 服务打桩。

已支持命令：

```bash
python3 -m agentloop.cli fetch-session
python3 -m agentloop.cli smoke-chat
python3 -m agentloop.cli render-run
```

输出文件包括：

- `session.json`
- `summary.json`
- `trajectory.jsonl`
- `audit.json`
- `tool_audit.md`

当前已成功导入并分析多个历史 run，其中包括 PPT 生成任务和简单问候任务。

### 2. 确定性工具审计能力

本周补充了确定性审计逻辑，覆盖：

- 工具调用数和工具结果数统计。
- 工具调用与结果配对检查。
- 工具错误识别。
- `terminal`、`write_file`、`patch` 等敏感工具提示。
- 阶段推断与工具调用明细输出。

该能力作为 DeepAgent 分析前的事实层，避免语义报告直接基于主观判断下结论。

### 3. DeepAgent 分析模块

已将 Agent 分析能力做成明确模块：

```text
agentloop/agents/
  agent.py
  prompts.py
  schemas.py
  subagents.py
  tools.py
```

模块设计参考 `/Users/chen/Downloads/code-analysis-platform` 的写法，采用：

- `create_deep_agent` 主 Agent。
- `build_subagents(model_spec)` 子 Agent 注册。
- `langgraph.json` 图入口。
- 可选 DeepAgent 依赖，避免影响普通 CLI。

当前定义了 4 个子 Agent：

- `trajectory_analyst`：分析运行轨迹、阶段流转、完成信号和上下文断点。
- `tool_auditor`：分析工具调用、错误、重试、危险操作和恢复失败。
- `root_cause_analyst`：归纳根因链路，区分事实和推断。
- `report_writer`：生成结构化 JSON、Markdown 和图表数据。

新增命令：

```bash
python3 -m agentloop.cli judge-run --run runs/<run-dir>
```

输出文件：

- `agent_judge.json`
- `agent_judge.md`
- `chart_data.json`

### 4. FastAPI 后端服务

已完成后端服务骨架：

```text
backend/app/
  config.py
  main.py
  routes/
    runs.py
    judge.py
```

已提供 API：

- `GET /api/health`
- `GET /api/runs`
- `GET /api/runs/{run_id}`
- `GET /api/runs/{run_id}/audit`
- `GET /api/runs/{run_id}/judge`
- `POST /api/runs/{run_id}/judge`
- `GET /api/runs/{run_id}/charts`

后端已实现 run 路径安全校验，禁止通过 `../` 等方式访问 `AGENTLOOP_RUNS_DIR` 之外的文件。

### 5. Vue 前端 Dashboard

已完成 Vue 3 + Vite + ECharts 前端页面：

```text
frontend/
  src/
    App.vue
    api.ts
    main.ts
    styles.css
```

页面当前支持：

- run 列表展示。
- run summary 展示。
- 工具调用分布图。
- 阶段时间线图。
- 发现项等级图。
- 确定性审计发现项展示。
- DeepAgent 分析报告展示。
- 触发 DeepAgent 分析按钮。

前端首页直接进入可用 dashboard，没有做营销页或说明页。

### 6. 统一环境配置

已新增根目录 `.env.example`，前后端共享配置来源。

关键配置：

```bash
AGENTLOOP_RUNS_DIR=/Users/chen/agentloop/runs
BACKEND_HOST=0.0.0.0
BACKEND_PORT=18080
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

MODEL_PROVIDER=openai
MODEL_NAME=gpt-4.1
OPENAI_API_KEY=
DEEPSEEK_API_KEY=
CHECKPOINTER_TYPE=memory

VITE_API_BASE_URL=http://localhost:18080
```

安全边界：

- 后端读取模型 Key。
- 前端只读取 `VITE_` 开头的公开变量。
- 模型 Key 不返回给浏览器。

## 三、验证结果

Python 测试：

```text
uv run --extra dev pytest -q
28 passed, 1 warning
```

前端构建：

```text
cd frontend
npm run build
vite build succeeded
```

后端健康检查：

```text
GET http://127.0.0.1:18080/api/health
{"status":"ok"}
```

后端 run 列表接口已验证可以返回本地 `runs/` 下的历史运行记录。

当前本机服务地址：

- 前端：`http://127.0.0.1:5173`
- 后端：`http://127.0.0.1:18080`

## 四、当前风险与问题

1. 局域网访问还未开放

   当前服务绑定在 `127.0.0.1`，主要是因为后端没有认证。如果直接绑定 `0.0.0.0`，局域网用户可以访问 run 数据并触发 DeepAgent 分析，需要先增加访问令牌或其他鉴权机制。

2. DeepAgent 真实分析需要模型授权

   `judge-run` 或 `POST /api/runs/{run_id}/judge` 会把轨迹内容发送给配置的模型提供商。正式使用前需要明确模型提供商、API Key 和数据发送授权边界。

3. 前端依赖存在 npm audit 提示

   `npm install` 提示 2 个 moderate 级别漏洞。本周未执行 `npm audit fix --force`，避免强制升级导致构建不稳定。

4. FastAPI TestClient 有弃用警告

   当前测试通过，但依赖链提示 `StarletteDeprecationWarning`。短期不影响功能，后续可根据 Starlette/FastAPI 版本调整测试依赖。

5. 项目还不是 git 仓库

   当前无法通过 commit 记录阶段成果。建议后续初始化 git 或纳入现有代码仓库，便于评审、回滚和发布。

## 五、下周计划

1. 增加后端访问控制

   增加简单访问令牌，例如 `AGENTLOOP_API_TOKEN`，前端请求时带 `Authorization`，再开放 `0.0.0.0` 给团队访问。

2. 完善 DeepAgent 报告质量

   针对 PPT 任务这类复杂 run，补充完成度判断规则、交付物检查和事实/推断证据链。

3. 增加批量评测视图

   支持按 run 批次汇总成功率、工具错误率、平均工具调用数、常见失败根因。

4. 优化前端体验

   增加 run 搜索、筛选、报告详情抽屉、证据定位和 Markdown 报告预览。

5. 准备局域网部署

   在访问控制完成后，启动后端和前端 `0.0.0.0` 服务，并整理局域网访问说明。

## 六、总体结论

本周 Agentloop 已完成从命令行工具到可视化评测平台雏形的主要闭环：

- 可以采集 xuanjian-agent 历史会话。
- 可以生成标准化轨迹和确定性审计报告。
- 已具备 DeepAgent 多子 Agent 分析框架。
- 已具备 FastAPI + Vue 的前后端分离服务。
- 已通过自动化测试和前端构建验证。

下一阶段重点应放在访问控制、真实 DeepAgent 分析授权、批量评测指标和团队可访问部署上。
