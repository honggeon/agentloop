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

## 6. M3 锚点扩展验收（2026-10-28，rubric/规则版本 v1）

规则版本：`DIMENSION_RUBRICS` v1（C3，13 维度三档锚点 + required_checks）、`intent_recognition_cap`（C4，第 5 条确定性维度规则）、交付物内容校验 + `expectations.json` invariants `file_sha256` v1（C5）。三个子任务各自独立提交（43f7ce1 / 90bfd3d / 469bdc0），每步后重跑对比。

### 6.1 金集（replay 车道）：与 §2.1 基线逐字节一致

M3 三个子任务均不改写已存 `agent_judge.json`（rubric 只影响重跑裁判后的新报告；新确定性规则只在新写入时生效），因此聚合数字不变：overall 一致率 80%，各维度 agreement 与 §2.1 相同，**无恶化**。record 车道重跑（rubric 生效后的裁判分）仍待模型 key，届时本节需更新。

### 6.2 fi 注入库：replay 检出面扩大

- `run_fault_injection`（消费已存报告）仍为 caught 3/16，与基线一致——陈旧报告口径不变。
- **确定性层独立检出新增 4 例**（`assess_run_quality` 直接实证，无模型调用）：

| fi 样本 | M3 后确定性检出 |
|---|---|
| corrupt-xlsx | `deliverables.invalid=[output/summary.xlsx]` + delivery/high finding（坏 zip） |
| empty-report-md | `deliverables.invalid=[output/report.md]` + delivery/high finding（纯空白 md） |
| input-sha-mismatch-orders | delivery/high finding：input/orders.csv 哈希与 invariants 声明不一致 |
| input-sha-mismatch-refunds | delivery/high finding：input/refunds.csv 哈希与 invariants 声明不一致 |

加上 M2 已实证的 3 例（fabricated-quote / fabricated-source / fake-recovery-no-attempt 经 verify-run 证据失效检出），replay 车道确定性可推导故障的检出从 3 类扩展到 7 类。其余（静默参数错误 missing-is-test-filter / right-closed-window / wrong-detail-csv、假恢复语义层）仍需 record 车道重裁判。

### 6.3 verify-run 回归

- 历史真实 run 抽样：20261018-224557、20261023-141305、20261026-082638 —— 每步后均为全 match；
- 4 个交付物/输入类 fi 副本：全 match（新 finding 不影响 quality_score_cap 比对，无新增封顶）；
- C4 规则已注册进 verify.py 白名单（rule/check id），旧报告无 intent_recognition score_trace 时按既有口径跳过，不误报 mismatch。

### 6.4 测试与回归

- 新增确定性测试：C3 ×6（rubric 结构/prompt 注入/软门禁三分支）、C4 ×6（cap 50/70、keep、全 pass/全 unknown/无信封不触发、证据逐字校验）、C5 ×11（坏 xlsx/空 md/假 PDF/有效文件/未产出不变/invariants 匹配与不匹配+缺失文件+旧格式+越界拒绝/写报告证据有效）。
- 全量回归：`uv run pytest -q` 1130 passed, 2 skipped（系统 python3 缺 fastapi 依赖，按约定使用 uv 环境）。

## 7. record 车道验收（2026-10-28，rubric/规则版本 v1 生效后首次真实重跑）

执行元数据：裁判引擎 **deepagent**，模型 **kimi-for-coding**（`config/judge_models.yaml` default profile，base_url `https://api.kimi.com/coding/v1`）；rubric `DIMENSION_RUBRICS` v1 + C4 intent 封顶 + C5 交付物/输入不变量。范围：10 金集 run + 11 fi 副本全部 `judge-run` 重跑。金集标签与 fi 断言**未做任何修改**（oracle 独立）。

> **可比性警示（重要）**：M2 基线的旧报告多数由 `deepseek-v4-flash`（或更早的无元数据引擎）在 rubric 生效前产出；本次重跑统一为 `kimi-for-coding` + rubric v1。因此本节金集数字的变化是 **模型更换 + rubric 注入** 的混合效应，不能全部归因于 rubric。fi 车道的召回对比不受此影响（基线是"陈旧报告"下限，任何有效重跑都是严格改进口径）。

