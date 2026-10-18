# Phase 5 生产架构决策记录（ADR）

状态：已决策（2026-10-18，Phase 5 实施基线 `3ae0d90`）
范围：PostgreSQL 持久化、可靠任务基础设施、统一成本导入、OIDC/JWT 认证、多用户 RBAC、健康/指标、备份恢复、三种部署模式。

本文件是 Phase 5 的架构决策记录。任何与本文冲突的实现以实际代码 + 最终交付报告为准，偏差必须显式说明。

## 1. 是否引入 PostgreSQL

**引入，但作为可选生产后端，不是唯一后端。**

- 平台元数据存储抽象为 `StorageBackend`（`auditrail/db/`），提供两个实现：
  - **SQLite（stdlib `sqlite3`）**：`database` 模式的默认内嵌实现。SQLite 的 `BEGIN IMMEDIATE` 事务与文件锁在**单机上提供真实的跨进程一致性**（多进程 API/worker 可安全并发 claim），不需要外部服务，也是全部自动化测试使用的真实后端（不是 fake）。
  - **PostgreSQL（可选依赖 `psycopg>=3.1`）**：生产推荐。通过 `AUDITRAIL_DATABASE_URL=postgresql://...` 启用，支持 `sslmode` TLS 配置。连接串密码不进入日志、健康接口或错误响应。
- 引入理由：当前平台元数据（ImportJob、外部证据、成本、审计、索引）全部是单进程 JSON 文件 + 进程内锁，无法支撑"worker 独立进程运行"和"API 多实例"，且全量重写型 JSONL 存储（成本/外部证据）随数据量增长不可持续。
- 数据库**只保存平台元数据和索引**。`runs/<run_id>/` 仍是 Run 证据与报告的权威文件契约；trajectory、完整 prompt、chain-of-thought 永不入库；外部 trace 原始 payload 保持现有文件存储与大小限制，数据库中只存安全引用与索引字段。

## 2. 是否引入 Redis / 队列 broker

**引入 Redis 作为可选 broker；默认不依赖 Redis。**

`AUDITRAIL_QUEUE_MODE` 三档：

| 模式 | 语义 | 可靠性声明 |
| --- | --- | --- |
| `inline`（默认） | 现有进程内线程池 + 文件存储 | 单进程。**不声明跨进程可靠性** |
| `local_worker` | 任务状态在数据库；worker 以独立进程轮询并事务性 claim | **跨进程、单机**可靠（数据库事务 + fencing token）。不需要 Redis |
| `redis` | 数据库仍是最终事实来源；Redis list 只作唤醒/投递提示（`LPUSH`/`BLPOP`），worker 拿到 id 后仍须回数据库事务 claim | 多进程/多实例投递；**不提供跨机 exactly-once** |

- Redis 不可用时：API 创建任务返回结构化错误（`queue_unavailable`，retryable），任务保持 `queued` 且显式标记 `queue_degraded`，**绝不假装已入队**，已有任务状态不得被覆盖为 `completed`。
- 不伪造可靠性：进程内队列继续明确标注 `inline`，`redis` 模式下的 at-least-once 语义依赖数据库 claim + idempotent merge，而不是 Redis 本身。

## 3. 为什么需要这些组件

- **数据库**：跨进程任务状态机（claim/heartbeat/fencing）必须有事务与行级条件更新；JSON 文件无法做到。
- **Worker broker（可选）**：多实例下缩短任务 pickup 延迟；但正确性不依赖它，因此它是可选优化而非正确性前提。
- **OIDC/JWT（PyJWT + cryptography，可选依赖）**：`local`/`disabled` 不是认证；生产需要真实的签发方校验、签名验证、过期与受众校验。

## 4. 本地开发如何运行

```bash
# 现状不变：file + inline + local/disabled
./start.sh

# 想体验 database 模式（无需任何外部服务，SQLite 内嵌）：
AUDITRAIL_STORAGE_MODE=database AUDITRAIL_QUEUE_MODE=local_worker ./start.sh
auditrail migrate-db            # 初始化/升级 schema（幂等）
auditrail migrate-platform --source file --target database   # 导入既有文件数据
auditrail import-worker         # 可选：独立 worker 进程
```

## 5. 生产部署如何运行

- **单机生产（single_process → 推荐起点）**：`database + local_worker + oidc`，PostgreSQL（或 SQLite 亦可，文档说明边界），API 与 worker 两个 systemd/compose 服务，`runs/` 与 `platform/` 挂载卷。migration 由独立命令（compose 的一次性 migrate job / 部署流水线步骤）执行，**不在每个 API 进程启动时隐式跑**。
- **多实例（production）**：`database + redis + oidc`，API 多副本 + worker 多副本 + PostgreSQL + Redis。数据库是唯一事实来源；SQLite 不支持该模式（启动检查拒绝）。
- compose 只用于本地/预发布，文档明确它不是高可用方案。

## 6. 文件型存储与数据库如何共存

`AUDITRAIL_STORAGE_MODE=file|database|dual`：

