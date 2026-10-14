# DSH 集成运维与故障排查手册

本手册面向在本地或受控环境运行 auditrail 的 DSH（DeepSeek Harness）集成功能的
运维者。目标是让「怎么安全地打开」「怎么确认它在正常工作」「出问题先看哪里」
三个问题都能在一页内回答。

运行契约、API 形状以代码和测试为准；本手册只描述操作方式和故障定位顺序。相关
设计背景见 `docs/reports/2026-10-14-dsh-acp-protocol-spike.md`，用户命令见仓库根
`README.md`。

## 1. 信任边界速览

一句话原则：**调用方只能从服务端白名单里选值，永远不能提供 argv、环境变量或
可执行文件路径。**

| 项 | 由谁决定 | 说明 |
|---|---|---|
| 可执行文件 | 服务端 `DSH_BINARY` | 必须是绝对路径；调用方无法覆盖 |
| profile | 服务端 `DSH_ALLOWED_PROFILES` | 调用方只能在白名单内选择 |
| workspace | 服务端 `DSH_ALLOWED_WORKSPACE_ROOTS` | 必须严格位于某个允许根之下 |
| 超时 | 服务端上下限 | 调用方可请求，但被 `DSH_MAX_TIMEOUT_SECONDS` 夹住 |
| 并发 | 服务端 `DSH_MAX_CONCURRENCY` | 单 worker，不做多进程共享 |
| 权限回答 | 人工 allow-once / reject-once | 没有 allow-always，没有自动批准 |
| 裁判 profile/workspace | 服务端 `DSH_JUDGE_*` | 必须与所有 evaluand profile 不同 |

进程以 `shell=False` 启动，并使用独立进程组，因此超时与取消能一并终止它派生的
工具子进程。本集成只在 POSIX 上可用。

## 2. 开关与最小可用配置

三个功能各自有独立开关，且都叠在同一个 master 开关之上：

| 功能 | master | 专属开关 | 其它必填 |
|---|---|---|---|
| Headless 单轮执行 | `DSH_RUNTIME_ENABLED=true` | 无 | `DSH_BINARY`、`DSH_ALLOWED_PROFILES`、`DSH_ALLOWED_WORKSPACE_ROOTS` |
| ACP 交互运行时 | `DSH_RUNTIME_ENABLED=true` | `DSH_ACP_ENABLED=true`（默认开） | 同上（profile 固定为 `acp`） |
| DSH 裁判 | `DSH_RUNTIME_ENABLED=true` | 无 | 上面全部 + `DSH_JUDGE_PROFILE` + `DSH_JUDGE_WORKSPACE` |

要点：

- **默认全关。** `DSH_RUNTIME_ENABLED` 未设置时，任何 DSH 路由都返回「未启用」，
  不会启动任何进程。
- ACP 的 `DSH_ACP_ENABLED` 默认是 `true`，但它无法单独打开 ACP —— 必须先有
  master 开关和白名单，所以「默认开」不等于「默认可用」。
- 白名单为空会让配置校验失败并映射成 503，而不是回退到「允许一切」。
- `DSH_JUDGE_PROFILE` 会被并入可信策略，因此它同时被用作「evaluand 不得使用
  裁判 profile」的判据。

`.env.example` 是权威模板，其中 DSH 段落全部被注释掉，即默认不启用。

## 3. 启动、健康检查与关闭

```bash
./start.sh            # 启动后端 + 前端
./start.sh status     # 查看端口与后端健康状态
./start.sh stop
./start.sh restart
```

后端健康检查是 `GET /api/health`，返回 `{"status": "ok"}`。它**不**探测 DSH：
健康检查保持确定性，不依赖外部进程。要确认 DSH 侧的可用性，用只读发现接口：

```bash
curl -s localhost:18080/api/dsh/options      | python3 -m json.tool   # Headless/裁判清单
curl -s localhost:18080/api/dsh/acp/options  | python3 -m json.tool   # 已协商的 ACP 能力
```

`/api/dsh/options` 不会返回 `DSH_BINARY`，只返回允许的 profile 与 workspace 根。

关闭顺序由 FastAPI 的 shutdown 钩子负责，顺序是固定的：

1. `shutdown_active_operations()` —— 取消所有在飞的 Headless 操作；
2. `shutdown_acp_runtime()` —— 关闭长驻进程，并清空管理器单例。

两者都是幂等的，重复调用安全。长驻 ACP 进程的**正常关闭方式是关闭 stdin**，
这是 DSH 支持的退出请求；随后才会对进程组发信号。

