# 安全模型（Phase 5）

## 认证

| 模式 | 语义 | 用途 |
| --- | --- | --- |
| `disabled` | 全部放行 | 仅本地开发；`production` 部署启动即拒绝 |
| `local` | 服务端环境变量配置的单一身份（`AUDITRAIL_AUTH_ROLE`/`AUDITRAIL_AUTH_ACTOR`） | 本地/单操作员；**不是生产认证** |
| `oidc` | OIDC discovery + JWKS 验签 | 生产（浏览器 SSO 登录） |
| `jwt` | Bearer JWT 验签（JWKS 或 HS256 共享密钥） | 生产（服务间/API 调用） |

校验项：issuer、audience、exp、nbf（可配 clock skew）、签名（RS256 经 JWKS / HS256 经服务端密钥）、`sub` 必须存在。JWKS 缓存有 TTL；未知 `kid` 触发一次强制刷新（key rotation）；provider 不可用时缓存 key 最多沿用 `AUDITRAIL_OIDC_JWKS_MAX_STALE_SECONDS`（默认 24h），超出即拒绝——过期信任不无限延续；新登录显式报错。

- 401 = 未认证（缺失/过期/无效 token）；403 = 已认证但角色不足。
- token 只经 `Authorization: Bearer` 传入；浏览器回调把 token 放在 URL fragment（不进服务端日志/历史）；前端存 sessionStorage。
- token 不进入日志、审计、metrics 或任何错误响应；`/api/auth/context` 只返回安全摘要。

## 授权（RBAC）

角色 `viewer < operator < admin < owner`，权限点集中在 `backend/app/auth.py`：

- viewer：只读（runs/agents/suites/imports/prices/index/audit）。
- operator：+ 运行/取消任务、导入写入/取消/重试。
- admin：+ Agent/套件写入、价格版本、索引管理、草稿审批。
- owner：+ 用户与角色管理（`users:admin`）；owner 角色的授予/修改只能由 owner 执行。

规则：

- 权限检查全部在后端（依赖/service 层）；前端隐藏按钮只是 UX。
- 不信任客户端提交的 role、actor、tenant_id（`X-Role` 等头被忽略）。
- 无效/未知 token 角色被忽略，缺省落到最低权限 viewer。
- OIDC 用户首次登录 JIT 落库；用户记录中的角色为服务端管理，优先于 token 声明（吊销不等 IdP）。
- 越权读取返回 403/404，不泄露资源内容（错误体不含 endpoint/记录内容）。

## 租户

当前**显式单租户**：`tenant_id` 只来自服务端配置（`AUDITRAIL_TENANT_ID`，默认 `default`）；所有数据库约束以 `(tenant_id, ...)` 为主键形态，预留多租户演进；客户端提交的 `tenant_id` 被丢弃。不声称多租户隔离。

## 数据与存储

- `runs/<run_id>/` 是证据权威；数据库只存平台元数据与索引；trajectory、完整 prompt、chain-of-thought 永不入库。
- 导入路径安全：`resolve_import_path` 限制在允许根内，拒绝穿越/符号链接逃逸/staging 文件。
- promptfoo 导入继续禁止执行 provider/command/plugin/script（声明可执行内容的草稿不可 approve）。
- 外部导入 SSRF 防护（`auditrail.netguard`）继续生效；Langfuse token 只从环境变量读取。
- 审计日志 append-only、字段脱敏（含 secret 片段的字段写入 `[redacted]`）、写失败显式 500。
- 健康/metrics 接口不含连接串、绝对路径、secret；metrics 禁止 run_id/trace_id/user_id 等高基数标签（注册时强制拒绝）。

## 依赖与传输

- PostgreSQL TLS：连接串 `sslmode=require|verify-full`。
- Redis TLS：`rediss://`。
- OIDC issuer/JWKS 仅允许 https（loopback 开发 provider 允许 http）。
- 可选依赖 `psycopg`/`redis`/`PyJWT`/`cryptography` 均为懒加载，缺失时报明确错误而非降级为不安全行为。

## 备份安全

备份不包含 `.env`、token、密钥文件（排除并记录）；manifest 只含文件名/大小/sha256。恢复默认 dry-run，不覆盖现有数据（除非显式 `--force`）。