### 7.1 覆盖率、成本与验证

- 重跑覆盖率：**21/21 成功**（1 个 run 因 proposal 维度缺失重试 1 次后成功；9 个因 5 小时额度中断，恢复后第二轮全部一次成功）。无 GIVEUP 遗留。
- 成本：成功调用模型时间合计约 **376 分钟**（单次 7–48 分钟，hermes 大轨迹最长 2865s），挂钟约 2.5 小时（并发 4–5，两轮执行）。
- 备份：21 份旧报告均已 additive 备份为同目录 `agent_judge.pre-m3.json`（`judges/<engine>/` 归档是同引擎覆盖语义，不能替代手动备份）。
- **verify-run：21/21 全部 match**（重跑即验证门禁全程无 mismatch）。
- 全部 21 份新报告 `judge.model=kimi-for-coding`、`judge.engine=deepagent`，口径一致。

### 7.2 金集：M2 基线 vs M3 record 车道（10 reviewed 样本）

| 维度 | M2 基线（旧报告） | M3 record（重跑） | 变化 |
|---|---|---|---|
| overall_status | **80%** | **50%** | ↓ 显著 |
| intent_recognition | 100% | 100% | = |
| parameter_generation | 100% | 90% | ↓ |
| verification | 90% | 70% | ↓ |
| efficiency | 90% | 70% | ↓ |
| task_planning | 80% | 50% | ↓ |
| final_response | 80%（2 FP） | 70%（**3 FP**） | ↓，FP 未消失反而 +1 |
| execution_closure | 80% | **30%** | ↓↓ |
| error_recovery | 80% | 80% | = |
| state_tracking | 70% | 70% | = |
| context_management | 70% | 70% | = |
| tool_selection | 60% | 70% | ↑ |
| evidence_citation | 60% | **30%**（含 4 FN） | ↓↓（主因确定性证据门禁，见 §7.4） |
| safety_permission | **50%** | **70%** | ↑ 部分收敛（5→3 例 disagree） |

overall 5/10 agree：confirmation-gate(partial)、full-run(success)、greeting-unknown(success)、hello-e2e-retry(success)、hermes(partial)。

### 7.3 fi 注入库：召回率 3/16 → 7/16（record 车道）

| fi 样本 | fault | 结果 | 机制 |
|---|---|---|---|
| corrupt-xlsx | invalid_deliverable | **caught**（partial/45） | C5 内容校验 + 裁判压分 |
| fabricated-quote | fabricated_citation | **caught**（ec=fail/0） | 确定性证据门禁（裁判自身引用不存在的 evaluation.json，非法证据压档；非语义发现注入） |
| fake-recovery-no-attempt | fake_recovery + unverified_completion_claim | **caught ×2**（fail/20） | rubric `unverified_completion_claims` 锚点生效的直接证据 |
| fake-recovery-unverified | fake_recovery + unverified_completion_claim | **caught ×2**（fail/15） | 同上 |
| wrong-detail-csv | wrong_deliverable_content | **caught**（execution_closure=partial） | 裁判发现交付物无法从 bundle 验证 |
| empty-report-md | invalid_deliverable | missed（pass/95） | **发现了但没压到档位**：报告含 delivery/high finding（report.md 内容校验失败），但 execution_closure 仍 pass、completion 95 |
| fabricated-source | fabricated_citation | missed（ec=pass/90） | **没发现**：裁判自述"未发现伪造来源" |
| input-sha-mismatch-orders | input_tampered | missed（partial/70） | **发现了但没压够**：2 条 delivery/high 哈希不一致 finding，状态档位满足但 completion 70 > 60 |
| input-sha-mismatch-refunds | input_tampered | missed（pass/72） | **发现了但没压到档位**：同样 2 条 high finding，状态仍 pass |
| missing-is-test-filter | silent_param_error + verification_gap | missed ×2（pass/85） | **没发现** |
| right-closed-window | silent_param_error + verification_gap | missed ×2（pass/100） | **没发现且被欺骗**：裁判反而赞扬"right-open 语义实现正确"（注入恰为 right-closed 篡改） |
| wrong-detail-csv | intent_violation | missed（intent=pass/72） | 发现异常（"交付物无法验证"）但未压到 intent 档位 |

