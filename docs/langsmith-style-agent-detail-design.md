# Auditrail LangSmith 风格 Agent 详情页设计

## 1. 文档定位

本文定义 Auditrail Agent 详情工作区的产品结构、数据语义、API 边界和实施范围。页面借鉴 LangSmith 的信息架构：

```text
Agent → Environment → Runs/Traces → Trajectory/Tool/Audit/Judge
```

只借鉴信息层级和交互范式，不复制 LangSmith 的品牌、代码、图标、视觉资产或 SaaS 语义。

公开参考页面的组织数据不可作为 Auditrail 的产品事实。Auditrail 的事实来源仍然是现有 Agent 配置、`runs/<run_id>/` 运行产物和后端聚合结果。

本文是设计文档，不是运行时 schema。实现必须以代码、测试和现有 Run 产物契约为准。

## 2. 当前项目基线和关键结论

当前项目已经具备实现第一版所需的主要基础：

- 前端已有深色侧栏、Runs 入口、批量总览、单次 Run、Trajectory、Live 和测试平台。
- `RunView` 已展示 Runtime Summary、LLM 调用、工具分布、阶段时间线、审计、评测维度、Judge 可信度和 Markdown 导出。
- `frontend/src/api.ts` 已有运行、Runtime、LLM、Judge 相关类型和请求封装。
- 后端和 CLI 以 `runs/<run_id>/` 为共享契约。
- 当前已有文件型 Agent/TestCase/TestSuite 配置，主要位于 `platform/`；这不是数据库型 Agent 注册中心。
- ImportJob 已支持数据库存储和跨进程 worker，但 SuiteJob 尚未完成跨进程调度。

因此，第一版 Agent 详情页应采用：

```text
已有 Agent 配置 + Run 产物聚合 + 现有 RunView/TrajectoryView 下钻
```

不应等待 PostgreSQL、SuiteJob 分布式执行或多租户完成，也不应引入新的 Agent 数据库作为页面前置条件。

## 3. 目标与非目标

### 3.1 目标

1. 让 Agent 成为一级导航对象，而不是只展示孤立 Run。
2. 首屏回答：Agent 是什么、最近是否运行、使用了哪些模型和工具、是否存在错误。
3. 用后端聚合现有运行产物，前端只负责展示和交互。
4. 保持确定性审计优先和 Judge 证据可信度语义。
5. 从概览自然下钻到现有 Run、Trajectory、Audit 和 Judge 页面。
6. 对无数据、旧 Run、缺少 LLM、缺少 Judge、缺少时间和局部失败提供明确状态。
7. 保持 Vue 3 + TypeScript + ECharts 和现有视觉体系，不新增不必要依赖。
8. 为未来数据库索引和增量聚合保留稳定 API 边界。

### 3.2 非目标

本阶段不实现：

- LangSmith 的组织、项目、计费、SaaS 多租户或品牌能力。
- 新的 Agent 运行时、采集协议或 Run 文件格式。
- Agent 详情页自己的数据库或第二套审计/Judge schema。
- SuiteJob 跨机器调度、分布式锁或高可用执行。
- 实时流式 Agent 监控。
- 完整的 Agent 版本管理和模型治理。
- 在 Vue 组件中扫描文件或重新计算审计规则。

## 4. Agent 身份和 Run 关联规则

### 4.1 身份来源优先级

Agent 识别必须集中在后端纯函数中，推荐优先级：

```text
run.agent_id
→ source.agent_id
→ 已有 platform Agent 的稳定 ID
→ run.agent_name / summary.agent_name
→ source.server_host
→ default-agent
```

实际字段名称必须以现有代码和 Run 产物为准，不得凭文档臆造字段。

Agent API 应返回身份来源，避免用户误以为所有 Agent 都来自独立注册中心：

```json
{
  "identity_source": "registered|run_metadata|derived",
  "association_confidence": "exact|fallback|unknown"
}
```

已有 Agent 配置但尚无 Run 的 Agent 也应出现在 Agent 列表，并展示“尚无运行”。无法关联到注册 Agent 的历史 Run 可以归入稳定派生 Agent，但必须在界面标记“来源于运行数据”。

### 4.2 稳定 ID

`agent_id` 必须经过安全校验，不能直接作为任意路径拼接。派生 ID 必须是稳定、可重复计算且不包含路径分隔符的值。实现优先使用现有 ID 规范化函数；没有现成函数时，新增纯函数并覆盖边界测试。

## 5. 路由和页面结构

新增命名路由：

```text
/agents                 Agent 列表
/agents/:agentId        Agent 详情
/agents/:agentId/runs   Agent 运行列表
```

保留现有路由：

```text
/                       批量评测总览
/runs/:runId            单次运行详情
/runs/:runId/trajectory 单次轨迹
/live                   实时评测
/platform               测试平台
```

### 5.1 首屏层级

