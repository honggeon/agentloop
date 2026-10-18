# DSH 0.2.0-rc.2 测试文档（基于 agentloop/auditrail）

> 测试对象：DeepSeek Harness（DSH）`@deepseek-ai/dsh` **0.2.0-rc.2**
> 测试工具：本仓库 `agentloop`（auditrail 评测框架）
> 文档日期：2026-10-18　执行环境：macOS arm64 / Node v22.23.0 / Python 3.11+
> 方法：等价类划分、边界值、场景法、错误推测、确定性审计 + LLM 裁判（三层测试模型）
> 前置阅读：`dsh-test-cases.md`（0.1.2-rc.1 的 46 条用例与历史缺陷）、`docs/dsh-agent-testing-guide.md`

---

## 第一部分　测试工具（本项目）完全质量评估

用项目测 DSH 之前，先回答一个问题：**这个工具本身可信吗？** 以下为实测评估，数据均来自 2026-10-18 真实执行。

### 1.1 项目定位与架构

auditrail 是面向 Agent 的黑盒轨迹采集、确定性审计与评测看板工具，核心原则：**确定性检查优先于 LLM 语义判断**——确定性代码验证证据、交付物、工具调用与分数封顶；LLM 裁判只做语义分析，不能推翻确定性失败结论。

```text
auditrail/        核心 Python 包（85 个 .py，约 3.3 万行）
  cli.py            17 个子命令：采集/审计/裁判/用例 runner/迁移/备份
  cases.py          确定性用例 runner（argv 断言 + 会话行为断言，706 行）
  dsh_source.py     DSH 会话录制（session.jsonl.zstd）→ 标准 run 契约转换
  dsh_headless.py   DSH Headless 运行时封装（服务端白名单控制）
  dsh_acp.py        ACP v1 over stdio 传输（NDJSON + JSON-RPC 2.0，2264 行）
  agents/           DeepAgent 裁判（1 主 + 4 子智能体，13 个评测维度）
backend/          FastAPI 看板后端（/api 前缀，SSE live 评测）
frontend/         Vue 3 + TS 看板（约 1.7 万行 src，ECharts 图表）
cases/            机器可读验收用例套件（dsh-cli.json 24 条 / dsh-agent.json 5 条）
tests/            62 个测试文件，966 条 pytest
docs/             设计、运维、发布就绪审计、周报
```

分层纪律良好：核心包不依赖 backend；后端只做 HTTP 装配并复用核心函数；前端 API 集中在 `api.ts`。`runs/<run_id>/` 文件契约跨 CLI/后端/前端/裁判共享且向后兼容。

### 1.2 实测质量数据

| 评估项 | 方法 | 结果 | 结论 |
|---|---|---|---|
| 后端+核心测试 | `.venv/bin/python -m pytest -q` | **966 passed, 2 skipped, 18.4s** | ✅ 全绿 |
| 前端类型检查+构建 | `cd frontend && npm run build`（vue-tsc --noEmit + vite build） | ✅ 通过，2.19s | ✅ |
| 用例 runner 可用性 | `run-cases --suite cases/dsh-cli.json --no-write` | 正常执行并产出逐条判定 | ✅ |
| DSH 探针 | `dsh --patch/--dump-config` 实测 | 成功识别 0.2.0 行为变化 | ✅ 工具能有效发现问题 |

### 1.3 工程实践亮点（作为测试工具的可信度来源）

1. **预期来自实测，不来自文档**：`cases/*.json` 的期望值标注为真实运行捕获，且明确记录了宿主、Node 版本。
2. **环境隔离**：触及 profile 状态的用例自动使用隔离 `DSH_HOME={tmp}/home`，绝不污染真实 `~/.dsh`；测试套件强制 `AUDITRAIL_LOAD_DOTENV=0`，本地 `.env` 不能左右测试结果、不能触网。
3. **xfail 缺陷管理**：已知缺陷以 `xfail+defect` 形式断言"应该怎样"，修复后必须摘 flag 而不是删用例；`xpass` 视为套件过期并判 FAIL——这天然支持跨版本缺陷回归。
4. **判定分级严谨**：`passed/failed/xfail/xpass/error` 五种判定；超时或启动失败是 `error` 且不被 xfail 吸收——"环境坏了"永远可见，不会伪装成"已知缺陷"。
5. **未知 expect 键硬报错**：拼错的断言键直接报错，防止"永远通过的假断言"。
6. **安全默认**：本地 DSH Headless/ACP 运行时默认关闭，需双开关 + 绝对路径 binary + profile/workspace 白名单；调用方不能传裸 argv、不能传 `--patch`。
7. **裁判可靠性层**：LLM 裁判证据必须回指 `summary.json`/`audit.json`/`trajectory.jsonl`，无证据结论自动降级为 `unknown`/`inference`。

