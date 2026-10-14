---
name: auditrail-run-trace
description: Use when capturing, rendering, or judging xuanjian-agent runs in the auditrail repository — the fetch-session, smoke-chat, render-run, judge-run, metrics-proxy, import-llm-calls CLI workflows and the runs/<run_id>/ artifact contract.
---

# auditrail 采集与裁判 CLI

面向 `xuanjian-agent` 的黑盒轨迹采集、确定性审计与裁判命令速查。所有产物以 `runs/<run_id>/` 目录为共享契约。

## 采集会话

```bash
python3 -m auditrail.cli fetch-session --base-url http://HOST:8788 --user-id ID --session-id SID
python3 -m auditrail.cli smoke-chat --base-url http://HOST:8788 --user-id ID --message "..."
```

## 重渲染与裁判

```bash
python3 -m auditrail.cli render-run --run runs/<run-dir>
python3 -m auditrail.cli judge-run --run runs/<run-dir>
```

`judge-run` 需要模型凭据和 `.[agent]` 依赖；`render-run` 与审计是纯确定性的，不需要。

## LLM 调用指标

```bash
python3 -m auditrail.cli metrics-proxy --upstream-base-url http://HOST:PORT/v1
python3 -m auditrail.cli import-llm-calls --run runs/<run-dir> --log runs/proxy-llm-calls.jsonl
```

## runs/<run_id>/ 产物契约

| 文件 | 内容 |
|---|---|
| `session.json` | 原始会话响应 |
| `summary.json` | 会话摘要与来源元数据 |
| `trajectory.jsonl` | 归一化消息轨迹 |
| `audit.json` / `tool_audit.md` | 确定性工具审计 |
| `source.json` / `expectations.json` / `llm_calls.json` | 来源、验收标准、LLM 调用信息 |
| `agent_judge.json` / `agent_judge.md` / `chart_data.json` | DeepAgent 裁判与图表数据 |

规则：新增字段必须向后兼容已有报告；写裁判报告必须走 `write_judge_report`，由它统一归一化并写出三个文件。
