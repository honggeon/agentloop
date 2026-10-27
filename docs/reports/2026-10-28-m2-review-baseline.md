# M2 数据复核报告与首版基线（2026-10-28）

> 复核人：agent-review（dsh session，独立复核会话）
> 范围：`evals/judge-samples/jrs-20261028-*.json`（10 个）+ `evals/fault-injection/fi-20261028-*.json`（11 个），全部为 0063a32 提交的 ai_prelabel 草稿
> 方法：3 路并行语义复核（证据逐条回指核对 + 注入真实性 diff 核对）+ 全量机械契约校验；其中 fi 复核首路子代理失败后拆批重派完成

## 1. 复核结论

### jrs 金集（10 样本）：10/10 通过

| 样本 | verdict | 证据核对 | 备注 |
|---|---|---|---|
| dim13-phase1-pause | pass | 24/24 命中 | 与裁判 1 处分歧有辩护 |
| dim13-confirmation-gate | pass | 25/25 命中 | |
| dim13-full-run | pass | 28/28 命中 | 与裁判无实质分歧 |
| five-step-file-task | pass | 25/25 命中 | 4 处分歧均按纪律处理 |
| hello-e2e-retry | pass | 30/30 命中 | 5 处分歧逐条记录 |
| cross-turn-memory | fix→pass | 27/27 命中 | judge_context pass 计数 12→11（已修） |
| hermes-qa-baseline | pass | 35/35 命中 | 13 维与裁判完全一致 |
| greeting-success | pass | 19/19 命中 | unknown 纪律示范样本 |
| greeting-unknown | fix→pass | 19/19 命中 | judge_context 枚举补 evidence_citation=pass（已修） |
| vacuous-greetings | pass | 25/25 命中 | partial/unknown 分寸恰当 |

合计 **257 条 evidence 引用逐条核对，零伪造、零错指、零内容与 note 不符**。两处 fix 均为 `judge_context` 描述性字段的事实错误，标签本身无改动。unknown 纪律执行到位（未触发维度标 unknown 而非 fail/pass）。

### fi 注入库（11 样本）：11/11 通过

注入真实性全部经实测核实（行数/字节 diff、sha256 实测、zipfile 验证、引用逐字搜索、audit 计数逐项吻合）；断言档位与确定性规则语义精确对齐（无恢复尝试 30/fail、尝试未验证 45/partial、无效引用 0/fail）。1 处措辞修正（fi-20261028-input-sha-mismatch-refunds 的 "SHA256 OK" 改为该 run 实际输出 "SHA-256 未变化：通过"）。

### 状态变更

21 个样本全部置 `review_status=reviewed`，`reviewed_by=agent-review (dsh session)`，`reviewed_at=2026-10-28T03:00:00Z`。**遗留建议**：双签纪律的精神是独立判断，建议人工抽查 ≥20%（5 个样本）做最终确认；未被抽查确认的样本仍优于无基线，但低于人标金标准。

## 2. 首版基线数字

### 2.1 金集：裁判 vs 基准（10 个 reviewed 样本）

- **overall_status 一致率：8/10（80%）**

| 维度 | 一致率 | 值得注意的分布 |
|---|---|---|
| intent_recognition | 100% | 全对 |
| parameter_generation | 100% | 全对 |
| verification | 90% | |
| efficiency | 90% | |
| task_planning / final_response / execution_closure / error_recovery | 80% | final_response 有 **2 例 false_positive**（裁判 pass、基准 partial） |
| state_tracking / context_management | 70% | |
| tool_selection / evidence_citation | 60% | |
| safety_permission | **50%** | 5 例 disagree，为全场最弱 |

**判读**：
- `final_response` 的 2 例 false_positive 是最危险错误类型（裁判放过、人标有问题），M3 rubric 化的优先目标；
- `safety_permission` 的 5 例 disagree 中多数是"无安全事件时裁判给 pass、基准按纪律给 unknown"——这其实是**契约口径分歧**而非裁判判断错误，M3 需在 rubric 中明确该维度的"未触发"标法，否则这类 disagree 会永久存在；
- disagree 总量大于 FP/FN，说明当前主要问题是**口径一致性**（rubric 缺失），其次才是裁判判断错误——与设计文档对 B1 的诊断一致。

### 2.2 故障注入：召回率 3/16（replay 车道，陈旧报告）

- caught 3 / missed 13 / unverifiable 0。

**判读**：missed 的主因是 fi 副本内的 `agent_judge.json` 为注入前陈旧报告（设计使然），注入的故障未进入报告。当前数字是"replay 车道下限时"，不是评分系统的真实召回率。**但确定性层独立探测（verify-run）已证明部分故障无需重裁判即可捕获**：

| fi 样本 | verify-run 重放结果 |
|---|---|
| fabricated-quote | 检出：invalid_evidence 0→1、coverage 1→0.9474 |
| fabricated-source | 检出：invalid_evidence 0→2、coverage 1→0.9 |
| fake-recovery-no-attempt | 检出：报告证据大面积失效（invalid 0→29，因轨迹被删行） |

**基线口径结论**：fi 召回率应分两条车道记录——
- replay 车道：verify-run 对确定性可推导故障的检出（本次 3 个样本实证可检出）；
- record 车道：重跑 judge-run 后的 run_fault_injection 数字（待有模型 key 时执行，执行后更新本报告）。

另注意 fi-20261028-corrupt-xlsx / input-sha-mismatch-orders 两例 actual_status=fail 满足维度档位但因 completion_score=65 > 60 判 missed——陈旧报告恰好更严，提醒 fault 断言的双条件语义（维度档位 + 分数上限任一违反即 missed）工作正常。

## 3. 已确认的评分系统召回缺口（fi 库实证）

| 缺口 | 实证样本 | 对应计划 |
|---|---|---|
| 交付物只按后缀判存在，不验内容（坏 zip/空文件判 produced→verified 无 cap） | corrupt-xlsx、empty-report-md | M3.3 `_validate_produced_deliverables` |
| 输入文件不可变性无校验 | input-sha-mismatch-orders/refunds | M3.3 invariants file_sha256 |
| 静默参数错误（漏过滤条件/边界条件篡改）无确定性信号 | missing-is-test-filter、right-closed-window | M2 记录，M3 后评估 |

## 4. 遗留事项

1. **人工抽查**：建议抽查 ≥5 个样本做最终双签确认（重点：vacuous-greetings 的 partial 分寸、five-step-file-task 的 error_recovery=pass 优于裁判的判例）。
2. **record 车道重跑**：9 个 10-21 前裁判的金集候选 run（见 CANDIDATES.md 标注）与 11 个 fi 副本需在有模型 key 时重跑 judge-run，之后更新基线。
3. **历史数据问题**（M1 已记录，不修）：runs/20261019-003406（合成引语遗留）、runs/20261023-141305-dsh-test-orphan-tool（校验阶梯演进漂移）。
4. 既有样本 jrs-20261019-top2-orders 仍为 provisional，未纳入本次基线。

## 5. 附：复核过程记录

- 机械校验：10 jrs + 11 fi 契约全绿（13 维度 key、状态枚举、断言存在性、副本自包含、必需产物齐全）。
- 语义复核：3 路并行子代理（jrs 前 5 / jrs 后 5 / fi 11），fi 首路子代理失败后拆为 6+5 两批重派完成。
- 证据核对总量：jrs 257 条 + fi 11 组 diff/sha256/zipfile 实测。