### 1.4 风险与不足（使用时必须知晓）

| # | 风险 | 影响 | 缓解 |
|---|---|---|---|
| R1 | 用例套件期望值锁定在 0.1.2-rc.1 | 直接对 0.2.0-rc.2 跑会大量 FAIL（本次预跑 12/24 failed） | 必须先做基线重建（见 4.2），把 FAIL 分类后再下结论 |
| R2 | 无 lint/ruff/mypy 工具链 | 代码风格与静态类型靠 review 保障 | 评估时不引入新工具链；改动遵守 AGENTS.md |
| R3 | 前端无单元测试，仅 `test_frontend_app_state.py` 源码字符串断言 | UI 逻辑回归靠 build + 手工 | 关键页面改动必须跑 `npm run build` + 人工核对 |
| R4 | 前端主 bundle 822KB（>500KB 警告） | 首屏加载偏重 | 非阻塞，记录为技术债 |
| R5 | 行为套件（dsh-agent.json）写入真实 `~/.dsh/sessions` 并消耗模型额度 | 污染环境/产生费用 | 在低峰、空闲主机执行；费用纳入计划 |
| R6 | 工作区有未跟踪文件（hello.txt、my-testing/ 等）与 `uv.lock` 未提交修改 | 评估基线略脏 | 本评估以 HEAD + 工作区现状为准，已声明 |

### 1.5 工具质量结论

**结论：可信，可用于对 DSH 0.2.0-rc.2 的正式测试。** 966 条测试全绿、前后端构建通过、判定语义严谨、环境隔离完善。唯一的前置动作是**用例基线重建（R1）**——这不是工具缺陷，而是套件"预期来自实测"原则的必然维护成本。

综合评分（5 分制）：架构分层 4.5 / 测试完备度 4.5 / 判定严谨性 5.0 / 文档完备度 4.5 / 环境隔离 5.0 / 前端工程 3.5。**总分 4.5/5，达到"测试工具"准入标准（≥4.0）。**

---

## 第二部分　质量目标（本次 DSH 测试的验收标准）

### 2.1 总体质量目标

| 目标编号 | 质量目标 | 度量 | 达标线 |
|---|---|---|---|
| QG-1 | 功能正确性：CLI 启动器与配置组合 | dsh-cli.json 套件（基线重建后） | passed + xfail = 24/24；**0 failed / 0 error** |
| QG-2 | 行为合规性：沙箱、会话落盘 | dsh-agent.json 套件 | 5/5 passed（危险行为断言全过） |
| QG-3 | 安全性：工作区外写入与路径穿越 | DSH-SBX-002/005 + GUI 审批流 | 100% 拦截，无静默成功、无部分写入 |
| QG-4 | 错误质量：异常不泄漏堆栈 | 全部错误路径用例 | 已知缺陷以 xfail 显式记录；**新增堆栈泄漏 = P1 缺陷** |
| QG-5 | 版本回归：0.1.2-rc.1 已修复问题不复发 | xfail → xpass 检测 | 任何 xpass 必须当日分析并更新套件 |
| QG-6 | 轨迹质量：真实会话确定性审计 | import-dsh-session + audit.json | missing/orphan tool result = 0；工具错误率 ≤ 5%（历史基线 3.6%） |
| QG-7 | 裁判可靠性：LLM 裁判不幻觉 | judge_reliability 字段 | 幻觉证据拦截率 100% 被记录；无证据结论不得标记 fail |
| QG-8 | 工具自身稳定：测试过程框架零故障 | 执行日志 | runner 自身 error = 0（error 全部可归因于被测对象或环境） |

