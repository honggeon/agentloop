# 裁判评分准确性设计：校准基线、故障注入与锚点扩展

> 状态：v1 草案（C1/C2/C6 已于 M1 落地；C3/C4/C5 已于 M3 落地，见 `docs/reports/2026-10-28-m2-review-baseline.md` 的验收对照）
> 日期：2026-10-28
> 调研依据：`docs/reports/2026-10-28-judge-accuracy-research.md`
> 事实源原则：本文涉及的现有行为一律以代码为准；文中行号仅供定位，行号漂移后以函数名为准。

## 1. 背景与目标

调研报告诊断出当前评分链路的六个瓶颈，其中最致命的是两个"没有数字"：

- **B0**：裁判从未与人标答案对齐过（`evals/judge-samples/` 仅 1 个样本且 `review_status != reviewed`，`compare_judge` 只能单样本对比、无聚合）；
- **B3**：评分系统对已知错误的召回率未知（无故障注入机制）。

以及两个"覆盖不足"：

- **B1**：13 个维度中 9 个是纯裁判自由心证，提示词里没有分数带与锚点（`auditrail/agents/prompts.py` 的 `REPORT_WRITER_PROMPT` 只列了维度 key 和中文名）；
- **B2**：交付物验证停在文件后缀存在（`auditrail/agents/tools.py` `_requested_deliverables` / `_produced_deliverables`），不验证内容有效性。

本设计的目标：把"分数有多准"变成可测量、可回归的工程指标，并把可计算信号接入更多维度。对应报告的 P0（测量基线）与 P1（锚点扩展）；P2（多次裁判一致性、效标关联、多引擎分歧路由）不在本设计范围，见 §8。

### 成功标准

1. `compare_judge` 能在整个金集目录上输出每维度 agreement / false_positive / false_negative 聚合矩阵；
2. 存在故障注入样本库，能输出"N 类已知错误的召回率"；
3. 每个维度在提示词中有分数带定义与固定原子检查项；
4. `evaluation.json` 的必需验收项失败能确定性封顶 `intent_recognition`；
5. 交付物检查能识别"文件存在但内容无效"（不可解析/违反已声明不变量）；
6. 任何 run 目录可无密钥重放确定性层并与已存报告比对（replay 车道）。

## 2. 事实源：当前代码契约摘要

### 2.1 评分写入口（唯一）

`auditrail/agents/tools.py` `write_judge_report`：

```
裁判提案 → 发布门禁（缺维度 JudgeProposalError）
  → Stage 1 validate_judge_evidence（base_reliability）
  → assess_run_quality（score_cap / quality findings）
  → assess_dimensions（4 条确定性规则）
  → Stage 2 validate_judge_evidence
  → _downgrade_unsupported_facts / _downgrade_unsupported_dimensions
  → 写 agent_judge.json / agent_judge.md / chart_data.json（+ judges/<engine>/ 归档）
```

### 2.2 确定性维度规则（`auditrail/agents/dimensions.py`）

| 维度 | 规则 id | 触发 | 候选分 | cap |
|---|---|---|---|---|
| tool_selection | tool_selection_cap | 有失败工具调用 | `round(100*(1-failed/total))` | 是 |
| execution_closure | execution_closure_set | 有轨迹 | 有最终回答：90（overall success）/65；无：20 | 否 |
| error_recovery | error_recovery_cap | 有失败调用 | 恢复已验证 60 / 尝试未验证 45 / 无尝试 30 | 是 |
| evidence_citation | evidence_citation_set | 总是 | 零证据或含无效引用 0；否则 `round(coverage*100)` | 否 |

规则执行动作 `set / cap / keep` 由 `_apply_rule` 决定并写入 `evidence_chain.score_trace.rules`；status 由分数映射（≥80 pass，≥50 partial，否则 fail）；`source` 更新为 `deterministic` 或 `deterministic+judge`。

### 2.3 证据校验（`auditrail/agents/evidence.py`）