## 4. 测试环境与 `.env` 隔离

这是最容易踩的坑：**本地 `.env` 会被自动加载**，因此它可能悄悄改变一次测试运行
的行为，甚至让测试去访问网络。

已经落地的隔离机制：

- `AUDITRAIL_LOAD_DOTENV=0` 会让 `backend/app/config.py` 与
  `auditrail/llm_proxy.py` 都不再读取项目 `.env`。`tests/conftest.py` 在任何测试
  模块被收集之前强制设置该变量，并且是**强制覆盖**，shell 里的残留值无法重新打开。
- `tests/conftest.py` 还提供 `no_network_egress` autouse fixture：任何测试如果
  通过 `scp`/`sftp`/`ssh`/`rsync`/`curl`/`wget` 发起网络传输，会直接失败。
  这是违规检测器，不是沙箱。
- 真实运行产物、真实 `~/.dsh` 都不参与测试：夹具一律使用 `tmp_path`。

为什么需要它：本地 `.env` 里的 `AUDITRAIL_LLM_PROXY_LOG` 指向一个**远程**主机
（`user@host:/path`）。导入一个没有 LLM 调用的 session 时，`_write_run_outputs`
会回落到这条配置并尝试 `scp`，于是测试会真实触网。修好之后，全量测试从
约 456 秒降到约 6 秒，因为那次注定失败的网络调用（30 秒超时）不再发生。

**运维含义：** 生产运行时不要设置 `AUDITRAIL_LOAD_DOTENV=0`，否则 `config.py`
不再读取 `.env`，你必须改用真实环境变量提供全部配置。

## 5. 可选的集成冒烟测试

确定性测试永远不会启动真实 DSH。要验证「生产适配器能驱动真实进程」，用 opt-in
的集成冒烟测试：

```bash
AUDITRAIL_DSH_INTEGRATION=1 \
DSH_BINARY="$(command -v dsh)" \
python3 -m pytest tests/test_dsh_integration.py -q
```

| 环境变量 | 作用 |
|---|---|
| `AUDITRAIL_DSH_INTEGRATION` | 设为 `1`/`true` 才启用；否则整个模块 skip |
| `DSH_BINARY` | 真实启动器绝对路径（否则跳过） |
| `AUDITRAIL_DSH_INTEGRATION_PROFILE` | Headless 使用的 profile，默认 `headless` |
| `AUDITRAIL_DSH_INTEGRATION_ACP_PROFILE` | ACP 使用的 profile，默认 `acp` |
| `AUDITRAIL_DSH_INTEGRATION_WORKSPACE` | agent 的工作目录，默认临时目录 |
| `AUDITRAIL_DSH_INTEGRATION_HOME` | DSH home，默认自动派生一个隔离 home |
| `AUDITRAIL_DSH_INTEGRATION_TIMEOUT` | 单轮预算秒数，默认 300 |

它覆盖 6 条路径：Headless NDJSON 契约、durable 录制导入、超时被显式上报、ACP
握手契约、ACP session+prompt 往返、ACP 取消。

**关于 DSH home 的隔离：** DSH 每次启动都会重写
`profiles/<name>/cordis.yml`。直接驱动运维者的真实 `~/.dsh` 既会改动他的状态，
在只读沙箱下还会直接失败（`EPERM`）。所以默认行为是从真实 home **复制**少量
配置文件（`cordis*.yml` / `package.json` / `pnpm-workspace.yaml`、凭据）到 pytest
临时目录，并把体积很大的 `profiles/node_modules` 以软链接方式共享。运行结束后
真实 home 的 mtime 与 sessions 目录不变。要刻意驱动真实 home，显式设置
`AUDITRAIL_DSH_INTEGRATION_HOME=~/.dsh`。

注意这些测试会消耗模型额度。每次真实运行会在隔离 home 的 `sessions/` 下留下一条
录制。

## 6. 故障排查

### 6.1 先看哪一层

```text
路由返回 400/404/409/503   → 输入或注册表问题（流开始之前）
SSE 里出现 event: error    → 流中途问题（看 code 与 stage）
进程根本没起来            → DSH 侧问题（看 stderr 诊断）
```

`event: error` 的 `stage` 字段指明阶段：`validate`、`register`、`execute`、
`import`。**`completed` 只表示回合结束，不表示成功**；只有 `run_imported` 表示
标准 run 已发布。

### 6.2 常见现象