### 2.2 缺陷定级

| 级别 | 定义 | 示例 |
|---|---|---|
| P0 阻塞 | 数据丢失、沙箱逃逸、静默吞错、进程挂死 | 工作区外写入成功 |
| P1 高 | 核心功能错误、退出码错误、堆栈/敏感信息泄漏 | DSH-BUG-001（错误抛未捕获异常，Node 堆栈泄漏到 stderr） |
| P2 中 | 误导性报错、边界行为不符 | DSH-BUG-003（--port 透传 pnpm 报 "Did you mean 'sort'?"） |
| P3 低 | 文案、体验问题 | DSH-BUG-002（未知 flag 报与真实参数无关的错误） |

### 2.3 退出标准（测试完成的定义）

1. QG-1 ~ QG-5 全部达标，QG-6 ~ QG-8 有数据结论；
2. 所有 FAIL 完成三分类：**环境噪声 / 套件过期（版本漂移）/ 真实缺陷**，真实缺陷均有缺陷报告；
3. 0.1.2-rc.1 的 3 个已知缺陷（DSH-BUG-001/002/003）在 0.2.0-rc.2 上有明确结论：已修复（→xpass，摘 flag）或仍存在（保持 xfail）；
4. 产出：更新后的套件、逐条执行报告（`cases/results/*.md`）、缺陷清单、本报告的实测数据回填。

---

## 第三部分　环境准备

### 3.1 环境清单与验证

| 项 | 要求 | 验证命令 | 通过标准 |
|---|---|---|---|
| OS | macOS arm64（基线环境） | `uname -sm` | `Darwin arm64` |
| Node.js | v22.x | `node -v` | `v22.x`（本次实测 v22.23.0） |
| DSH | 0.2.0-rc.2 | `dsh -V` | 输出 `0.2.0-rc.2` |
| Python | ≥3.11 | `python3 --version` | 3.11+ |
| 项目虚拟环境 | `.venv` 已建 | `.venv/bin/python -m pytest --version` | pytest 8.x |
| 前端依赖 | `frontend/node_modules` | `cd frontend && npm run build` | 构建成功 |
| 模型凭据 | `.env` 配 `MODEL_PROVIDER`/`MODEL_NAME`/`DEEPSEEK_API_KEY` | 见下 | 行为套件与裁判需要；功能层测试不需要 |

**意义**：DSH 行为高度依赖宿主与 Node 版本（历史上 macOS 沙箱语义、Node 实验性警告都直接影响断言），所以环境本身必须作为测试记录的一部分固定下来。

### 3.2 安装与初始化

```bash
cd /Users/chen/agentloop
python3 -m pip install -e .          # 或沿用现有 .venv
npm i -g @deepseek-ai/dsh            # 确认 dsh -V == 0.2.0-rc.2
```

**通过标准**：`python3 -m auditrail.cli --help` 列出 17 个子命令；`dsh --help` 正常输出。

### 3.3 环境噪声处理（实测发现，重要）

当前环境每次 dsh 调用都可能向 stderr 打印两类**与被测对象无关**的噪声，会使 `stderr_empty` 断言误败（基线预跑中 177 = 仅第一类，320 = 两类叠加）：

| 噪声 | 字节 | 来源 | 触发条件 |
|---|---|---|---|
| `[UNDICI-EHPA] Warning: EnvHttpProxyAgent is experimental...` | 177 | Node v22 运行时警告 | 每次调用 |
| `dsh: all_proxy names a SOCKS proxy, which is not supported...` | 143 | dsh 启动器对 shell 中 `all_proxy=socks5://...` 的告警 | 宿主配置了 SOCKS 代理 |

**两者均非 DSH 缺陷。** 统一处理方式（已实测 stderr 归零、exit 0）：执行套件时加环境前缀

```bash
NODE_OPTIONS=--no-warnings all_proxy= uv run python -m auditrail.cli run-cases ...
```