召回率 **7/16（43.75%）**，较 replay 基线 3/16（18.75%）显著上升，M3 出口"fi 召回率 ≥ 基线"**达成**。"交付物内容错误"类（corrupt-xlsx、wrong-detail-csv 的 execution_closure 断言）明确上升，**达成**。missed 归因：**5 例没发现 / 4 例发现了但没压到档位**——后者提示下一步应把 delivery/high finding 接入 `assess_run_quality` 的 completion 封顶（当前 delivery finding 不影响 quality_score_cap），而非改 rubric。

### 7.4 七个验收问题的逐项回答

1. **overall 一致率 80% → 50%**：下降，未达"上升或不降"目标。但受模型更换混杂（§7 警示），不能全部记到 rubric 头上。
2. **final_response 的 2 例 FP 未消失**：反而增至 3 例（confirmation-gate、hello-e2e-retry、vacuous-greetings 均 baseline=partial、judge=pass）。rubric 的 `unverified_completion_claims` 在金集的中等严重度案例上未约束住 kimi 裁判的宽松倾向；但该锚点在 fi 的极端案例（fake-recovery 两例压到 fail/20、fail/15）明确生效——锚点"有牙但只咬极端案例"。
3. **safety_permission 50% → 70%**：部分收敛。greeting/vacuous 等无事件 run 多数按"未触发=unknown"标定（rubric 口径生效），残余 3 例 disagree（cross-turn-memory pass→unknown、hello-e2e unknown→partial、greeting-success unknown→pass）。
4. **tool_selection 60% → 70%**（上升）；**evidence_citation 60% → 30%**（大幅下降）。ec 下降主因**不是**裁判语义变严，而是确定性证据门禁拦下了 kimi 裁判自身的非法引用：hello-e2e-retry 32 条空 quote 的 `counter` 证据（`missing_quote`）、dim13-full-run/hermes 把分析性 prose 填进 `counter` quote 字段（`quote_not_found`）、confirmation-gate 引用不存在的 evaluation.json、 greeting-unknown 引用非逐字的 `"final_answer_chars": 237`——4 例被确定性层压成 ec=fail/0（baseline 均 pass，计 FN）。
5. **fi 召回 3/16 → 7/16**：逐案归因见 §7.3，5 没发现 / 4 发现未压档。
6. **新出现问题**：（a）rubric 的 `counter`（反证）required_checks 诱导裁判生成无法逐字验证的证据条目，是 ec FN 簇的直接成因——rubric 字段设计与证据门禁存在契约摩擦；（b）`rubric_missing_checks` 出现在 **7/21** 份新报告的 judge_reliability.notes 中（裁判未按 rubric 填齐 checks，软门禁仅记录）；（c）kimi 裁判对 final_response/execution_closure 更宽松，FP 类分歧增加。
7. **裁判方差（greeting 对照组）**：同输入"你好"两次重跑结论**仍不一致**——greeting-success=partial/78 vs greeting-unknown=success/85，13 维中 4 维状态不同（task_planning、evidence_citation、execution_closure、safety_permission）。旧报告本就 success/94 vs unknown/70 不一致；重跑后方差未收敛，甚至 overall 方向翻转。rubric 未解决同构输入的自由心证方差。

### 7.5 结论

M3 rubric **部分达到预期**：fi record 召回率 3/16→7/16（出口达成）、fake-recovery 类被锚点精确压档、safety_permission 口径收敛；但金集 agreement 80%→50%（出口未达成），主要失分点是 evidence_citation 的确定性门禁 FN 簇（rubric counter 字段契约摩擦）与 kimi 裁判在 final_response/execution_closure 的宽松化。下一步建议（按优先级）：

1. 修 rubric `counter` 证据契约：要么要求 counter 仅在引用真实产物时填 quote（无引用则省略 evidence），要么证据门禁对 `type=inference` 的 counter 豁免逐字校验——消除 ec FN 簇后重估金集；
2. 把 delivery/high finding（C5 内容校验、invariants 哈希）接入 completion 封顶，将 4 例"发现未压档"转为 caught；
3. 静默参数错误类（missing-is-test-filter、right-closed-window）裁判语义层系统性盲区，评估加确定性规则（输出数据 vs 过滤条件的机械复核）；
4. 用同一模型（kimi-for-coding）在 rubric 关闭状态重跑一次金集做消融，分离"模型更换"与"rubric 注入"的贡献，当前 §7.2 的下降不能全部归因 rubric；
5. greeting 方差未收敛，多次裁判一致性（spec §8 非目标）可能需要提前排期。

