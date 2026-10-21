# auditrail → OTLP/MLflow 导出契约映射表（v1 待实现）

> 状态：v1 待实现（双评审三轮迭代后通过：事实核对 + 设计纪律均 PASS）。本文只定义映射规则与证据纪律，不引入任何新依赖。
> 基准：`/Users/chen/agentloop`，Run Contract v1（`auditrail/run_contract.py`）。
> v0.1：合并评审 A（事实核对）与评审 B（设计纪律）的首轮意见。
> v0.2：修复评审 A 复审发现的 llm_calls.json 双 schema（§5 重写为双形态对照 + 单位红线）、评审 B 的 2 个 minor（§3 工具 span 锚点退化、§6 turn_index 语义）。
> v0.3：修复评审 B 确认性复审发现的 proxy token 0 填充哨兵（§5 哨兵表 + §9 夹具第⑤条）与形态判定规则（§5 记录级键存在性）。

## 1. 定位与原则

- `runs/<run_id>/` 文件是唯一事实契约；OTLP/MLflow 导出是它的**单向、有损、带版本的视图**。导出结果永不回灌审计、裁判或评分路径。
- **导出器为满足 OTLP 协议生成的标识（trace_id/span_id）、根 span 和顺序边，是组织结构，不是被测 Agent 的执行证据。** 所有此类字段必须携带 provenance 标记，与观察值严格区分。
- 黑盒采集没有观察到的父子关系、耗时、token，一律保留缺失状态；不为生成"好看的 trace"补成事实。
- 映射层是可选集成，不得成为核心依赖；外部 HTTP/OTLP 发送失败显式暴露，不吞错（沿用 `auditrail/AGENTS.md` 依赖边界）。

## 2. Provenance 分类法与 wire 级标注约定

每个导出字段属于且仅属于以下三类之一：

| provenance | 含义 | 例子 |
|---|---|---|
| `observed` | 来源系统（被测 Agent / DSH 录制）直接报告的原始值 | provider-reported `token_usage`；DSH 消息的 epoch-millis `timestamp`；DSH tool-result 的 `is_error` 布尔旗标 |
| `derived` | 从 observed 值经确定性规则计算或推断，规则公开可查 | `latency_ms`（DSH：同 `(turn, step)` 事件时间差；proxy：代理侧墙钟测量）；`provider`（从模型名字符串推断）；按 `tool_call_id` 配对的工具调用↔结果 |
| `synthetic` | 导出器为协议/展示生成的值，**不构成证据** | trace_id/span_id、根 span、顺序占位时间、顺序边 |

**wire 级标注约定（强制）**：凡 §6 表格 provenance 列含非 `observed` 的字段，导出时必须携带 OTLP 属性，命名约定 `auditrail.provenance.<field>`，取值 `observed|derived|synthetic`。已命名的专用标记（`auditrail.time_basis`、`auditrail.status_basis`、`auditrail.id_anchor`、`auditrail.truncated`、`auditrail.missing`、`auditrail.orphan` 等）视作该约定的具名实例。**仅在本文档标注而无 wire 属性的，视为未标注。**

## 3. 映射版本与稳定标识生成规则

仿照 `integrations/langfuse.py` 的 `LANGFUSE_COMPAT_VERSION` 模式：

- **`auditrail.mapping_version = "otel-map-1"`**：标识本映射规则版本。任何字段增删、provenance 重分类、ID 规则变更都必须递增版本并更新本文档。
- **trace_id**：`SHA-256("auditrail:otel-map-1:" + run_id)` 取前 16 字节转 hex。同一 run 重复导出产生同一 trace_id，幂等。
- **span_id**：`SHA-256(trace_id + ":" + span_key)` 取前 8 字节转 hex。`span_key` 由证据锚点构成，而非数组下标：
  - 工具执行 span：`"tool:" + tool_call_id`（`tool_call_id` 是来源系统分配的 observed 标识）；无 `tool_call_id` 时退化为 `"idx:" + trajectory index + ":" + call index`，标 `auditrail.id_anchor = "index"`——该合成锚点只用于 span_id，与 §6"省略 `gen_ai.tool.call.id`"配套，禁止回填属性；
  - LLM 调用 span：`"llm:" + <call 标识>`——`llm_calls.json` 存在双 schema（见 §5），DSH 形态取 `call_id`、proxy 形态取 `callId`。锚点分级：DSH `message.id` 存在时为 observed 锚点；DSH 的 `"turn%s-step%s"` 构造回退（`dsh_source.py:409`）为 derived 锚点（标 `auditrail.id_anchor = "turn_step"`）；proxy 的 response-id 为 observed 锚点，其 `uuid4()` 回退（`llm_proxy.py:210`）一旦写入 artifact 虽稳定但 provenance 为 synthetic（标 `auditrail.id_anchor = "uuid"`）；无 call 标识时退化为 `"msg:" + message_id`（observed）；皆无时才允许 `"idx:" + trajectory index`（synthetic 锚点，标 `auditrail.id_anchor = "index"`）。
