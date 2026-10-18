# 备份与恢复（Phase 5）

## 范围

| 内容 | 是否包含 | 说明 |
| --- | --- | --- |
| `platform/` 文件型元数据（agents/suites/jobs/外部证据/成本/价格/审计 JSONL） | ✅ | 逐文件 sha256 入 manifest |
| 数据库表（database 模式） | ✅ 可选 | 逐表 JSONL 导出（SQLite/PostgreSQL 通用）；生产建议另配 `pg_dump` |
| run 索引元数据 | ✅ 可选 | 仅索引缓存副本 |
| `runs/<run_id>/` 证据目录 | ❌ 默认不含 | 权威文件契约，由部署方单独备份（对象存储/文件级备份） |
| `.env`、token、密钥文件（`*.pem/*.key/...`） | ❌ 永不 | 排除并记录在 manifest `excluded` |

每个备份含 `manifest.json`（schema_version、backup_id、UTC 时间戳、组件清单、逐文件 sha256）。

## 命令

```bash
# 备份（文件 + 数据库）
python -m auditrail.cli backup-platform --output /backups --include-database

# 校验（manifest + 校验和 + secret 扫描）
python -m auditrail.cli verify-platform-backup --backup /backups/backup-xxx

# 恢复：默认 dry-run；真实恢复不覆盖已存在文件/行（幂等补齐）
python -m auditrail.cli restore-platform --backup /backups/backup-xxx --dry-run
python -m auditrail.cli restore-platform --backup /backups/backup-xxx --restore-database --yes
# 目标目录非空时需显式 --force 才允许叠加恢复
```

也可经 API：`POST /api/admin/backups`（admin，写审计）、`GET /api/admin/backups`。

## 恢复后一致性检查

```bash
python -m auditrail.cli verify-platform-migration   # 文件/数据库计数对比
curl -s http://127.0.0.1:18080/api/health/ready     # 全链路就绪检查
```

## RPO / RTO（诚实声明）

- 本实现是**单机备份**：备份产物在本地/挂载卷上。RPO = 备份间隔（建议 cron 每日或每小时）；RTO = 恢复命令执行时间（分钟级，取决于数据量）。
- **不构成跨区域灾备或高可用**：没有复制流、没有异地副本、没有自动 failover。需要更低 RPO 时请使用 PostgreSQL 的 PITR/流复制与对象存储版本化，并把 `runs/` 纳入同一备份策略。
- SQLite 备份通过"导出 JSONL + 文件复制"实现；在线一致性由导出时刻决定。PostgreSQL 生产环境建议以 `pg_dump`/`pg_basebackup` 为主，本工具导出为可移植副本。

## 回滚与灾难场景

- schema 迁移出错：migration 失败不标记成功，重跑安全。
- 数据迁移出错：`rollback-platform-migration` 只删除带本次 `migration_id` 的行；文件与 `runs/` 始终未被修改。
- 全量丢失：先装依赖 → `migrate-db` → `restore-platform --restore-database --yes` → `verify-platform-migration` → 启动 API/worker。