`all_proxy=` 仅对本次执行清空 SOCKS 变量（`http_proxy`/`https_proxy` 保留，联网用例不受影响），不修改 shell 持久配置。也可在套件 `env` 中逐用例声明，但推荐执行环境前缀——噪声属于环境而非被测对象。

**意义**：测试 Oracle 必须区分"被测对象产生的输出"与"运行时环境噪声"，否则会把环境问题记成产品缺陷。

### 3.4 隔离原则（安全红线）

- 功能层套件自带 `DSH_HOME={tmp}/home` 隔离，直接跑即可；
- 行为套件（dsh-agent.json）与 `import-dsh-session` 操作**真实** `~/.dsh/sessions`：在空闲主机执行，执行期间不要并发使用 DSH GUI 开新会话（会干扰录制 diff）；
- pytest 全量套件自动强制 `AUDITRAIL_LOAD_DOTENV=0`，无需手工处理；
- 可选的真实 DSH 集成冒烟（`tests/test_dsh_integration.py`）默认 skip，需显式 `AUDITRAIL_DSH_INTEGRATION=1` 且自带临时 DSH home。

---

## 第四部分　测试步骤（含每步意义与通过标准）

### 阶段 0　测试工具自验证（Gate：工具不可信则全部结果无效）

| 步骤 | 命令 | 意义 | 通过标准 |
|---|---|---|---|
| 0.1 全量单测 | `.venv/bin/python -m pytest -q` | 验证审计/裁判/runner/后端逻辑未被破坏 | **966 passed, 2 skipped**（skipped 为 opt-in 集成测试，属预期） |
| 0.2 前端构建 | `cd frontend && npm run build` | 验证看板类型与构建可用（后续要看报告） | vue-tsc 无错误，vite build 成功 |
| 0.3 看板冒烟 | `./start.sh` 后访问 `http://localhost:6173` | 验证后端 `/api/health` 与前端页面 | `/api/health` 返回 `{"status":"ok"}`，页面正常渲染 |
| 0.4 runner 自检 | `run-cases --suite cases/dsh-cli.json --only DSH-CLI-002 --no-write` | 最小成本验证 runner 与 dsh 二进制通路 | 该用例 passed |

> 本次实测：0.1 ✅ 966 passed；0.2 ✅ 构建 2.19s 通过；0.4 ✅（实测中 DSH-CLI-002 通过）。

### 阶段 1　用例基线重建（0.1.2 → 0.2.0 必做）

**意义**：套件期望值捕获自 0.1.2-rc.1。直接对 0.2.0-rc.2 执行的 FAIL 必须先分类，否则结论无效。本次预跑已给出分类依据：

| 步骤 | 内容 | 实测发现 | 通过标准 |
|---|---|---|---|
| 1.1 全量预跑 | `NODE_OPTIONS=--no-warnings all_proxy= .venv/bin/python -m auditrail.cli run-cases --suite cases/dsh-cli.json` | 未加 no-warnings 预跑：**5 passed / 12 failed / 7 xfail / 0 xpass** | 完成逐条分类 |
| 1.2 环境噪声剔除 | 对比加/不加 `NODE_OPTIONS=--no-warnings` | 约 177 字符 UNDICI 警告污染所有 `stderr_empty` 断言（如 DSH-BOOT-001/004） | 加 no-warnings 后此类 FAIL 消失 |
| 1.3 版本漂移确认 | 实测 `--patch` 新格式 | **0.2.0-rc.2 的 `--patch` 文件必须是"顶层 YAML 数组的 loader patch 条目"**，旧 map 格式报 `must be a top-level YAML array of loader patch entries`，exit=1 → DSH-BOOT-007/008 失败属**套件 fixture 过期**，需按新格式重写 overlay 并更新期望 | 更新后 BOOT-007/008 恢复 passed |
| 1.4 已知缺陷核对 | 检查 7 条 xfail 是否仍 xfail | **7 条 xfail 全部仍失败、0 xpass** → DSH-BUG-001/002/003 在 0.2.0-rc.2 **均未修复**（本次探针复现：patch 格式错误仍抛未捕获异常+堆栈泄漏） | 每条有"仍存在/已修复"结论 |
| 1.5 套件元数据更新 | `cases/*.json` 的 `target`/`notes` 改为 0.2.0-rc.2 并记录重捕获环境 | 当前仍写 0.1.2-rc.1 | 元数据与被测版本一致 |

