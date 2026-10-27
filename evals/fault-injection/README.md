# 故障注入样本库（格式契约 v1）

故障注入（fi）样本声明"这个 run 含有已知注入错误，评分系统应当抓住"，用来度量
裁判/确定性层对已知错误类型的召回率。与 jrs 金集样本的区别：jrs 标注"真实 run
的真实质量"，fi 样本标注"人工注入的缺陷必须被压低评分"。

样本是数据不是代码：新增样本只加文件，不改 schema。样本只引用 run 产物，永不
修改 run 目录内容。

## 文件位置与命名

- 目录：`evals/fault-injection/`
- 命名：`fi-<yyyymmdd>-<slug>.json`，`sample_id` 与文件名一致。

## 字段契约（v1）

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
        "rationale": "失败后无成功验证，error_recovery 不得高于 partial"
      }
    ]
  },
  "labeling": {"review_status": "reviewed", "labeled_by": "...", "reviewed_by": "..."}
}
```

## 断言语义

- 每条 fault 至少声明一条断言（`expect_status_at_most` /
  `expect_completion_score_at_most`），两者都没有的样本视为不可用（计 skipped）。
- `expect_status_at_most` 取值 `pass | partial | fail`，档位语义为
  pass > partial > fail：该维度的实际 status 不得超过此档。`unknown` 或缺失的
  status 不携带正向结论，满足任何档位上限。
- `expect_completion_score_at_most` 为数值上限：报告的 `completion_score` 不得
  超过该值；`completion_score` 为 null 时该断言记 `unverifiable`（不算 caught
  也不算 missed）。
- fault 判定：任一断言被违反 → `missed`；至少一条断言成立且无违反 → `caught`；
  所有断言都不可评估 → `unverifiable`。

## 注入方式

- `authored`：手工构造的 run 目录；
- `edited_artifact`：复制真实 run 后修改某个产物，`description` 必须说明改了什么；
- `synthetic_run`：代码生成的最小 run。

## 运行器

```bash
python3 evals/run_fault_injection.py --dir evals/fault-injection [--json]
```

- 只处理 `labeling.review_status == "reviewed"` 的样本（与金集同一纪律）；
  非 reviewed 样本单列提示。
- 缺 run 目录 / 缺 `agent_judge.json` 计 `skipped` 并列出，不中断。
- 输出召回率 `caught / total`，未抓住的 fault 逐条列出（sample、fault id、维度、
  期望上限、实际值）；空目录输出 `0/0`，退出码 0。
- 纯只读、无网络、无模型凭据（replay 车道）；带密钥重跑裁判后再断言属于
  record 车道，命令相同。