逐字子串校验（JSON dump 精确 → 原始字符串 → 空白折叠三级阶梯），trajectory 行号漂移容错并记录 `index_corrections`。产出 `judge_reliability`：coverage、invalid、unsupported_dimension、fact_inference_mismatch 等。只证明"引用存在"，不证明"结论成立"。

### 2.4 验收信封（`auditrail/evaluation.py`）

`evaluation.json`（EvaluationEnvelope v1）：`deterministic_checks`（blocking）、`expectation_results`（required）、`artifact_checks`（required + `exists` + `valid`）。`compute_final_quality` 产出封顶：必需交付物缺失 40、blocking 检查失败 60、必需验收项失败 60，取最小。`unknown` 永不判负、永不产生 cap。`assess_run_quality` 已消费此信封（`tools.py` `_envelope_quality`）。

**关键观察**：`artifact_checks` 已有 `valid: bool` 字段，但当前没有生产者在"文件存在但内容无效"时填 `valid=false`——B2 的修复点就在这里，不需要改信封 schema。

### 2.5 金集契约（既有）

`docs/superpowers/specs/2026-10-21-judge-regression-sample-format.md` 定义 jrs v1 样本格式（labels.dimensions[].status + rationale + evidence 指引；`labeling.review_status` 区分 `pending_human_review / reviewed`）。`evals/compare_judge.py` 对比单样本与 `agent_judge.json`，分类 `agree / false_positive / false_negative / disagree / *_missing`，非 reviewed 样本输出 PROVISIONAL 标记。**现状缺口：只支持单样本，无聚合报告。**

### 2.6 裁判引擎

`auditrail/judges/base.py`：`deepagent` / `dsh` 双引擎注册表，`AUDITRAIL_JUDGE_ENGINE` 环境变量选引擎；`write_judge_report` 按引擎归档到 `judges/<engine>/`。多引擎分歧对比的基础设施已存在。

## 3. 设计总览：三条车道

借鉴 DSH 的 replay/record/refresh 三态，把裁判产物生命周期显式化：

| 车道 | 内容 | 凭据 | 运行位置 |
|---|---|---|---|
| replay（无密钥） | 金集对比、故障注入校验、确定性层重放比对 | 无 | CI 门禁，默认绿 |
| record（带密钥） | 真实裁判评分、金集标注辅助（ai_prelabel） | 模型 key | 本地/定时任务 |
| refresh（人工门禁） | 金集标签修订、rubric 修订后的基线重跑 | 人工 review | 评审后提交 |

制度纪律（沿用 DSH oracle 独立性）：**人标标签与注入标签永远不被自动化流程改写**；refresh 产生的每处基线 diff 须人工评审。

## 4. 契约草案

### C1 金集聚合校准（B0）

**改动**：`evals/compare_judge.py` 增加聚合模式，单样本行为不变（向后兼容）。

```
python3 evals/compare_judge.py --samples-dir evals/judge-samples [--json]
```

- 自动发现 `jrs-*.json`，按 `run_ref.run_dir` 定位对应 `agent_judge.json`；run 缺失或报告缺失的样本计 `skipped` 并在报告中列出，不中断整体；
- 聚合输出：
  - 总览：`reviewed` 样本数 / provisional 样本数 / skipped 数；**聚合指标只基于 reviewed 样本**，provisional 单独列出（与既有 PROVISIONAL 语义一致）；
  - 每维度矩阵：agreement、false_positive、false_negative、disagree、judge_missing、baseline_missing 计数与占比；
  - overall_status 一级的一致率；
- 退出码：0 正常完成；`--fail-below <0..1>` 时若任一 reviewed 维度 agreement 低于阈值则退出码 2（供 CI 门禁用，默认不启用）。

**不做**：不自动重跑裁判（金集对比消费已存报告；重跑属 record 车道，由 `judge-run` 负责）。

### C2 故障注入样本库（B3）

**新目录**：`evals/fault-injection/`，命名 `fi-<yyyymmdd>-<slug>.json`。与 jrs 样本的区别：jrs 标注"真实 run 的真实质量"，fi 样本声明"这个 run 含有已知注入错误，评分系统应当抓住"。

**格式契约 v1**：