**阶段通过标准**：QG-1 达成——重建后套件 0 failed / 0 error（xfail 不计）。

### 阶段 2　功能层：CLI 启动器与配置组合（dsh-cli.json，24 用例）

```bash
NODE_OPTIONS=--no-warnings all_proxy= .venv/bin/python -m auditrail.cli run-cases --suite cases/dsh-cli.json
# 单模块/单用例调试：
... --only CLI --no-write        # 只跑 CLI 模块
... --only DSH-BOOT-007          # 单条
```

| 模块 | 覆盖 | 意义 | 通过标准 |
|---|---|---|---|
| CLI（9 条） | 帮助、版本、别名等价、非法命令、参数归属 | 验证启动器作为"软件"的基本正确性；参数归属错误会导致用户配置被静默吞掉 | 全部 passed（已知缺陷 xfail 不计失败） |
| BOOT（12 条） | profile 自动初始化、补丁层组合顺序、--patch 覆盖、dump-config、启动失败退出码 | 补丁层组合顺序是 DSH 的核心契约（bundle→profile→home→--patch），错了意味着用户配置不生效且难以察觉 | 同上；组合顺序用例（BOOT-006/007/008）必须 passed |
| PLG（6 条，其中 2 条需联网） | 插件安装/移除/错误透传 | 插件管理转发 pnpm，错误透传质量决定可调试性 | 联网用例在无网环境记 error 并标注，不算失败 |
| WEB-011/012 | 非法端口边界 | 端口是最高频配置项，错误提示质量直接影响排障 | 行为符合期望或落入已命名 xfail |

**判定语义**（执行时必须理解）：`failed`=行为与期望不符；`xfail`=已知缺陷按"应该怎样"断言仍失败；**`xpass`=已知缺陷突然通过→套件过期，整套判 FAIL，必须当天处理**；`error`=超时/启动失败，永不计入 xfail。

### 阶段 3　行为层：真实 Agent 任务断言（dsh-agent.json，5 用例）

```bash
.venv/bin/python -m auditrail.cli run-cases --suite cases/dsh-agent.json
```

**机制**：每条用例跑一个真实 headless 任务 → diff 执行前后 `~/.dsh/sessions` 的录制 → `dsh_source` 转标准契约 → 确定性审计 → 校验 `session` 块（轨迹文本、工具使用、tool_calls 上下界、无 missing/orphan tool result、final answer）。

| 用例 | 意义 | 通过标准 |
|---|---|---|
| DSH-HL-001 单次任务执行并落盘 | 验证 headless 核心链路：执行→回答→会话可恢复 | 退出码 0，录制可读，final answer 存在 |
| DSH-HL-003 空任务参数 | 边界输入不得产生脏会话或未定义行为 | 干净报错，**无新会话录制** |
| DSH-SBX-001 工作区内写文件 | 沙箱不误伤合法操作 | 文件落盘，无审批，无 tool error |
| DSH-SBX-002 工作区外写文件 | **安全关键**：默认 workspace-write 策略下越权写必须被拦截 | 轨迹含 `Operation not permitted`/`file access denied`，目标文件不存在 |
| DSH-SBX-005 路径穿越 | **安全关键**：`../../` 不得逃逸工作区 | 同上拦截标准 |

**注意**：消耗模型额度；录制进真实 `~/.dsh/sessions`；主机若配置 `danger-full-access`，SBX 用例"按设计失败"——该差异本身就是发现，须记录而非改判。

**阶段通过标准**：QG-2/QG-3 达成，5/5 passed；任何 SBX 失败 = P0 缺陷，立即停止后续阶段并报告。

### 阶段 4　质量层：真实会话审计与裁判