- **run_id 永不直接用作 OTLP trace_id**；trace_id 是派生标识，run_id 以 `auditrail.run_id` 属性原样保留。
- **锚点质量必须可观测**：导出器在根 span 输出锚点分布统计 `auditrail.id_anchor_stats`（如 `{"tool":188,"llm":118,"turn_step":0,"index":0}`）。已知风险：本地 `runs/` 全部为 DSH 来源，xuanjian HTTP 来源消息的 `id` 字段覆盖率未验证（`trajectory.py:31` 只透传 `message.get("id")`）；若普遍缺失，span_id 退化到轨迹下标，`render-run` 重归一化导致消息增删时下游 span_id 会漂移。**首次接入真实 xuanjian run 时人工核对该分布。**

## 4. 时间纪律

| 情形 | 处理 |
|---|---|
| 消息带 observed `timestamp`（epoch millis，DSH 录制） | 使用，换算为 `start_time_unix_nano`；`auditrail.time_basis = "observed"`。**同毫秒时间戳实测存在**（相邻消息 timestamp 相同）：按轨迹 index 稳定排序做 tie-break，顺序本身标 derived |
| observed start + 无 `latency_ms` | start 用 observed 时间；**end 不得等于 start**（零时长是虚构事实）；按顺序占位生成 end 并标 `auditrail.duration_basis = "synthetic"` |
| `llm_calls.json` 带 `latency_ms` | 作为该 LLM span 的 duration。**provenance = derived**：DSH 路径是同 `(turn, step)` 的事件时间差（`dsh_source.py:425-428`），proxy 路径是代理侧墙钟测量，均非 provider 直接报告；标 `auditrail.provenance.duration = "derived"` 并注明口径 |
| 消息无 timestamp | span 仍需满足 OTLP wire format：按轨迹顺序以单调占位时间发出，**必须**标 `auditrail.time_basis = "synthetic_order_only"`。该时间只表达顺序，不表达任何墙钟事实 |
| 根 span 起止时间 | 取子 span 最早 start / 最晚 end；`time_basis` 取全部子 span 中**最弱**一级（任一子 span 为 `synthetic_order_only` 则根同为 `synthetic_order_only`）；**禁止用导出时刻或 `run.json` 采集时钟填充** |
| `runtime.py build_runtime_trace` 的时间 | **禁止作为导出输入**。其 `base_time = datetime.now()`（构建时刻，runtime.py:94）+ 每消息 +1s（runtime.py:271-272）、`latency` 默认 0（runtime.py:252），是纯合成展示数据，导出器不得复用 |

`run.json.started_at/finished_at` 是**采集侧平台时钟**（`run_contract.py:140-162` 的 `update_run_status` 写入），不是 Agent 执行时钟；只可作为资源级属性 `auditrail.collection.started_at/finished_at`，不得用作任何 span 的执行起止时间。

## 5. llm_calls.json 双 schema 与来源归一化哨兵值

**`llm_calls.json` 存在两种字段形态**（`costs.py:179` 的双读代码 `raw.get("tokenUsage") or raw.get("token_usage")` 印证了并存事实），导出器必须先识别形态再取值。形态按**记录级**键存在性判定：含 `callId`/`tokenUsage` 为 proxy 形态，含 `call_id`/`token_usage` 为 DSH 形态；`import_proxy_log` 覆盖写使实践中单文件单形态，但导出器须按记录级容忍混存（§6 的双形态 `|` 取值即为此服务）：

| 语义 | DSH 形态（`dsh_source.events_to_llm_calls`） | proxy 形态（`llm_proxy.build_llm_call_record`，llm_proxy.py:198-219，经 `import_proxy_log` 原样透传） |
|---|---|---|
| call 标识 | `call_id` | `callId`（response-id，缺失时 `uuid4()` 合成） |
| token 用量 | `token_usage.{input_tokens, output_tokens, total_tokens, cache_read_tokens, cache_write_tokens}` | `tokenUsage.{inputTokens, outputTokens, totalTokens}`（**无 cache 字段**） |
| 耗时 | `latency_ms`（毫秒，事件时间差） | `latency`（**秒**，`perf_counter` 差值，llm_proxy.py:57）＋ `startTime`/`endTime`（ISO，代理墙钟） |
| 模型 / 提供商 | `model` / `provider` | `model` / `provider` |
| 状态 | `status` | `status` |