- `file`（默认）：现状，完全不需要数据库。
- `database`：数据库成为 **ImportJob 状态、用户注册表、OIDC state、worker 注册表** 的权威来源（这些是跨进程正确性所必需的）。文件保留为只读兼容层，旧 ImportJob 记录经 `migrate-platform` 导入后可查。
- `dual`：在 database 语义之上，每次 ImportJob 写入后把记录镜像回文件（best-effort，供运维直接查阅）；读以数据库为准，冲突时数据库优先且由 consistency check 暴露，绝不静默覆盖。
- **诚实边界（本阶段）**：Agent/TestSuite/SuiteJob/成本记录/外部证据/价格版本的*运行时*读写仍以文件为准；它们被迁移进数据库用于统一查询、备份与后续切换，但本期不把在线读写路径切到数据库。下一阶段的切换点已在 schema 与迁移工具中就位。审计事件维持 append-only JSONL 文件为权威，数据库副本用于查询与备份。

迁移工具（`auditrail migrate-platform`）默认 dry-run，幂等，单条坏记录不阻塞，旧文件永不删除。

## 7. 数据迁移顺序

1. `auditrail migrate-db`：schema migration（版本化，`schema_migrations` 表，可重复检查，失败不标记成功）。
2. `auditrail migrate-platform --source file --target database --dry-run`：预演报告（total/imported/skipped/duplicate/invalid/warnings/errors）。
3. 正式执行；每条记录保存 `source_ref`（文件路径 + 记录 id）与 `migration_id`，可回溯。
4. `auditrail verify-platform-migration`：逐类计数对比（agents / suites / suite_jobs / import_jobs / 外部 trace+evaluation / cost / price version / audit events）。
5. 切换 `AUDITRAIL_STORAGE_MODE=database|dual`，重启。
6. rollback：`auditrail rollback-platform-migration` 只删除带本次 `migration_id` 标记的数据库记录，**不 DROP TABLE，不碰任何文件与 Run 证据**。

## 8. 回滚策略

- 应用层：切回 `AUDITRAIL_STORAGE_MODE=file` 即回到 Phase 4 行为（文件从未被改）。
- 数据层：`rollback-platform-migration`（dry-run 默认）按 `migration_id` 精确删除。
- schema 层：migration 只增不毁；不提供自动 down migration，回滚=停止使用新表。

## 9. 故障边界

- **单机故障**（进程崩溃/机器重启）：数据库中的租约过期 → 任务标记 `stale`（绝不自动 completed），显式 resume 后从 checkpoint 继续；fencing token（`lease_version`）阻止死掉的 worker 复活后覆写新 worker 的状态。
- **多实例故障**（`redis` 模式）：任一 API/worker 实例宕机不影响他例；Redis 宕机 = 新任务入队显式失败（degraded），已入队任务仍可由 worker 轮询兜底拾取。
- **不保证跨机 exactly-once**：投递是 at-least-once；重复执行的防护是 事务 claim + idempotent merge（`source_system+source_id` 唯一）+ checkpoint，而不是队列语义。网络分区下两个实例各自 claim 的防护是数据库条件更新与 fencing token。

## 10. Secret 与配置管理

- 所有 secret 只来自环境变量/挂载的 `.env`（不提交）；`AUDITRAIL_DATABASE_URL`、OIDC client secret、JWT 密钥永不写入平台文件、审计日志、metrics、健康接口或错误响应。
- 配置集中在 `backend/app/config.py`；生产模式启动校验（`production + disabled` → 启动失败；`production + sqlite` 多实例 → 拒绝）。
- 备份产物不包含 `.env`、token、连接串；manifest 记录的是配置键名而非值。

## 11. 数据库不可用时 API 的行为

- `/api/health/live` 仍然 200（进程活着；不因 DB 短暂不可用触发重启）。
- `/api/health/ready` 返回非 ready + 结构化 check 明细（无连接串/绝对路径）。
- 读写平台元数据的 API：返回 503 `storage_unavailable`（retryable=true），不返回部分伪造数据。
- `file`/`dual` 模式下的只读端点可降级到文件读取，响应显式标注 `degraded: true`。
- ImportJob worker claim 失败按 retryable 处理，指数退避，不崩溃。

## 12. 认证与授权决策

- 模式：`disabled|local|oidc|jwt`。`disabled` 仅 `local` 部署可用；`production + disabled` **启动失败**。
- OIDC：discovery（`/.well-known/openid-configuration`）、JWKS 缓存（TTL 可配）+ unknown-kid 触发刷新（key rotation）、issuer/audience/exp/nbf 校验、可配 clock skew；provider 不可用时允许在 `jwks_max_stale_seconds` 内用缓存 key 继续验证，超过即拒绝（不无限期用过期 key）；新登录显式报错。
- 浏览器登录：授权码流程（state+nonce 服务端暂存），token 经 fragment 回传前端存 sessionStorage；**token 不进日志**，`/api/auth/context` 只返回安全摘要。
- RBAC：`viewer < operator < admin < owner`，权限点集中映射（见 `backend/app/auth.py`）；后端依赖统一检查，前端隐藏按钮只是 UX。
- 租户：本阶段**显式 single-tenant**。内部 `TenantContext`（固定 `default`，只来自服务端配置）预留；拒绝客户端提交的 `tenant_id`；数据库唯一约束预留 tenant 维度。不声称多租户。

## 13. 仍不保证的能力（显式声明）

- 跨机 exactly-once 投递（见 §9）。
- 多租户隔离（single-tenant，见 §12）。
- 高可用/跨区域灾备：单机文件复制 + 数据库备份不是 HA；RPO/RTO 目标见 `docs/backup-restore.md`。
- SuiteJob（批量评测执行器）本阶段仍为单进程执行器；跨进程化的只有 ImportJob 体系。
- 大对象（trajectory/prompt/原始 payload）不入库，也不进入平台备份的默认范围（由部署方另行备份 `runs/`）。