```text
Agent Header
  ↓
Models / Tools
  ↓
Traces per day
  ↓
KPI
  ↓
Auditrail Health / Judge & Audit
  ↓
Recent Runs
  ↓
现有 RunView / TrajectoryView
```

页面是“发现异常和选择下钻对象”的工作区，不复制 RunView 的全部细节。

### 5.2 Agent Header

展示：

- Agent 名称。
- 稳定 Agent ID，可复制。
- Agent 类型：External、DSH、ACP 或 Unknown；只有存在证据时展示。
- Environment：Production、Local 或 Unknown；没有来源证据时显示 Unknown。
- 来源主机；没有证据时显示“未知”。
- 最近运行时间；没有可信时间时显示“运行时间不可用”。
- 最近一次运行状态：成功、失败、运行中、未知。
- 身份来源：已注册、运行元数据、派生。

操作：

- 刷新。
- 查看全部 Runs。
- 返回批量总览。
- 复制 Agent ID。

### 5.3 Models 卡片

展示当前时间范围内有证据的模型聚合：

- 模型名称和 provider。
- LLM 调用次数。
- 总 Token。
- 平均延迟；无延迟证据时显示“未统计”。
- 失败调用数。

优先复用现有 Runtime/LLM dashboard 数据。不能仅凭模型名称猜测调用次数。

### 5.4 Tools 卡片

展示：

- 工具名称。
- 调用次数。
- 成功次数。
- 失败次数。
- 错误率及统计口径。
- 最近失败时间（有证据时）。
- 数据来源：确定性审计或运行指标。

工具错误不能直接等同于 Run 失败。

### 5.5 Traces per day

支持 7 天、30 天和全部范围；范围由后端过滤并在响应中回显。每个日期点可包含：

- 日期。
- Run 数。
- 成功 Run 数。
- 失败 Run 数。
- 工具调用数。
- 工具错误数。

没有可信日期时不能伪造日期。图表必须同时提供表格或文本摘要。点击日期可进入带日期筛选的 Agent Runs 页面；如果当前阶段不支持日期 query，下拉仍必须显示明确的未实现状态。

### 5.6 KPI

首期推荐：

1. Trace count：关联 Run 数。
2. Run error rate：失败 Run / 可判定 Run，必须标明口径。
3. Tool error rate：工具失败 / 可判定工具调用。
4. Completion score：有 Judge 报告且有完成度证据的 Run 的聚合值。
5. Token count：有 Token 证据的累计值。
6. Total cost：已有成本证据的累计值，并标记 observed/imported/estimated/mixed/missing。
7. Judge evidence coverage：有 Judge 证据的 Run 比例或证据覆盖率，必须明确具体口径。

“没有 Run”和“有 Run 但没有该指标”必须区分。无数据使用 `null` 或明确状态，不用 0 伪造。

### 5.7 Auditrail Health

展示摘要而非重新定义质量规则：

- 任务完成。
- 工具稳定性。
- 性能。
- 成本效率。
- 确定性审计发现数。
- 高严重度发现数。
- Judge 覆盖和可信度。

`unknown`、缺少产物、无 Judge 报告必须显示“未评估/暂无证据”，不得显示为失败。

### 5.8 Recent Runs

每行展示：

- 时间。
- Run ID。
- 来源主机。
- Run 状态。
- 完成度。
- 工具调用数。
- 审计发现数。
- Judge 状态。

整行进入 `/runs/:runId`。不在 Agent 页面复制完整轨迹或 Judge 报告。

## 6. API 设计

推荐只读接口：

```text
GET /api/agents
GET /api/agents/{agent_id}
GET /api/agents/{agent_id}/overview?range=7d
GET /api/agents/{agent_id}/runs?range=7d&status=all&limit=50&offset=0
```

推荐响应保持 additive，并包含数据范围和身份来源：

```json
{
  "agent": {
    "id": "rag-test-demo",
    "name": "rag_test_demo",
    "type": "External",
    "environment": "Unknown",
    "server_host": null,
    "last_run_at": null,
    "last_status": "unknown",
    "identity_source": "registered",
    "association_confidence": "exact"
  },
  "range": "7d",
  "summary": {
    "trace_count": 0,
    "run_error_rate": null,
    "tool_error_rate": null,
    "completion_score": null,
    "total_tokens": null,
    "total_cost": null,
    "cost_label": "missing",
    "judge_evidence_coverage": null
  },
  "models": [],
  "tools": [],
  "traces_by_day": [],
  "health": {
    "task_success": null,
    "tool_stability": null,
    "performance": null,
    "cost_efficiency": null,
    "deterministic_findings": 0,
    "high_severity_findings": 0
  },
  "runs": [],
  "pagination": {
    "limit": 50,
    "offset": 0,
    "total": 0,
    "has_more": false
  },
  "data_quality": {
    "runs_considered": 0,
    "runs_skipped": 0,
    "partial": false
  }
}
```