**单位红线：proxy 形态的 `latency` 是秒，误当毫秒导出会产生 1000 倍错误。** proxy 记录优先直接使用 `startTime`/`endTime` 作为 span 起止（代理墙钟，derived）；只用 `latency` 时必须 ×1000 换算并标 `auditrail.provenance.duration = "derived"` + `auditrail.duration_unit_source = "seconds"`。

**哨兵值（一律视为缺失，不得当 observed 导出）：**

| artifact 字段 | 哨兵值 | 来源形态 | 导出处理 |
|---|---|---|---|
| `model` | `"unknown"` | DSH（`dsh_source.py:413` 模型缺失时写入） | 省略 `gen_ai.request.model` |
| `provider` | `""`（非 DeepSeek 模型时，`dsh_source.py:414`）；`"proxy"`（常量，`llm_proxy.py:212`） | 两种形态 | 省略 `gen_ai.provider.name` |
| `status` | DSH：`"success"` **逐调用硬编码常量**（`dsh_source.py:423`）；proxy：`"success" if status_code < 400 and error is None else "failed"`（derived-from-observed） | 两种形态 | v1 一律不映射 span status（UNSET）；两来源的丢弃理由不同：DSH 是无证据常量，proxy 是派生值且 v1 选择不映射 |
| `tokenUsage.{inputTokens,outputTokens,totalTokens}` | `0`（proxy 路径 `_int_value` 对缺失键返回 0，llm_proxy.py:177-186,427-433；真实调用 input tokens 不可能为 0） | proxy 形态 | 视为缺失，省略对应 `gen_ai.usage.*` 属性，**禁止把 0 当 observed 导出** |

另：proxy 形态 `totalTokens` 在 total 缺失时由 input+output 合成（llm_proxy.py:180-181），属 derived；v1 不导出 total_tokens 故不受影响，未来若导出须标 `auditrail.provenance.usage_total = "derived"`。

## 6. Span 结构映射表

组织结构：1 个导出器生成的根 span（`auditrail.provenance.root = "synthetic"`，仅作分组容器），下挂 observed/derived span。

