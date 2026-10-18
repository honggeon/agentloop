# 部署指南（Phase 5）

本文档覆盖三种部署形态。架构决策见 `docs/phase5-production-architecture.md`；运维手册见 `docs/operations.md`；备份恢复见 `docs/backup-restore.md`；安全模型见 `docs/security.md`。

## 部署模式总览

| 模式 | 配置 | 适用 | 可靠性声明 |
| --- | --- | --- | --- |
| 本地开发 | `STORAGE=file, QUEUE=inline, AUTH=local/disabled` | 开发者机器 | 单进程，不声明跨进程可靠性 |
| 单机生产 | `STORAGE=database, QUEUE=local_worker, AUTH=oidc/jwt` | 单机/预发布 | 跨进程（事务 claim + fencing），单主机 |
| 多实例生产 | `STORAGE=database(postgres), QUEUE=redis, AUTH=oidc` | 生产 | at-least-once 投递 + 幂等合并；不保证跨机 exactly-once |

对应环境变量：

```bash
AUDITRAIL_DEPLOYMENT_MODE=local|single_process|production
AUDITRAIL_STORAGE_MODE=file|database|dual
AUDITRAIL_QUEUE_MODE=inline|local_worker|redis
AUDITRAIL_AUTH_MODE=disabled|local|oidc|jwt
```

启动校验（失败即拒绝启动）：`production + disabled auth`、`production + file storage`、`production + sqlite`、`queue=local_worker|redis + storage=file`、`queue=redis` 缺 `AUDITRAIL_REDIS_URL`、`oidc` 缺 issuer。

## 1. 本地开发

```bash
./start.sh            # 后端 18080 + 前端 6173，file + inline + local
./start.sh status
```

体验 database 模式（无需外部服务，SQLite 内嵌）：

```bash
export AUDITRAIL_STORAGE_MODE=database AUDITRAIL_QUEUE_MODE=local_worker
.venv/bin/python -m auditrail.cli migrate-db
.venv/bin/python -m auditrail.cli migrate-platform --source file --target database   # 先看 dry-run，再加 --yes
./start.sh
```

## 2. 单机部署（single_process）

架构：一个主机跑 API 进程 + worker 进程 + PostgreSQL（或 SQLite），`runs/` 与 `platform/` 为挂载卷。

```bash
# .env（由部署管线注入，不提交）
AUDITRAIL_DEPLOYMENT_MODE=single_process
AUDITRAIL_STORAGE_MODE=database
AUDITRAIL_QUEUE_MODE=local_worker
AUDITRAIL_DATABASE_URL=postgresql://auditrail:***@127.0.0.1:5432/auditrail?sslmode=require
AUDITRAIL_AUTH_MODE=oidc
AUDITRAIL_OIDC_ISSUER_URL=https://idp.example.com
AUDITRAIL_OIDC_CLIENT_ID=auditrail
AUDITRAIL_OIDC_AUDIENCE=auditrail
AUDITRAIL_OIDC_CLIENT_SECRET=...        # 仅环境变量
AUDITRAIL_RUNS_DIR=/srv/auditrail/runs
AUDITRAIL_PLATFORM_DIR=/srv/auditrail/platform
```

初始化与启动顺序（migration 只执行一次，不在 API 启动时隐式跑）：

```bash
python -m auditrail.cli migrate-db --database-url "$AUDITRAIL_DATABASE_URL"
python -m auditrail.cli migrate-platform --source file --target database --dry-run
python -m auditrail.cli migrate-platform --source file --target database --yes
python -m auditrail.cli verify-platform-migration --database-url "$AUDITRAIL_DATABASE_URL"
uvicorn backend.app.main:app --host 0.0.0.0 --port 18080          # API
python -m backend.app.worker_main                                  # worker（独立进程）
```

## 3. 多实例生产（production）

- PostgreSQL 为唯一事实来源；Redis 只做投递提示（宕机 = 入队降级为 DB 轮询，不丢任务）。
- API 多副本 + worker 多副本；migration 由部署流水线一次性执行。
- Redis 不可用时 API 创建任务返回 `503 queue_unavailable`（retryable），任务保持 `queued` 并标记 `queue_degraded`。

```bash
AUDITRAIL_DEPLOYMENT_MODE=production
AUDITRAIL_STORAGE_MODE=database
AUDITRAIL_QUEUE_MODE=redis
AUDITRAIL_DATABASE_URL=postgresql://...?sslmode=require
AUDITRAIL_REDIS_URL=rediss://...        # TLS 由 URL scheme 控制
AUDITRAIL_AUTH_MODE=oidc
```

## 4. Docker / compose（本地与预发布）

```bash
docker compose up -d postgres redis
docker compose run --rm migrate        # 一次性 schema migration
docker compose up -d api worker        # API + worker
```

- compose **不是高可用方案**；生产请用编排器 + 托管数据库。
- 镜像不含 `.env`、token 或真实 `runs/`；`runs/`、`platform/` 经卷挂载；数据库密码走环境变量注入。
- worker 与 API 使用同一镜像、不同入口；migration 是独立一次性容器。

## 5. 目录与挂载

| 路径 | 内容 | 备份 |
| --- | --- | --- |
| `runs/` | Run 证据（权威文件契约） | 部署方单独备份（不在平台备份内） |
| `platform/` | 文件型元数据、审计 JSONL、SQLite 默认位置 | `auditrail backup-platform` |
| PostgreSQL | database 模式的元数据权威 | 平台备份导出 JSONL 表 + 建议 pg_dump |

## 6. 健康检查与指标

- `GET /api/health`：存活性（恒 `ok`）。
- `GET /api/health/live`：进程存活，不因依赖故障翻转。
- `GET /api/health/ready`：就绪（200 ready/degraded，503 not_ready）；含 database/queue/storage/runs_root/audit_log/workers 检查项。
- `GET /api/metrics`：Prometheus 文本（无高基数标签、无 secret）。
- Admin UI：`/admin/health`、`/admin/workers`、`/admin/migrations`、`/admin/backups`。

## 7. 关闭与 drain

- API：`SIGTERM` → uvicorn 优雅退出；进程内任务标记中断，租约到期后由 recovery 标记 `stale`（绝不伪造 completed）。
- worker：`SIGTERM` → 停止领取新任务；在跑任务保持租约，进程退出后由其他 worker 以 `stale → resume` 接管。
- 升级顺序：先 `migrate-db`（向后兼容的增量 migration），再滚动重启 worker，最后滚动重启 API。