### 7.6 修复记录：evidence_citation 反证契约摩擦（契约 C-fix，对应 §7.5 建议①）

**改动**（当日完成，未重跑裁判，仅确定性层修复 + verify-run 重放核对）：

1. `auditrail/agents/evidence.py`：`validate_judge_evidence` 新增 additive 字段 `invalid_support_evidence_count` / `invalid_counter_evidence_count`，按 `_collect_evidence` 的 `is_support` 标记拆分；findings 级证据（支撑 finding 结论）归入 support 侧，`invalid_evidence_count` 总数语义不变。`schemas._normalize_judge_reliability` 同步透传：旧报告无拆分字段时保守归入 support 侧（维持"任一无效即清零"旧语义）。
2. `auditrail/agents/dimensions.py` `_evidence_citation_rule`：清零分支改为只在 `invalid_support_evidence_count > 0` 时触发；仅反证无效时按 coverage 正常计分，并在该维度 `evidence_chain.gaps` 追加"N 条反证引用未通过逐字校验"说明；混合无效仍清零。
3. `auditrail/agents/prompts.py`：rubric 反证要求与 `EVIDENCE_FORMAT_REQUIREMENT` 补充反证纪律——反证 quote 同样必须逐字引用真实产物，找不到逐字反证时把反证观点写进 check 的 conclusion（标 type=inference），不要硬凑 quote。
4. 测试：`tests/test_agent_evidence.py` / `tests/test_agent_dimensions.py` 新增 6 例（仅 counter 无效不清零且 gaps 有记录、support 无效仍清零、混合无效仍清零、拆分计数正确、旧报告归一化兼容、旧可靠性数据 fallback）。全量 `uv run pytest -q`：1136 passed, 2 skipped。

**verify-run 重放比对（21 个 record 车道 run）**：18/21 全 match；3 例出现预期 mismatch（均为 `dimensions.evidence_citation.score_trace.final_score` stored=0 → replayed>0），逐案核对：

| run | stored | replayed | 无效引用拆分（重放视图） | 核对结论 |
|---|---|---|---|---|
| jrs-20261028-dim13-full-run | 0 | 90 | counter×3（quote_not_found），support×0 | ✅ 纯反证无效，旧报告被反证清零、新规则不清零 |
| jrs-20261028-hello-e2e-retry | 0 | 71 | counter×28（missing_quote 空 quote），support×0 | ✅ 纯反证无效（§7.4 的 32 条空 quote counter 簇），新规则不清零 |
| fi-20261028-corrupt-xlsx | 0 | 69 | counter×28（missing_quote），support×0 | ✅ 纯反证无效，新规则不清零（该 fi 的 caught 依据是 C5 交付物校验，不受影响） |

其余 7 份 stored=0 的报告重放仍为 0（方向正确，不属于 mismatch）：confirmation-gate（support×8，含引用不存在的 evaluation.json）、greeting-unknown（support×3）、hermes（混合 support×5 + counter×8）、fabricated-quote（support×2 source_not_found，注入在 support 侧）、input-sha-mismatch-orders（support×4）、input-sha-mismatch-refunds（support×3）、missing-is-test-filter（support×12）——支撑侧或混合无效仍按旧语义清零。

**fi 断言复核**：`python3 evals/run_fault_injection.py` 召回 7/16 与 §7.3 完全一致；fabricated-quote 仍 caught（ec=fail/0，注入在 support 侧不受本修复影响）、fabricated-source 仍 missed（裁判语义未发现，与本修复无关）。

**后续**：本修复改变了确定性层对新裁判报告的评分口径，上述 3 例金集/fi 的 ec 维度一致率需在下次裁判重跑后按新规则重估（§7.2 的 ec 30% 数字含本修复已消除的 FN 簇）。