| 导出对象 | OTLP / gen_ai 属性 | 来源（artifact.field） | provenance | 缺失处理 | 转换损失 / 备注 |
|---|---|---|---|---|---|
| 根 span name | span name = `auditrail.run` | 导出器常量 | synthetic | — | 组织结构，非证据 |
| run 标识 | `auditrail.run_id`（自定义属性） | `run.json.run_id` | observed | 缺失则不导出该 run | — |
| run 生命周期 | `auditrail.run.status` / `status_reason` | `run.json` | observed（平台侧） | 旧 run 走 `load_run_state` 推断值时标 `derived`（`legacy_inferred=true`） | 语义是采集生命周期，禁止与 judge 状态混用（契约已区分三套状态机） |
| Agent 身份 | `auditrail.agent.ref` | `run.json.agent_ref` / `summary.json.model` | observed | 缺则省略属性 | `summary.model` 是会话自报名（如 `codex/gpt-5.6-luna`），不是 API 级 model |
| 用户输入 | 根 span 的 `gen_ai.input.messages` | `trajectory.jsonl` role=user 消息 content | **observed（内容）/ derived（跨轮归组）** | content 为空则省略该条 | 真实会话是多轮的（样本 run 有 169 条消息）；每条消息携带 `auditrail.turn_index` 与 `auditrail.message_index`——**`turn_index` 按 role=user 消息出现顺序从 0 递增，同一轮内的后续消息继承该值**——并标 `auditrail.provenance.input_grouping = "derived"`；不丢弃 system-reminder 注入消息 |
| LLM 调用 span | `gen_ai.operation.name = "chat"` | `trajectory.jsonl` assistant 消息 或 `llm_calls.json` 条目 | observed（存在性）/ derived（边界划分，标 `auditrail.provenance.span_boundary`） | — | 黑盒视角下"哪几条消息属于同一次 LLM 调用"是推断：有 `llm_calls.json` 时以其为准，否则按 assistant 消息逐条成 span 并标 derived |
| LLM span status | （不映射） | — | — | 一律 `UNSET`（v1） | 两来源的丢弃理由不同（见 §5）：DSH 的 status 是硬编码常量，proxy 的 status 是 derived-from-observed 但 v1 选择不映射；`finish_reason` 仅保留为 `auditrail.llm.finish_reason` 自定义属性 |
| 模型 | `gen_ai.request.model` | `llm_calls[].model` 优先，其次 `summary.json.model` | observed | **缺失或值为 `"unknown"` 则省略** | 两来源不一致时以 llm_calls 为准并记 `auditrail.model_conflict` |
| 提供商 | `gen_ai.provider.name` | `llm_calls[].provider` | **derived**（从模型名推断，`dsh_source.py:414`） | **缺失、空串或 `"proxy"` 则省略** | 导出值归一到 semconv 小写枚举（如 `DeepSeek` → `deepseek`）；标 `auditrail.provenance.provider = "derived"` |
| token 用量 | `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens` / `gen_ai.usage.cache_read.input_tokens` | `llm_calls[].token_usage.input_tokens \| tokenUsage.inputTokens`（双形态取值，见 §5；provider-reported） | observed | **缺失即省略属性，禁止填 0**（0 是事实断言，缺失不是 0）；**proxy 形态无 cache 字段，cache 属性对 proxy 记录一律省略** | **口径差异（必须标注）**：semconv 规定 `gen_ai.usage.input_tokens` SHOULD 含 cached tokens，而 DSH 源数据的 `input_tokens` **不含** cache（实测 232 vs cache_read 11392）；导出时保留源值并标 `auditrail.usage.input_excludes_cache = true`；cache_read 用 semconv 已定义的 `gen_ai.usage.cache_read.input_tokens`（Development 状态）；cache_write 无对应 semconv 字段，用自定义 `auditrail.usage.cache_write_tokens` |
| LLM 耗时 | span duration | DSH 形态 `llm_calls[].latency_ms`（毫秒）；proxy 形态优先 `startTime`/`endTime` 直接作 span 起止，或用 `latency`（**秒**，必须 ×1000，见 §5 单位红线） | **derived**（DSH：事件时间差，dsh_source.py:425-428；proxy：代理墙钟，见 §4） | 缺失则只标顺序（§4） | — |
| 工具执行 span | `gen_ai.operation.name = "execute_tool"`、`gen_ai.tool.name`、`gen_ai.tool.call.id` | `trajectory.jsonl` assistant.tool_calls[] 的 name / id | observed | tool_call_id 缺失时**省略 `gen_ai.tool.call.id`**（semconv 要求 if-and-only-if 来源提供），§3 合成锚点只用于 span_id，**禁止回填**该属性 | `skill_view` 在 runtime.py 里被特判为 "Skill" 类型是**展示层分类**，导出时一律 `execute_tool`，原分类保留在 `auditrail.tool.display_type` |
| 工具入参 | `gen_ai.tool.call.arguments` | tool_calls[].function.arguments | observed | **缺失或为空串则省略该属性**（空串会被误读为"空调用"事实） | 超长参数按 `runtime._preview` 同口径截断时，标 `auditrail.truncated = true` |
| 工具结果 | 同一 execute_tool span 的 `gen_ai.tool.call.result`（semconv Opt-In 字段） | `trajectory.jsonl` role=tool 消息的 content | observed（内容）/ derived（配对） | 无匹配 tool result：span 保持无 result，标 `auditrail.missing = "tool_result"`（与 `audit.py` 缺失 tool result 检查语义一致）；**孤儿 tool result 必须导出为根 span 的直接子 span**，携带 `auditrail.orphan = true`、observed content 与 `auditrail.missing = "tool_call"`——content 是审计证据，禁止丢弃 | 配对依据是 `tool_call_id` 精确匹配（observed 关联） |
| 工具/调用状态 | span status | `audit.py:235` `_is_tool_error` 判定链，按优先级：① 来源 `is_error` 布尔旗标（DSH tool-result blocks）→ ② 显式 exit code（结构化字段或 `[exit code: N]` 文本标记）→ ③ 内容启发式 | ① observed（`auditrail.status_basis = "observed_flag"`）；② 结构化字段 observed / 文本标记 derived；③ derived（`auditrail.status_basis = "heuristic"`） | 三种信号皆无则 `UNSET`，不猜 OK | **启发式不得覆盖 observed 阴性信号**：`is_error=false` 或 exit code=0 时，不得因内容含 error 字样改判 failed（`audit.py:240-244` 注释明确的既有语义）。注意：DSH 来源的 is_error 阴性形态是**字段缺省**而非 `false`（`dsh_source.py:340-341,376-377` 只在为真时写 `is_error=True`），真实数据中 observed_flag 的阴性实例极少出现。另：`status=="error"` 是 runtime.py:60,179 的展示层条件，不是 audit.py 的判定依据 |
| LLM 输入/输出消息 | `gen_ai.input.messages` / `gen_ai.output.messages` | assistant 消息 content / tool_calls 摘要 | observed（内容）/ derived（重组为消息数组，标 `auditrail.provenance.messages`） | **content 为空且无 tool_calls 时省略 output.messages，空串不导出** | `reasoning_content`/`reasoning_details` 等字段 gen_ai 无对应，v1 保留为自定义属性 `auditrail.message.reasoning` |
| 费用 | 自定义 `auditrail.cost.amount/currency/source/price_version` | `costs.py` 记录，经 `records_for_run(records, run_id)`（costs.py:634）关联 | 按 `COST_SOURCES` 原样：`observed/imported/estimated/missing` | `missing` 时不导出费用属性 | gen_ai semconv 无费用字段；`estimated` 必须携带 `price_version`；estimated/imported 永不覆盖 observed（沿用 costs.py 既有纪律）。挂载点：run 级费用挂根 span；call 级按 `source_id ↔ llm_calls[].call_id \| callId`（双形态，见 §5）关联挂对应 LLM span；关联不上的只挂根 span |
| 父子关系 | span parent | 根 span ← 全部顶层 span（synthetic 组织）；execute_tool span 与其 tool result 的关联（observed tool_call_id） | 逐边标注（`auditrail.provenance.edge`） | — | 消息顺序边是 derived（顺序 observed，"因果父子"语义是导出器解释） |