```json
{
  "schema_version": 1,
  "sample_id": "fi-20261028-fake-recovery",
  "created_at": "<ISO8601>",
  "run_ref": {"run_dir": "runs/<run_id>", "source_type": "dsh"},
  "injection": {
    "method": "authored | edited_artifact | synthetic_run",
    "description": "注入了什么问题（如：轨迹截断制造假恢复、引用不存在的话术）",
    "faults": [
      {
        "id": "fake_recovery",
        "expect_dimension": "error_recovery",
        "expect_status_at_most": "partial",
        "expect_completion_score_at_most": 60,
        "rationale": "失败后无成功验证，error_recovery 不得高于 45/partial"
      }
    ]
  },
  "labeling": {"review_status": "reviewed", "labeled_by": "...", "reviewed_by": "..."}
}
```

- `expect_status_at_most` 取值 `pass | partial | fail`，语义为该维度 status 不得超过此档（pass > partial > fail）；`expect_completion_score_at_most` 为可选数值上限；两类断言至少一条；
- 注入方式三种：`authored`（手工构造的 run 目录）、`edited_artifact`（复制真实 run 后修改某个产物，描述中必须说明改了什么）、`synthetic_run`（代码生成的最小 run）；

**运行器**：`evals/run_fault_injection.py --dir evals/fault-injection [--json]`

- 对每个 reviewed fi 样本：读 `runs/<run_dir>/agent_judge.json`，逐条核对 fault 断言（维度 status 档位、completion_score 上限）；
- 输出召回率：`caught / total`，未抓住的 fault 逐条列出（维度、期望上限、实际值）；
- **裁判侧说明**：fi 校验同时适用于确定性层（无密钥，直接对现有报告断言）和裁判层（带密钥重跑 judge-run 后再断言）。replay 车道只跑前者。

### C3 全维度 rubric 锚点（B1）

**改动**：`auditrail/agents/prompts.py` 新增 `DIMENSION_RUBRICS: dict[str, dict]`，13 个维度各含：

```python
{
  "score_bands": [
    {"range": "80-100", "status": "pass", "anchor": "锚定到可观察产物的行为描述"},
    {"range": "50-79",  "status": "partial", "anchor": "..."},
    {"range": "0-49",   "status": "fail", "anchor": "..."},
  ],
  "required_checks": ["check_id_1", "check_id_2"],  # 裁判必须填的原子检查项
  "counter_evidence_required": True,  # 给 pass 前必须主动找反证
}
```

以 task_planning 为例的 required_checks（对齐 Agent GPA 的 Plan 段审计思路，评"对齐"不评"最优"）：

- `plan_precedes_execution`（fact）：规划是否先于执行出现，引用轨迹中计划消息；
- `requirement_coverage`（fact/inference）：用户目标拆成子需求，逐条标注规划中的对应步骤；
- `deviation_justified`：偏离规划处是否有显式理由或触发事件；
- `replan_on_failure`：失败后是否调整计划而非原样重试。

**提示词接线**：`REPORT_WRITER_PROMPT` 注入各维度 rubric 全文；保持既有契约不变（无证据 status=unknown、score=null，rubric 只约束"给分必须挂在检查项上"）。

**软门禁（不拒写，只记录）**：`_normalize_judge_reliability` 或 Stage 2 后新增统计 `rubric_missing_checks`（judge 来源、status != unknown 但缺 required_checks 的维度列表），记入 `judge_reliability.notes`。**不用 JudgeProposalError 拒写**，理由：向后兼容旧提案，且结构性拒写目前只用于"维度缺失"这一更严重的缺陷。

**确定性层不动**：rubric 是提示词与记录层契约，`schemas.py` / `dimensions.py` 无需为 rubric 新增依赖。

### C4 expectation 驱动的维度封顶（B1 的确定性侧）

**改动**：`auditrail/agents/dimensions.py` 新增 `_intent_recognition_rule`，数据源自 run 目录 `evaluation.json`（复用 `auditrail/evaluation.py` 的 `load_evaluation`，不重复解析）。

规则语义：

