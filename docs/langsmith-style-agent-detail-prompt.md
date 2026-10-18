# LangSmith 风格 Agent 详情页实现 Prompt

你是熟悉 Vue 3、TypeScript、FastAPI、pytest 和 ECharts 的资深全栈工程师。请在当前 Auditrail 仓库中，实现一个参考 LangSmith 信息架构、但使用 Auditrail 自有数据和视觉体系的 Agent 详情工作区。

## 一、必须遵守的项目边界

先阅读并遵守：

- `AGENTS.md`
- `docs/AGENTS.md`
- `frontend/AGENTS.md`
- `tests/AGENTS.md`
- `frontend/src/App.vue`
- `frontend/src/router.ts`
- `frontend/src/api.ts`
- `frontend/src/views/BatchView.vue`
- `frontend/src/views/RunView.vue`
- `frontend/src/views/TrajectoryView.vue`
- `backend/app/config.py`
- `backend/app/routes/runs.py`
- `backend/app/routes/platform.py`
- 相关 run index、audit、runtime、evaluation、judge schema 和聚合代码
- `tests/test_frontend_app_state.py` 及相关后端测试

代码和测试优先于设计文档。设计文档是产品意图，不是运行时 schema。

必须保持：

- `runs/<run_id>/` 既有文件契约和旧 Run 兼容。
- 确定性 audit 优先于 Judge 和外部证据。
- `unknown` 不得显示成失败或 0 分。
- `null`、空数组、`unknown`、`missing`、`estimated` 语义不能混淆。
- 核心 `auditrail/` 不得依赖 backend。
- 前端不得直接扫描 Run 文件或自行重写 audit/Judge 规则。
- 不暴露 API key、token、完整私密 prompt、completion 或未经脱敏的思考内容。
- 所有 `run_id`、`agent_id` 必须经过安全校验。
- 不修改无关文件，不提交 `dist/`、`node_modules/`、`.env`、真实 `runs/` 或凭据。

## 二、当前事实和实现策略

当前项目已经有文件型 Agent/TestCase/TestSuite 配置，但尚未完成数据库型 Agent 注册中心；ImportJob 的数据库 worker 已完成，但 SuiteJob 尚未完成跨进程调度。

本任务不是数据库迁移，也不是高可用改造。第一版只实现：

```text
已有 Agent 配置 + Run 产物后端聚合 + Agent 页面 + 现有 RunView/TrajectoryView 下钻
```

不要为了本页面新增 Agent 数据库、第二套 Run schema 或分布式 SuiteJob。

Agent 身份关联优先级：

```text
run.agent_id
→ source.agent_id
→ 已有 platform Agent 稳定 ID
→ run.agent_name / summary.agent_name
→ source.server_host
→ default-agent
```

实际字段必须以代码和真实 fixture 为准。请先检查现有字段，再决定映射。已有 Agent 无 Run 时也要出现在列表。无法关联的历史 Run 可以归入稳定派生 Agent，但必须返回并展示身份来源。

## 三、产品目标

新增三个命名路由：

```text
/agents                 Agent 列表
/agents/:agentId        Agent 详情
/agents/:agentId/runs   Agent 运行列表
```

详情页首屏顺序：

1. Agent Header：名称、稳定 ID、类型、环境、来源主机、最近运行、最近状态、身份来源。
2. Models：模型/provider、调用次数、Token、平均延迟、错误数。
3. Tools：工具名、调用次数、成功/失败、错误率、证据来源。
4. Traces per day：7d、30d、全部。
5. KPI：Trace count、Run error rate、Tool error rate、Completion score、Token count、Total cost、Judge evidence coverage。
6. Auditrail Health：任务完成、工具稳定、性能、成本效率、确定性 findings、高严重度 findings。
7. Recent Runs：点击进入现有 `/runs/:runId`。

页面用于发现异常和选择下钻对象，不复制现有 RunView 的完整内容。

## 四、后端实施要求

### 4.1 聚合服务