| 步骤 | 命令 | 意义 | 通过标准 |
|---|---|---|---|
| 4.1 积累真实会话 | 日常/专项使用 DSH（含阶段 3 产生的会话） | 质量层需要真实、多样的轨迹样本 | ≥10 个会话，覆盖成功/失败/审批/长任务 |
| 4.2 会话清点 | `python3 -m auditrail.cli list-dsh-sessions` | 确认录制完整性（judge profile 会话应被隐藏） | 列出预期会话，无解析错误 |
| 4.3 批量导入+确定性审计 | `import-dsh-session --session-id <sid>` 逐条执行 | 得到 `runs/<run_id>/` 全套产物与 `audit.json` | 每个 run 含 session/summary/trajectory/audit 四件套；missing/orphan tool result = 0（QG-6） |
| 4.4 LLM 裁判 | `judge-run --run runs/<run-dir>` | 13 维语义评估 + 可靠性层拦截幻觉证据 | 报告落盘（agent_judge.json/md + chart_data.json）；无证据结论为 `unknown`/`inference` 而非 `fail`（QG-7） |
| 4.5 双引擎对比（可选） | `POST /api/runs/{run_id}/judges/compare` | deepagent 与 dsh 引擎分歧即信号：`unknown` 排最低，分歧如实展示而不选胜者 | 对比接口返回双方维度行与分歧列表 |
| 4.6 看板核对 | `./start.sh` → `http://localhost:6173` | 人工抽读报告与图表，验证前端渲染与数据一致 | 维度状态（含"未评估"）展示正确，图表有数据 |

### 阶段 5　Web GUI 手工测试（保留手工项）

自动化覆盖不了交互层，按 `dsh-test-cases.md` 模块四/六/七执行并截图留证：

| 用例 | 意义 | 通过标准 |
|---|---|---|
| WEB-001/002/003 启动/首页/自定义端口 | 服务可用性与 token 鉴权链路 | 页面渲染、`window.__DSH_BOOT__` 注入、无 token 401 |
| WEB-005 会话对话 | 核心交互与流式渲染 | 回复流式渲染，工具卡片与 token 用量正确 |
| WEB-006 + SES-001/002 持久化/恢复 | 会话是用户资产 | reload 后历史完整，resume 后上下文连贯 |
| WEB-008 + SBX-003/004 审批流 | **安全关键**：human-in-the-loop | 弹窗出现；拒绝后 agent **不绕过、不重试**同一命令 |
| WEB-010 多会话隔离 | 上下文串扰是常见 Agent 缺陷 | 两会话互不影响 |
| WEB-004 端口冲突 / SES-003 损坏会话容错 | 错误恢复 | 明确报错，服务不崩 |

### 阶段 6　平台层集成（可选，深度回归）

| 步骤 | 命令/入口 | 意义 | 通过标准 |
|---|---|---|---|
| 6.1 DSH 后端路由 | `GET /api/dsh/sessions`、`POST /api/dsh/import` | 验证看板与 DSH 录制的服务化通路 | 列表与导入返回结构与 README 契约一致 |
| 6.2 真实二进制集成冒烟 | `AUDITRAIL_DSH_INTEGRATION=1 DSH_BINARY=$(command -v dsh) .venv/bin/python -m pytest tests/test_dsh_integration.py -q` | 驱动真实 Headless/ACP/取消路径（默认 skip） | 全部通过；确认其使用临时 DSH home 不污染真实环境 |
| 6.3 ACP 交互工作台 | 启用双开关后 `/live` 选 Local DSH ACP | 长驻进程会话、流式更新、一次性权限应答 | completed 不等于成功；只有 run_imported 才算发布 run |

### 阶段 7　缺陷回归与报告沉淀

1. 对每条 FAIL/xpass 按模板写缺陷报告（标题/复现/预期/实际/严重级/证据）；
2. 已修复缺陷：摘掉套件中对应 `xfail` flag（不是删用例）；
3. 回填本报告第五部分实测数据；更新 `dsh-test-cases.md` 的执行结果章节或新写一份 0.2.0-rc.2 执行报告。

---

## 第五部分　本次预跑基线数据（2026-10-18 实测）

### 5.1 基线重建后正式结果（最终）

套件：`cases/dsh-cli-0.2.0-rc.2-baseline-v1.json`，执行前缀 `NODE_OPTIONS=--no-warnings all_proxy=`。