实现注意：

- 具体字段必须先核对现有 API 和产物，不能照抄示例 JSON。
- 聚合逻辑放在后端或 `auditrail/` 纯函数中。
- 所有 Run 路径通过 `resolve_run_path` 或等价安全逻辑访问。
- `agent_id`、`run_id` 拒绝路径穿越。
- 单个损坏或缺失 Run 不应静默伪造整体空数据；应返回 `data_quality.partial` 或显式错误。
- 不返回 API key、token、完整私密 prompt、completion 或未经脱敏的思考内容。
- 第一版允许受控扫描现有 Run，但必须有时间范围、分页、上限和可观测的跳过计数，为未来 Run Index/数据库聚合预留替换点。

## 7. 前端交互和状态

### 加载

Header、Models、Tools、趋势、KPI、Health、Runs 独立显示 loading，局部请求失败不清空整页。

### 空状态

- 无 Agent：暂无 Agent，先导入或运行一次会话。
- Agent 无 Run：该 Agent 尚无运行。
- 无模型：暂无 LLM 调用数据。
- 无工具：暂无工具调用数据。
- 无 Judge：尚未生成 Judge 报告。
- 无时间：运行时间不可用。
- 局部失败：该区域加载失败，可重新加载。

### 语义

- `null`：无法计算或没有数据。
- `[]`：明确的空列表。
- `unknown`：未评估。
- `missing`：来源不可用。
- `estimated`：成本为估算值。
- `partial`：部分 Run 被跳过或产物不完整。

颜色不能是唯一状态标识；所有状态同时提供文字。图表提供数值摘要、表格或 accessible text。图标按钮有 `aria-label` 和 tooltip。

## 8. 性能和演进策略

第一版不引入数据库，但不能为未来数据库迁移制造 API 依赖。必须：

- 统一后端聚合入口。
- 限制扫描范围和返回数量。
- 对 Run 读取做局部错误隔离。
- 不在每个 Vue 组件重复请求和聚合。
- 为后续 Run Index 或数据库聚合保留同一响应形状。
- 不把文件路径暴露给前端。

未来数据库化时，优先替换后端聚合实现，不改变页面路由和主要响应语义：

```text
文件扫描聚合 → Run Index → 数据库聚合/增量统计
```

## 9. 分阶段实施

### Phase A：第一版可用页面

- Agent 列表、详情、Runs 路由。
- 基于当前 Agent 配置和 Run 产物的后端聚合。
- Header、Models、Tools、趋势、KPI、Health、Recent Runs。
- 7d/30d/all 范围。
- RunView/TrajectoryView 下钻。
- 加载、空态、错误、旧 Run 兼容。

### Phase B：质量和证据增强

- 确定性 findings 和严重度。
- Judge evidence coverage 和 reliability。
- 失败根因和工具错误下钻。
- 数据质量提示和局部产物缺失明细。

### Phase C：索引和筛选

- Run Index 增量聚合。
- 模型、来源主机、状态筛选。
- 日期下钻。
- Agent 对比。
- 数据库聚合替换文件扫描。

## 10. 验收标准

### 功能

- [ ] `/agents` 能列出已有 Agent，并显示只有运行数据派生的 Agent。
- [ ] `/agents/:agentId` 展示 Header、Models、Tools、趋势、KPI、Health、Recent Runs。
- [ ] `/agents/:agentId/runs` 支持分页和范围参数。
- [ ] Recent Run 能进入现有 RunView，轨迹入口可用。
- [ ] 无 Run、无 LLM、无 Judge、无时间、旧 Run 和局部错误有明确状态。
- [ ] 7d/30d/all 的范围语义在 API 和 UI 中一致。

### 数据和安全

- [ ] Agent 关联规则有单元测试。
- [ ] 聚合复用现有产物和 schema，不复制审计/Judge 逻辑。
- [ ] `unknown`、`missing`、`null`、空数组和失败含义区分。
- [ ] `agent_id`、`run_id` 经过安全校验。
- [ ] 不暴露凭据、私密 prompt 或未经授权内容。
- [ ] 单个坏 Run 不会让整个页面伪造为空。
- [ ] 受控扫描有上限、分页和跳过计数。

### 视觉和可访问性

- [ ] 信息层级为 Agent 身份 → 能力 → 趋势 → 指标 → 下钻。
- [ ] 使用 Auditrail 自有 token，不复制 LangSmith 品牌资产。
- [ ] 图表有可读的数字或表格摘要。
- [ ] 关键控件可键盘操作。

### 验证

```bash
python3 -m pytest tests/test_frontend_app_state.py -q
python3 -m pytest -q
cd frontend && npm run build
```

如果只完成页面骨架，必须明确注明尚未实现的 API 和真实数据能力，不得把静态 mock 作为完成。