新增或扩展后端纯聚合服务，集中负责：

- Agent 列表和身份关联。
- Agent 详情。
- 时间范围过滤。
- Models 聚合。
- Tools 聚合。
- 每日趋势。
- KPI。
- Audit/Judge 健康摘要。
- Recent Runs 分页。
- 数据质量和局部产物缺失统计。

不要在路由中堆积大段聚合逻辑。可以在 backend 复用 auditrail 的纯函数，但不能让 auditrail 反向 import backend。

### 4.2 API

优先实现：

```text
GET /api/agents
GET /api/agents/{agent_id}
GET /api/agents/{agent_id}/overview?range=7d
GET /api/agents/{agent_id}/runs?range=7d&status=all&limit=50&offset=0
```

实际响应必须与现有 API 风格一致，并保持 additive。至少覆盖：

```text
agent
range
summary
models
tools
traces_by_day
health
runs
pagination
data_quality
```

建议字段语义：

- `identity_source`: `registered | run_metadata | derived`
- `association_confidence`: `exact | fallback | unknown`
- `run_error_rate`: 失败 Run / 可判定 Run
- `tool_error_rate`: 工具失败 / 可判定工具调用
- `cost_label`: `observed | imported | estimated | mixed | missing`
- `data_quality.partial`: 是否有 Run 被跳过或产物不完整

如果现有数据不能计算某字段，返回 `null` 或明确状态；不要伪造 0。

### 4.3 读取和性能

第一版可以读取现有 Run 产物，但必须：

- 使用现有 Run index；如果必须扫描，限制时间范围和最大数量。
- 支持分页。
- 单个坏 Run 不得让整个响应伪造为空。
- 对跳过的 Run 记录 `runs_skipped` 和 `partial`。
- 不在每个 API 请求中进行无界递归扫描。
- 不把文件绝对路径返回给前端。
- 为未来 Run Index/数据库聚合保留可替换的服务边界。

### 4.4 安全

- `agent_id` 拒绝空值、路径分隔符、`.`、`..` 和路径穿越。
- `run_id` 继续使用现有 `resolve_run_path` 或同等安全逻辑。
- API 错误显式返回，不要把异常吞掉后返回空列表。
- 不返回凭据、token、私密 prompt、完整 completion 或 chain-of-thought。

## 五、前端实施要求

### 5.1 API 类型优先

先在 `frontend/src/api.ts` 中定义并导出：

```ts
AgentOverview
AgentSummary
AgentModelMetric
AgentToolMetric
AgentTraceDay
AgentHealth
```

再增加统一请求封装。组件不得直接散落 `fetch`。

### 5.2 视图和路由

新增 Agent 列表、详情和 Runs 视图；路由必须有 `name` 和 `meta.title`。在 App 侧栏增加 Agents 入口，保持现有 Runs 分组和导航能力。

优先复用现有 CSS token、布局、状态组件、图表组件和错误提示，不新增前端依赖。

### 5.3 页面状态

必须实现：

- 独立 loading：Header、Models、Tools、趋势、KPI、Health、Runs。
- 无 Agent。
- Agent 无 Run。
- 无模型。
- 无工具。
- 无 Judge。
- 无时间字段。
- 局部 API 失败和重试。
- `unknown` 显示“未评估”。
- `missing` 显示“暂无数据/来源缺失”。
- 估算成本显示“预计/估算”。
- 没有 Run 与有 Run 但没有指标必须区分。

### 5.4 可访问性

- 成功、失败、高危、未评估不能只靠颜色区分。
- 图表提供数值摘要、表格或 accessible text。
- 图标按钮提供 `aria-label` 和 tooltip。
- 关键控件可键盘操作。
- 桌面端使用现有宽工作区；移动端可单列堆叠。

### 5.5 下钻

- Agent Header 的 Runs 进入 `/agents/:agentId/runs`。
- Recent Run 进入 `/runs/:runId`。
- RunView 的轨迹入口进入 `/runs/:runId/trajectory`。
- 不复制 RunView、TrajectoryView、Audit 或 Judge 的业务逻辑。