- 无有效信封（missing / 非 completed）→ 返回 None，维度保持 judge 来源（与"无工具失败时 error_recovery 不触发"同一哲学）；
- 任一 `expectation_results` 条目 `required=true 且 status=fail` → 候选分 50，`cap=True`（必需验收项失败说明意图未被正确理解或遵守，裁判分不得高于 partial 中线）；
- 仅 optional 失败 → 候选分 70，cap=True；
- 全部 pass / 仅 unknown → 返回 None（unknown 永不判负，与信封语义一致）；
- 证据引用 `evaluation.json` 中对应条目原文（沿用 `_envelope_quote` 的逐字安全模式），score_trace 的 reason 注明触发条目 id。

**为什么只 cap 不 set**：验收失败可能是执行差而非意图错（执行问题归 execution_closure 等维度）；cap 只限制裁判给出与硬信号矛盾的高分，差异由裁判在 summary 中解释。这与既有 4 条规则的 cap 语义一致。

**规则注册**：在 `assess_dimensions` 中追加一行 `_apply_rule(dimensions, "intent_recognition", _intent_recognition_rule(run_dir, artifacts), cap=True)`。

### C5 交付物内容不变量（B2）

**事实基础**：`make_artifact_check` 已有 `valid` 字段，`compute_final_quality` 已对 `required + fail` 的 artifact check 封顶 40。本契约只新增**生产者**与**触发 fail 的条件**，不改信封 schema。

**改动 1（tools.py）**：`_produced_deliverables` 之后增加 `_validate_produced_deliverables`：对已产出的交付物按后缀做内容有效性检查（全部 stdlib，无新依赖）：

| 后缀 | 检查 |
|---|---|
| .pptx / .docx / .xlsx | zipfile 可打开且含必需成员（如 `[Content_Types].xml`） |
| .pdf | 文件头 `%PDF-` |
| .md | 非空且非纯占位（去除空白后长度 > 0） |

无效交付物 → 生成 `category=delivery, severity=high` 的 fact finding（证据为 summary.json 任务标题 + 文件名），并使 `deliverables.verification` 保持 `verified` 语义不变但新增 `invalid` 列表。**注意**：该检查只对 run 目录内已产出的文件生效；"未产出"仍维持现有 unverifiable 语义（agent 工作区不可信、不可扫描，见 `assess_run_quality` 注释），不新增封顶。

**改动 2（evaluation 生产者侧约定）**：产生 `evaluation.json` 的一方（cases/runner 链路）在 artifact check 中，当 `exists=true` 但内容无效时填 `valid=false` 并将 `status` 置为 `fail`（required 时即触发既有 40 封顶）。auditrail 核心只消费信封，不规定生产者实现。

**expectations.json 扩展（可选，v1 只定义不强制）**：允许声明输入文件不可变不变量：

```json
{"invariants": [{"id": "input_immutable", "kind": "file_sha256", "path": "input.csv", "sha256": "..."}]}
```

消费侧（audit/quality）校验 run 目录内对应文件哈希，不一致生成 delivery finding。v1 范围只做这一种 kind；`content_regex` 等任务级约束留给后续（需要逐任务标注，成本更高）。

### C6 确定性重放校验（replay 车道入口）

**新 CLI 子命令**：`python3 -m auditrail.cli verify-run --run runs/<run_dir> [--json]`

纯确定性、无网络、无凭据，对一个已裁判的 run 重放并比对：

1. 读 `agent_judge.json`，`normalize_judge_report` 归一化；
2. 重算 `validate_judge_evidence` → 与报告中 `judge_reliability` 比对（coverage、invalid_evidence_count）；
3. 重算 `assess_dimensions` → 与报告中 4 个确定性规则的 `score_trace.final_score` 比对；
4. 重算 `assess_run_quality` → 比对 `metrics.quality_score_cap`；
5. 输出逐项 `match / mismatch` 清单；任一 mismatch 退出码 2。

**用途**：报告被篡改检测、确定性规则变更后的历史报告回归核对、生产抽查的机械部分。**边界**：judge 来源的分数天然不可重放（需要重跑裁判），verify-run 只校验确定性可推导部分，并在输出中显式标注哪些维度是 `judge` 来源、不在校验范围。

