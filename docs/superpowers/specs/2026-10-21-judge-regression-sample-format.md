# 裁判回归样本格式 v1（评测契约）

> 状态：v1 草案，首个样本见 `evals/judge-samples/jrs-20261019-top2-orders.json`（待人工复核）。
> 用途：为「标注样本验证 judge 增益」（评审收敛优先级 ③）提供基准数据契约。

## 定位

裁判回归样本是**人工确认的基准标签**（ground truth），用来度量任何裁判引擎
（现有 DeepAgent 引擎、DSH 引擎、未来的 MLflow judge）在真实 run 上的误报、
漏报与分歧。它是数据不是代码：新增样本只加文件，不改 schema。

样本**不是**行为套件（`cases/*.json` 由 `run-cases` 执行，断言运行结果）；
样本引用一个已存在的 `runs/<run_id>/`，标注的是"这次运行的真实质量"。

## 文件位置与命名

- 目录：`evals/judge-samples/`
- 命名：`jrs-<yyyymmdd>-<短slug>.json`，`sample_id` 与文件名一致。

## 字段契约

```json
{
  "schema_version": 1,
  "sample_id": "jrs-20261019-top2-orders",
  "created_at": "<ISO8601>",
  "run_ref": {
    "run_id": "20261019-120937-dsh-...",
    "run_dir": "runs/20261019-120937-dsh-...",
    "source_type": "dsh"
  },
  "task": {
    "input_summary": "一句话任务描述",
    "acceptance_criteria": "标注时确认的验收标准（可补写；与 expectations.json 的关系需在 note 说明）",
    "criteria_note": "acceptance_criteria 的来源说明"
  },
  "labels": {
    "overall_status": "success | partial | failed",
    "dimensions": [
      {
        "key": "intent_recognition",
        "status": "pass | partial | fail | unknown",
        "rationale": "为什么这么标",
        "evidence": [
          {"artifact": "trajectory.jsonl", "ref": "m40", "note": "最终回答声明……"}
        ]
      }
    ]
  },
  "labeling": {
    "method": "ai_prelabel | human",
    "review_status": "pending_human_review | reviewed",
    "labeled_by": "<标注者>",
    "reviewed_by": null,
    "reviewed_at": null,
    "judge_context": "标注时既有裁判报告的状态（供分歧分析）"
  }
}
```

## 硬性规则

1. **维度 key 必须以 `auditrail/agents/schemas.py` 的 `DIMENSION_DEFINITIONS` 为准**（13 个）；
   状态枚举以 `ALLOWED_DIMENSION_STATUS` 为准。
2. **未触发的维度标 `unknown`，不标 fail**（与裁判契约同一纪律）。例如全程无工具
   失败的 run，`error_recovery` 标 unknown——"没有发生"不等于"恢复能力差"。
3. **每条标签必须带证据引用**：`artifact` 只能是 run 目录内文件（`trajectory.jsonl` /
   `audit.json` / `summary.json` / `session.json` / `expectations.json`），`ref` 用
   消息序号（m12）或 audit 字段路径。无法回指的结论降级为 rationale 中的推断说明。
4. `overall_status` 用裁判报告的三态（success/partial/failed），与维度状态分开标注。
5. **复核流程**：`method=ai_prelabel` + `review_status=pending_human_review` 的样本
   不得作为基准使用；人工复核后改为 `reviewed` 并填 `reviewed_by/reviewed_at`。
6. 样本只引用 run 产物，**永不修改** run 目录内容。

## 度量口径（供优先级 ③ 使用）

对每个被测裁判引擎，在同一批 reviewed 样本上计算：

- 维度级一致率（标签 == 裁判维度状态）；
- 误报（裁判 pass、基准 partial/fail）与漏报（裁判 fail、基准 pass/partial）；
- 分歧案例清单（逐条人工归因：裁判错 / 基准错 / 标准模糊）。

两个裁判一致不等于正确；收益判定只看相对人工基准的误报/漏报变化。