| 现象 | 常见原因 | 处理 |
|---|---|---|
| 所有 DSH 路由 503「ACP runtime is disabled」 | master 开关未开，或 `DSH_ACP_ENABLED=0` | 检查 `DSH_RUNTIME_ENABLED`；`/api/dsh/options` 会给出 `disabled_reason` |
| 503「DSH_BINARY is required…」 | 开了 master 但没给二进制 | 设置 `DSH_BINARY` 为绝对路径 |
| 403/503「workspace is outside the allowed roots」 | workspace 不在允许根下 | 检查 `DSH_ALLOWED_WORKSPACE_ROOTS`；注意解析后会比较真实路径 |
| `error{stage:"register", code:"runtime_busy"}` | 达到 `DSH_MAX_CONCURRENCY` | 等当前回合结束，或显式提高上限（单 worker 共享进程仍不被支持） |
| `error{stage:"register", code:"session_busy"}` | 同一 session 已有在飞回合 | 先取消或等待 |
| `error{stage:"import", code:"import_failed"}` | 回合结束但没有可导入的录制 | 回合**不算**成功；确认 `DSH_HOME` 与 workspace 匹配 |
| 立刻 `write_timeout` | 对端停止读取 stdin | 传输已被整体作废；等重启，检查 DSH 是否卡死 |
| `frame_too_large` | 单帧超过 4 MiB | 传输被关闭（fail-closed，有意为之）；这是整个长驻进程级的失败 |
| `event: error{code:"client_disconnected"}` | 浏览器断开 | 操作被取消；用 `GET /api/dsh/acp/operation/{id}` 查最终状态 |
| `completed` 里 `dropped_events > 0` | 消费端读得太慢，进度事件被丢弃 | 终态事件永不丢弃；降低事件量或检查前端卡顿 |

### 6.3 背压与容量边界

这些边界是为了让「慢消费者」不会拖垮后端：

- 每条 ACP 操作的 SSE 队列上限 `MAX_OPERATION_QUEUE = 4096`，其中
  `_TERMINAL_QUEUE_RESERVE = 64` 个位置**只留给**终态事件。高流量进度事件在
  队列接近上限时被丢弃并计数，`completed.dropped_events` 会如实上报。
- 单轮 `session/update` 订阅缓冲上限 `ACP_MAX_SUBSCRIBED_UPDATES = 4096`，
  超出部分计入 `dropped`。
- 未被认领的入站帧缓冲上限 `ACP_MAX_UNCLAIMED_INBOX = 4096`，超出计入
  `inbox_dropped`。
- 已作答的入站请求 id 记录上限 `ACP_MAX_RESPONDED_IDS = 4096`。
- 保留的权限请求上限 `ACP_MAX_PENDING_PERMISSIONS = 256`；回合结束时会丢弃该
  回合遗留的权限请求，避免「事后补答一个已结束的回合」。
- 已结算的操作最多保留 `MAX_RETAINED_OPERATIONS = 32` 条，供状态轮询。
- 帧上限 4 MiB，stderr 诊断上限 16 KiB（读取时脱敏）。

### 6.4 权限问题

只有 `allow-once` 与 `reject-once` 两种回答会被送出。即使对端在自己的 options
里塞进 `allow_always`，传输层也会拒绝，因为回答还受本端白名单约束。一个没有
`sessionId` 的权限请求会被直接拒绝（它没有归属者，永远等不到回答），并回一个
JSON-RPC 错误，而不是让对端无限等待。

被取消的回合会把待答权限按 `cancelled` 回掉，因此 DSH 不会卡在等待中。

### 6.5 判断一次运行是否可信

```bash
python3 -m auditrail.cli render-run --run runs/<run-dir>
```

确定性审计（`audit.json` / `tool_audit.md`）始终权威。裁判只是语义补充
（`agent_judge.json`，多引擎归档在 `judges/<engine>/`，索引是
`judges/manifest.json`）。`judges/compare` **不会**选出赢家：没有证据的维度记为
`unknown`，确定性失败不会被语义判断拔高。

## 7. 发布前自检清单

```bash
python3 -m pytest -q                       # 全量确定性测试
cd frontend && npm run build               # 类型检查 + 打包
python3 -m compileall -q auditrail backend # 语法编译
git diff --check                           # 空白/冲突标记

# 可选：真实运行时冒烟
AUDITRAIL_DSH_INTEGRATION=1 DSH_BINARY="$(command -v dsh)" \
  python3 -m pytest tests/test_dsh_integration.py -q
```

默认全量测试**不会**启动 DSH、不会联网、不需要模型凭据、不需要 `.[agent]` 依赖。