## 六、测试要求

先写或补测试，再完成实现。至少覆盖：

### 后端

- 无 Agent。
- 已注册 Agent 无 Run。
- 一个 Agent 多个 Run。
- 多 Agent 聚合不串数据。
- 派生 Agent 和已注册 Agent 的关联。
- 旧 Run 缺字段。
- 无 LLM 调用。
- 无 Judge 报告。
- 无时间字段。
- 成本缺失、估算和混合成本。
- `unknown` 与失败区分。
- 单个坏 Run 的部分失败语义。
- 7d、30d、all 范围。
- 分页和状态过滤。
- 非法 `agent_id`。
- Run 路径穿越。
- 敏感字段不会进入 overview 响应。
- 错误不会被静默转换成正常空数据。

### 前端

同步更新 `tests/test_frontend_app_state.py` 中与以下内容相关的源码断言：

- 新路由。
- 侧栏入口。
- 页面组件。
- API 类型。
- 关键中文文案。
- unknown → 未评估。
- Agent/Run 下钻链接。

不要把测试改成依赖真实 Run、真实 Agent、真实网络或模型凭据。

## 七、实施顺序

1. 阅读项目规则、现有 API、Run 产物和测试。
2. 列出实际可用字段和不能计算的字段，不得先猜 schema。
3. 实现 Agent 身份解析纯函数及其测试。
4. 实现后端聚合服务及 API 测试。
5. 更新 `frontend/src/api.ts` 类型和请求封装。
6. 增加 Agent 列表、详情、Runs 视图和命名路由。
7. 加入侧栏入口和 Run/Trajectory 下钻。
8. 补齐加载、空态、局部错误、旧 Run 和敏感字段处理。
9. 更新前端源码断言和后端测试。
10. 运行验证命令，检查 Git 状态，报告未实现能力。

## 八、验收标准

### 功能

- [ ] `/agents` 能列出 Agent，包含已有配置 Agent 和可解释的派生 Agent。
- [ ] `/agents/:agentId` 展示 Header、Models、Tools、趋势、KPI、Health、Recent Runs。
- [ ] `/agents/:agentId/runs` 支持范围、状态和分页。
- [ ] Recent Run 可以进入现有 RunView。
- [ ] RunView 可以进入现有 TrajectoryView。
- [ ] 7d/30d/all 的 API 和 UI 语义一致。
- [ ] 无数据、旧 Run、无 Judge、无 LLM、无时间和局部失败均有清晰状态。

### 契约和安全

- [ ] 新字段先在 `api.ts` 定义，再由视图使用。
- [ ] 聚合复用现有 Run、audit、evaluation、runtime 和 Judge 产物。
- [ ] 没有复制第二套 audit/Judge schema。
- [ ] `unknown`、`null`、空数组、`missing`、失败含义明确区分。
- [ ] `agent_id` 和 `run_id` 安全校验完整。
- [ ] 不返回凭据、私密 prompt 或未经授权内容。
- [ ] 单个坏 Run 不会导致整体伪造为空。
- [ ] 扫描有范围、分页、上限和跳过计数。

### 验证

```bash
python3 -m pytest tests/test_frontend_app_state.py -q
python3 -m pytest -q
cd frontend && npm run build
```

如果只实现页面骨架或静态 mock，必须在最终报告中明确列出尚未实现的 API、真实数据聚合和测试，不得声称功能已完成。

## 九、最终输出格式

完成后必须输出：

1. 修改文件清单。
2. 新增 API、路由和前端页面清单。
3. Agent 身份关联规则和数据来源。
4. `null`、空数组、unknown、missing、estimated、partial 的处理方式。
5. 安全和性能边界。
6. 测试和构建命令及结果。
7. 尚未实现的能力，以及哪些属于后续数据库化、SuiteJob 分布式执行或高可用阶段。