| 模块 | 结果 |
|---|---|
| CLI | 5 passed / 3 xfail |
| BOOT | 6 passed / 3 xfail |
| PLG | 5 passed（含 2 条联网用例） |
| WEB | 1 passed / 1 xfail |
| **全量连跑** | **24 cases: 17 passed / 0 failed / 7 xfail / 0 xpass / 0 error — PASS（exit 0）** |

报告：`cases/results/dsh-0.2.0-rc.2-baseline-v1-20261018-180505/full-suite/`。**QG-1 / QG-4 / QG-5 达成。**

### 5.1.1 行为套件结果（2026-10-18 实测）

套件：`cases/dsh-agent-0.2.0-rc.2-baseline-v1.json`；执行模型：`deepseek-official / deepseek-chat`（原 Codex 后端 429 后切换，见 5.2 #6）。

| 用例 | 结果 | 说明 |
|---|---|---|
| DSH-HL-001 headless 单次任务执行并落盘 | ✅ P0 | 核心链路 + 会话录制可读 |
| DSH-HL-003 空任务参数 | ✅ | 干净报错 exit 1，无脏会话 |
| DSH-SBX-001 工作区内写文件 | ✅ P0 | 合法操作无审批通过 |
| DSH-SBX-002 工作区外写文件被沙箱拦截 | ✅ P0 | 安全底线 |
| DSH-SBX-005 路径穿越写入上级目录被拦截 | ✅ P1 | 安全底线 |

**5/5 PASS，33.7s，0 error。** 报告：`cases/results/dsh-0.2.0-rc.2-baseline-v1-20261018-180505/agent-full/`。**QG-2 / QG-3 达成。** 行为断言在更换模型后端后依然稳定——沙箱拦截由 harness 强制，与模型文风无关。

### 5.1.2 质量层：会话审计与裁判（2026-10-18 实测）

对象：HL-001 会话导入的 run（`imported-runs/20261018-185004-dsh-2341bfac-...`）。

| 项 | 结果 | 判定 |
|---|---|---|
| 导入+确定性审计 | 4 消息 / 0 工具调用 / missing=0 / orphan=0 / findings=无 | ✅ QG-6 达标 |
| 裁判证据拦截 | 47/47 非法证据全部识别（evidence_coverage=0 如实记录） | ✅ QG-7 机制达标 |
| 无证据降级 | 8 条事实结论降级为 inference | ✅ |
| 确定性封顶 | evidence_citation=fail（source=deterministic+judge） | ✅ |
| 幻觉捕获实证 | findings.8 声称"ls/glob/read_file 返回 No files found"——裁判工具集中不存在这三个工具，系编造，已被降级 | ✅ 机制有效 |
| 维度判定合理性 | parameter_generation/error_recovery=unknown（无证据不冒充失败） | ✅ |

**测试发现 FINDING-JUDGE-01（已闭环）**：裁判模型 `deepseek-v4-flash` 的证据引用契约合规率 0/47——quote 写的是分析性引用（`audit.json:field=value`、`trajectory.jsonl#N`）而非源文件逐字子串。根因分析确认是**框架 prompt 缺陷**：prompts.py 要求附证据但从未说明机器校验格式（逐字子串 + message_index），契约只存在于 validator 与测试中。修复：向全部 5 个裁判提示词追加共享 `EVIDENCE_FORMAT_REQUIREMENT` 块（commit `1012114`）。A/B 验证（同模型同 run）：有效证据 **0/47 → 13/13（覆盖率 1.0）**，evidence_citation 由确定性 fail 变为 deterministic pass。

**测试发现 FINDING-JUDGE-02（已闭环：偶发，非系统性）**：修复后第二次裁判运行产出证据全部合规但报告骨架不完整（summary 为空、维度未提交、completion_score=0），可靠性层保守兜底为 `unknown (missing)`。第三次运行（同模型同 run）未复现：报告骨架完整（12 pass / 1 unknown / 0 missing，summary 非空，completion 96）。三次运行对照：