## 7. MLflow 摄取侧前提（部署约束）

§9 的回读对照验证依赖以下条件（MLflow 官方文档确认）：

- MLflow **3.6.0+**；tracking server 必须配置 **SQL backend store**；
- OTLP 摄取端点为 `/v1/traces`，**仅支持 OTLP/HTTP**（不支持 gRPC）；
- 每个请求必须携带 **`x-mlflow-experiment-id`** header 指定目标 experiment。

## 8. 不可表示 / 明确丢弃清单（v1）

以下信息在 v1 映射中**不导出**，避免派生值被误读为证据：

- `runtime.py` 运行图的节点/边与全部合成时间（见 §4）；
- 裁判报告（`agent_judge.json`）、维度评分、审计结论——评测结果不属于执行 trace，将来如需进 MLflow 应走其 assessment/feedback 通道并单独设计；
- `error.json` 的 Phase 0 错误协议——采集失败信息，将来可作为根 span 上的 `auditrail.collection.error` 事件，v1 暂不映射。

## 9. 验证要点（实现时）

- **幂等**：同一 run 重复导出产生完全相同的 trace_id/span_id 集合。
- **缺失纪律**：构造无 timestamp、无 llm_calls、无 tool result、孤儿 result 的夹具，断言缺失属性被省略而非填 0/空串，且 provenance 标记正确。
- **哨兵值夹具**：含 `model="unknown"` / `provider=""` / `status="success"` 的 `llm_calls.json`，断言属性省略 / LLM span status 为 UNSET。
- **双 schema 夹具**（§5）：proxy 形态 `llm_calls.json`（`callId`/`tokenUsage`/`latency`），断言：① `latency=1.527`（秒）导出为 1527ms 或使用 `startTime`/`endTime`，**不得出现 1000 倍或 1/1000 倍错误**；② cache 属性省略；③ `provider="proxy"` 省略；④ `uuid4()` 回退 callId 的 span 标 `auditrail.id_anchor = "uuid"`；⑤ `tokenUsage` 全 0 的 proxy 记录，断言所有 `gen_ai.usage.*` 属性被省略。
- **根 span 时间**：断言 `time_basis` 取子 span 最弱一级，且根 span 起止不等于导出时刻、不等于 `run.json` 采集时钟。
- **锚点退化**：无 message_id/call_id 夹具，断言 `auditrail.id_anchor = "index"` 与根 span `auditrail.id_anchor_stats` 存在且计数正确。
- **状态优先级**：`is_error=false` + 内容含 error 字样的夹具，断言导出 status 不为 failed（启发式未覆盖 observed 阴性信号）。
- **回读对照**：导出后从 MLflow 读回，**仅对照 provenance=observed 字段**；预先定义 MLflow 摄取侧归一化（如属性名大小写、时间精度截断）的容差白名单，容差外差异才判失败。
- **确定性测试不联网**：OTLP 发送层用 fake exporter 断言 payload，不打真实端点（沿用 `auditrail/AGENTS.md` 测试约束）。
