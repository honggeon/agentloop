# 运维手册（Phase 5）

## 日常操作

### 查看平台状态

```bash
curl -s http://127.0.0.1:18080/api/health/ready | jq .
curl -s http://127.0.0.1:18080/api/metrics | grep auditrail_import
```

Admin UI：`/admin/health`（数据库/队列/worker/stale 任务/索引/最近失败/审计可写性）、`/admin/workers`、`/admin/migrations`、`/admin/backups`。

### Schema migration

```bash
# 状态检查（只读）
python -m auditrail.cli migrate-db --status
# 应用待执行 migration（幂等，失败不标记成功）
python -m auditrail.cli migrate-db
# 或经 API（admin 角色，写审计）
curl -X POST http://127.0.0.1:18080/api/admin/migrations/apply
```

规则：migration 版本化、只增不毁、不在 API 启动时隐式执行。升级窗口内新旧代码共享同一 schema（本阶段只有 v1，后续 migration 必须保持向后兼容一个版本窗口）。

### 文件 → 数据库迁移

```bash
python -m auditrail.cli migrate-platform --dry-run     # 默认 dry-run
python -m auditrail.cli migrate-platform --yes         # 真实执行（幂等）
python -m auditrail.cli verify-platform-migration      # 计数一致性
python -m auditrail.cli rollback-platform-migration --dry-run   # 预演回滚
python -m auditrail.cli rollback-platform-migration --yes       # 只删本次 migration_id 标记的行
```

迁移不删除旧文件、不触碰 `runs/` 证据；单条坏记录计入 `invalid` 不阻塞整体。

### ImportJob 运营

- 列表/详情/控制 API：`/api/integrations/imports`、`/api/costs/imports/{id}`。
- 控制：`cancel`（协作取消，不回滚已导入证据）、`pause`（批次边界停止，租约释放）、`resume`（从 checkpoint 继续）、`retry`（仅 failed/partial/stale，受 max_attempts 限制）。
- stale 任务：租约过期自动标记，显式 resume 恢复；任何路径都不会把中断任务写成 completed。

### Worker 运营

```bash
python -m backend.app.worker_main     # 独立 worker 进程
```

- worker 注册表：`workers` 表 + `/api/admin/workers`（心跳、pid、状态）。
- worker 崩溃：在跑任务租约到期 → stale → 手动/自动 resume；fencing token（lease_version）阻止旧 worker 复活后覆写新状态。

### Redis 故障

`queue_mode=redis` 下 Redis 不可达：创建任务返回 `503 queue_unavailable`，任务保持 `queued` + `queue_degraded=true`；worker 的 DB 轮询兜底拾取。Redis 恢复后自动回到正常投递。不要把 `queue_degraded` 当作失败——它不是终态。

## 告警建议（基于 /api/metrics）

| 指标 | 告警条件 | 含义 |
| --- | --- | --- |
| `auditrail_import_job_stale_total` | > 0 持续 10m | worker 崩溃或失联 |
| `auditrail_import_job_failures_total` | 5m 内增量 > 阈值 | 导入源故障 |
| `auditrail_auth_failures_total` | 突增 | 认证滥用或 IdP 故障 |
| `auditrail_permission_denials_total` | 突增 | 越权尝试 |
| `auditrail_run_index_errors_total` | > 0 | 索引刷新失败 |
| `auditrail_import_job_duration_seconds` | p99 超阈值 | 导入变慢 |

指标不含 run_id/trace_id/user_id 等高基数标签，不含 token/prompt。

注意：计数器是**进程内**注册表——API 进程的 `/api/metrics` 只含 API 进程的计数（HTTP 请求、认证失败），worker 进程执行的导入计数在 worker 自己的 `/api/metrics`（若 worker 进程未暴露 HTTP 端口，则由 Prometheus 抓取配置决定）。跨进程状态（stale 任务数、活跃任务数、worker 心跳）以数据库为准，经由 scrape 时 gauge 反映。多进程部署请让 Prometheus 抓取所有实例。

## 日志

- 审计事件：`platform/audit/audit_events.jsonl`（append-only）+ database 模式下 `audit_events` 表。
- token、数据库密码、Redis 凭据不进入任何日志与错误响应。

## 关闭与 drain

见 `docs/deployment.md` §7。核心保证：进程中断的任务永不伪造 completed；证据文件只增不改。