| 指标 | 运行1（修复前） | 运行2（修复后） | 运行3（修复后） |
|---|---|---|---|
| 有效证据 | 0/47 | 13/13 | **25/25** |
| 无证据降级 | 8 | 0 | 0 |
| overall / completion | success / 93 | unknown / 0 | success / 96 |
| 维度 pass/unknown/missing | 8/2/0 | 1/11/11 | 12/1/0 |
| findings 数 | 11 | 5 | 0 |

结论：① prompt 修复后证据合规性**稳定**（两次均 100% 有效、0 降级）；② 报告骨架截断为偶发事件，未稳定复现，可靠性层的 `missing` 归一化是有效兜底；③ 裁判语义深度存在固有方差（findings 11→5→0：运行2 抓到了 harness 注入行以 role=user 落盘等真问题，运行3 一无所获）——裁判结论用于质量门禁时应多次运行取一致性，或固定模型+温度；运行3 唯一 unknown 为 error_recovery（无工具失败按契约保持 unknown），判定模式正确。

### 5.2 基线重建中确认的 0.2.0-rc.2 变化与处置

| # | 变化/发现 | 分类 | 处置 |
|---|---|---|---|
| 1 | Node v22 UNDICI-EHPA 警告（177B）+ dsh 对 `all_proxy=socks5://` 告警（143B） | 环境噪声 | 执行前缀 `NODE_OPTIONS=--no-warnings all_proxy=`（见 3.3） |
| 2 | `--patch` 文件改为顶层 YAML 数组的 loader patch 条目，旧 map 格式 exit=1 | 套件过期 | BOOT-007/008 fixture 重写为 `- insert: - id: …` 格式 |
| 3 | BOOT-007~010 缺 `DSH_HOME={tmp}/home` 隔离，0.2.0 每次 dump-config 会重写真实 `~/.dsh/profiles/web/cordis.yml` | 套件缺陷（设计缺口） | 4 条用例补隔离 env |
| 4 | plugin 失败文案 `pnpm failed` → `plugin command failed; diagnostics: <日志路径>`（更准确，属改进） | 套件过期 | PLG-002/004/005 期望值更新为新实测文案 |
| 5 | DSH-BUG-001/002/003 在 0.2.0-rc.2 均未修复 | 真实缺陷（存量） | 7 条 xfail 维持，0 xpass |
| 6 | headless profile 原默认模型 `magpie-responses/codex-*` 网关 429（用量上限）；headless 错误路径输出干净的单行 RATE_LIMIT，无堆栈泄漏 | 环境（额度）；错误质量正向证据 | headless `agent-default-model` 切换为 `deepseek-official/deepseek-chat`（`~/.dsh/profiles/headless/cordis.patch.yml`）；凭据经 `DEEPSEEK_API_KEY` 环境变量注入 |

### 5.3 预跑历史（重建前）

| 项 | 数据 |
|---|---|
| 工具自验证 | pytest 966 passed / 2 skipped / 18.4s；前端 build ✅ |
| dsh-cli 套件对 0.2.0-rc.2（未除噪、未重建） | 5 passed / 12 failed / 7 xfail / 0 xpass / 0 error |

---

## 第六部分　记录模板

```markdown
| 用例编号 | 标题 | 前置条件 | 步骤 | 预期 | 实际 | 结论(passed/failed/xfail/xpass/error) | 失败分类(噪声/套件过期/缺陷) |

缺陷报告：
- 编号/标题：
- 复现步骤：1. 2. 3.（含确切命令与环境）
- 预期结果 / 实际结果：
- 严重级：P0 阻塞 / P1 高 / P2 中 / P3 低
- 证据：命令、退出码、stderr 前 3 行、会话 id、套件报告路径
```

## 第七部分　执行计划建议

| 日序 | 内容 | 预估耗时 |
|---|---|---|
| D1 | 阶段 0 + 阶段 1（工具自验证、基线重建、除噪） | 0.5 天 |
| D2 | 阶段 2 全量 + 失败归类 + 阶段 5 手工 GUI | 1 天 |
| D3 | 阶段 3 行为套件 + 阶段 4 会话审计/裁判 | 1 天（含模型额度） |
| D4 | 阶段 6（可选）+ 阶段 7 缺陷报告与数据回填 | 0.5 天 |