## 5. 不变式与兼容性

- **向后兼容**：C1/C2 是纯新增工具；C3 只改提示词与 notes；C4 新增规则只影响重跑裁判后的新报告，旧报告字段不变；C5 新增 finding/字段均为 additive；C6 只读。
- **确定性层无凭据**：C4/C5/C6 不引入任何模型调用；测试 monkeypatch 或手写小 JSON（遵循 tests/ 现有惯例）。
- **unknown 语义不动**：所有新规则遵循"无证据 unknown、unknown 不判负、0 分不冒充失败"。
- **runs/ 契约**：新增字段（`deliverables.invalid`、`judge_reliability` notes 新条目）向后兼容；`agent_judge.json` schema 无破坏性变更。
- **维度 key 不动**：13 个 key 仍以 `DIMENSION_DEFINITIONS` 为准，rubric 不新增/重命名维度。

## 6. 测试计划

| 契约 | 测试位置 | 要点 |
|---|---|---|
| C1 | `evals/` 自测或 `tests/test_compare_judge.py`（新增） | 聚合只算 reviewed；provisional 单列；缺 run 跳过；--fail-below 退出码 |
| C2 | `tests/test_fault_injection.py`（新增） | 断言档位语义（pass>partial>fail）；completion 上限；召回率计算 |
| C3 | `tests/test_agent_schemas.py` 扩展 | notes 中 rubric_missing_checks 记录；旧报告（无 checks）归一化不报错 |
| C4 | `tests/test_agent_dimensions.py` 扩展 | 必需 fail → cap 50；optional fail → cap 70；unknown 不触发；无信封不触发；证据逐字可通过校验 |
| C5 | `tests/test_agent_tools.py` 扩展 | zip 损坏的 xlsx 被判 invalid；空 md 判 invalid；未产出保持 unverifiable 不封顶 |
| C6 | `tests/test_cli_verify_run.py`（新增） | match/mismatch 判定；被篡改报告检出；judge 维度标注为不在校验范围 |

全部测试确定性、无网络、无凭据。涉及 `agent_judge.json` 写路径的改动须覆盖旧报告兼容（沿用现有测试夹具模式）。

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| rubric 写不好反而压缩裁判有效判断空间 | rubric 锚点一律绑定可观察产物，禁止抽象形容词；金集 agreement 做 P1 前后对比验收 |
| C4 封顶误伤"验收失败但意图正确"的 run | 只 cap 不 set（最低也是 partial 50，不归 fail）；score_trace 留完整 reason，裁判可在 summary 解释 |
| 金集标注质量参差 | 双签：`labeled_by` + `reviewed_by` 必须不同人；分歧样本进评审而非强判 |
| fi 样本与真实失败分布漂移 | 每发现一种线上漏判新模式固化为新 fi 样本（contract-regression 式沉淀） |

## 8. 非目标（本设计不做）

- 多次裁判一致性（pass^k / 中位数 / 极差告警）——依赖 C1 基线建立后评估必要性；
- 多引擎分歧路由人审队列——基础设施已在（judges 注册表），等样本量起来再启用；
- 维度效标关联体检（维度分 vs 成败相关性）——需要积累期数据；
- pairwise / Elo 评分模式——提示词与报告 schema 改动大，单独设计；
- 任务级 content_regex 不变量——需要逐任务标注机制，v1 只做文件哈希；
- 训练向的 rubric reward / 过程奖励模型——auditrail 是评测工具不是训练回路。

## 9. 里程碑

| 里程碑 | 内容 | 出口标准 |
|---|---|---|
| M1（P0 工具） | C1 + C2 + C6 | 金集聚合报告可跑；fi 召回率有数；verify-run 可对既有 runs 输出比对 |
| M2（P0 数据） | 金集扩至 20+ reviewed 样本；fi 库 10+ 样本 | 每维度 agreement/FP/FN 基线写入报告附录 |
| M3（P1 锚点） | C3 + C4 + C5 | rubric 化后金集 agreement 较 M2 基线提升；fi 召回率不下降 |
