# DeepSeek Harness（DSH）测试用例

> 测试对象：DeepSeek Harness v0.1.2-rc.1（`@deepseek-ai/dsh`）
> 测试类型：功能测试（黑盒）+ 接口/异常场景
> 设计方法：等价类划分、边界值分析、场景法、错误推测法
> 编写日期：2026-XX-XX　编写人：XXX

## 1. 测试范围说明

DSH 是 Agent 运行时启动器：通过 `dsh` 命令引导 profile（web / headless / sdk / sdk-minimal / acp），按序组合插件补丁层（bundle → profile 层 → home 层 → `--patch` 覆盖层），并提供 Web GUI、插件管理（转发 pnpm）、文件沙箱与审批等能力。

| 模块 | 覆盖内容 |
|---|---|
| CLI 启动器 | 命令语法、参数归属、非法输入、退出码 |
| Profile 引导 | 自动初始化、补丁层组合顺序、dump 配置 |
| 插件管理 | `dsh plugin` 转发 pnpm、profile 校验 |
| Web GUI | 服务启动、页面加载、会话交互 |
| Headless 模式 | 单次任务执行、输出、退出 |
| 文件沙箱与审批 | 工作区读写策略、越权拦截、审批流 |
| 会话管理 | 会话持久化、恢复、压缩 |

---

## 2. 测试用例

### 模块一：CLI 启动器（参数解析与命令语法）

| 用例编号 | 用例标题 | 优先级 | 前置条件 | 测试步骤 | 预期结果 |
|---|---|---|---|---|---|
| DSH-CLI-001 | 查看启动器帮助 | P1 | dsh 已安装 | 执行 `dsh --help` | 输出 Usage、Options（--profile/--patch/--dump-config 等）、Commands（web/plugin）及示例，退出码 0 |
| DSH-CLI-002 | 查看版本号 | P1 | 同上 | 执行 `dsh -V` | 输出版本号 `0.1.2-rc.1`，退出码 0 |
| DSH-CLI-003 | web 别名等价性 | P1 | web profile 可启动 | 分别执行 `dsh web` 与 `dsh --profile web` | 两者引导同一 profile，行为一致 |
| DSH-CLI-004 | 非法命令退出码 | P0 | 同上 | 执行 `dsh --profile not-exist-cmd`（不存在的 profile 且未通过 plugin 创建） | 进程以非 0 退出码退出，stderr 给出错误信息，不静默成功 |
| DSH-CLI-005 | 混淆其他模式的选项 | P1 | 同上 | 执行 `dsh plugin --profile web --port 9999`（--port 属于 web app，不属于 plugin 模式） | 进程报错并非 0 退出，提示选项不属于当前模式 |
| DSH-CLI-006 | 应用参数透传 | P1 | 同上 | 执行 `dsh --profile web --help` | 输出的是 web app 自身的帮助（如 --port 等），而非启动器帮助 |
| DSH-CLI-007 | 首个未识别 token 后的参数归 app | P2 | 同上 | 执行 `dsh --profile web --port 8080` | `--port 8080` 被透传给 web app，启动器不解析该 flag |
| DSH-CLI-008 | 缺少 --profile 值 | P1 | 同上 | 执行 `dsh --profile`（不带值） | 参数解析报错，非 0 退出，提示缺少参数值 |
| DSH-CLI-009 | 未知 flag 报错 | P2 | 同上 | 执行 `dsh --nonexistent-flag` | 启动器报错并非 0 退出 |

### 模块二：Profile 引导与配置组合

| 用例编号 | 用例标题 | 优先级 | 前置条件 | 测试步骤 | 预期结果 |
|---|---|---|---|---|---|
| DSH-BOOT-001 | web profile 首次自动初始化 | P0 | `$DSH_HOME/profiles/web` 不存在 | 执行 `dsh web` | 自动从模板初始化 profile 目录（生成 cordis.yml 等），随后正常启动 |
| DSH-BOOT-002 | headless profile 自动初始化 | P1 | headless profile 不存在 | 执行 `dsh --profile headless "hello"` | 自动初始化并执行任务 |
| DSH-BOOT-003 | 非内置 profile 不自动初始化 | P1 | `myprofile` 未创建 | 执行 `dsh --profile myprofile` | 报错并非 0 退出，提示需通过 `dsh plugin` 创建 |
| DSH-BOOT-004 | dump 默认配置不启动 | P1 | web profile 可用 | 执行 `dsh --profile web --dump-default-config` | 仅打印不含用户层和 --patch 层的组合配置树，进程退出且不启动服务（端口未被监听） |
| DSH-BOOT-005 | dump 完整配置不启动 | P1 | 同上 | 执行 `dsh --profile web --dump-config` | 打印含用户层的完整组合树后退出，不启动服务 |
| DSH-BOOT-006 | 补丁层组合顺序 | P1 | 准备 bundle 层、profile 层 `cordis.patch.yml`、home 层 `$DSH_HOME/cordis.patch.yml` 中对同一项的不同值 | 执行 `--dump-config` 对比 | 组合结果遵循优先级：bundle 顺序 → profile 层 → home 层 → `--patch` 覆盖层，后层覆盖前层 |
| DSH-BOOT-007 | --patch 覆盖层生效 | P1 | 准备一个合法 overlay yml | 执行 `dsh --profile web --patch ./extra.yml --dump-config` | dump 结果中包含 overlay 的覆盖内容 |
| DSH-BOOT-008 | --patch 可重复 | P2 | 准备两个 overlay 文件 | 传入两个 `--patch` 参数 | 两个 overlay 按传入顺序依次应用 |
| DSH-BOOT-009 | --patch 指向不存在文件 | P1 | 同上 | `--patch ./missing.yml` | 配置错误，非 0 退出并提示文件问题 |
| DSH-BOOT-010 | --patch 非法 YAML | P2 | 准备语法损坏的 yml | 同上 | 报 YAML 解析错误，非 0 退出 |
| DSH-BOOT-011 | 启动失败退出码 | P0 | 构造一个会启动失败的 profile（如端口被占用） | 启动 profile | 进程非 0 退出，输出 boot 失败原因 |
| DSH-BOOT-012 | 调用目录为默认工作区 | P2 | 在目录 A 下启动 | 启动后观察会话 workspace | 默认 workspace root 为启动时的调用目录 |

### 模块三：插件管理（dsh plugin）

| 用例编号 | 用例标题 | 优先级 | 前置条件 | 测试步骤 | 预期结果 |
|---|---|---|---|---|---|
| DSH-PLG-001 | 安装插件到 profile | P1 | 网络可用、profile 存在 | `dsh plugin --profile web add <package>` | 参数转发给 pnpm 在 profile 目录执行，依赖写入 profile 的 package.json 并安装到其 node_modules |
| DSH-PLG-002 | 移除插件 | P1 | 插件已安装 | `dsh plugin --profile web remove <package>` | 依赖从 package.json 移除，node_modules 同步清理 |
| DSH-PLG-003 | 缺少 --profile 报错 | P1 | 同上 | `dsh plugin add <package>` | 报错并非 0 退出 |
| DSH-PLG-004 | 目标 profile 不存在 | P1 | 同上 | `dsh plugin --profile ghost add <pkg>` | 按设计先初始化该 profile（BOOT-003 的报错文案即指引用 plugin add 创建 profile），pnpm 失败后非 0 退出；会留下已初始化的空 profile 目录 |
| DSH-PLG-005 | 安装不存在的包 | P2 | 网络可用 | add 一个不存在包名 | pnpm 报错透传，非 0 退出 |
| DSH-PLG-006 | 离线安装失败提示 | P3 | 断网 | add 任意新包 | 网络错误明确暴露，不静默吞错 |

### 模块四：Web GUI

| 用例编号 | 用例标题 | 优先级 | 前置条件 | 测试步骤 | 预期结果 |
|---|---|---|---|---|---|
| DSH-WEB-001 | Web 服务启动 | P0 | web profile 可用 | `dsh web` 启动 | 控制台输出监听地址，默认可访问 `http://127.0.0.1:3080` |
| DSH-WEB-002 | 首页加载 | P0 | 服务已启动 | 浏览器打开 GUI 地址 | 页面正常渲染，注入 `window.__DSH_BOOT__`，无白屏/500 |
| DSH-WEB-003 | 自定义端口 | P1 | 同上 | `dsh web --port 8080` | 服务监听 8080，原默认端口不再占用 |
| DSH-WEB-004 | 端口冲突 | P1 | 3080 已被占用 | 再次启动默认端口实例 | 明确报错并非 0 退出 |
| DSH-WEB-005 | 新建会话并对话 | P0 | 模型路由可用 | 页面创建会话，发送一条消息 | 助手回复正常流式渲染，工具调用卡片正确展示 |
| DSH-WEB-006 | 会话持久化与恢复 | P1 | 存在历史会话 | 重启服务后打开历史会话 | 历史消息完整加载，可继续对话 |
| DSH-WEB-007 | 会话压缩 | P2 | 长上下文会话 | 触发 compact | 压缩后上下文摘要正确，会话可继续 |
| DSH-WEB-008 | 审批请求 UI | P1 | 触发需审批操作（如越权写文件） | 观察 UI | 出现审批提示，允许/拒绝后流程按选择继续或终止；拒绝后不重复弹同一请求 |
| DSH-WEB-009 | 后台任务展示 | P2 | 启动一个长任务 | 观察任务卡片 | job 状态（运行/完成/失败）正确更新 |
| DSH-WEB-010 | 多会话隔离 | P2 | 同开两个会话 | 分别对话 | 会话上下文互不串扰 |

### 模块五：Headless 模式

| 用例编号 | 用例标题 | 优先级 | 前置条件 | 测试步骤 | 预期结果 |
|---|---|---|---|---|---|
| DSH-HL-001 | 单次任务执行 | P0 | headless profile 可用、模型可用 | `dsh --profile headless "run the tests"` | 创建一次全新持久化会话，执行后打印最终回答并退出，退出码 0 |
| DSH-HL-002 | 任务失败退出码 | P1 | 同上 | 提交一个必然失败的任务 | 最终输出错误信息，退出码非 0 |
| DSH-HL-003 | 空任务参数 | P2 | 同上 | `dsh --profile headless ""` | 明确报错或提示，不发生未定义行为 |
| DSH-HL-004 | 产物落盘 | P2 | 执行任务后 | 检查 `$DSH_HOME/sessions` | 生成对应会话记录，可用于后续 resume |

### 模块六：文件沙箱与审批

| 用例编号 | 用例标题 | 优先级 | 前置条件 | 测试步骤 | 预期结果 |
|---|---|---|---|---|---|
| DSH-SBX-001 | 工作区内写文件 | P0 | 策略为 workspace-write | 让 agent 在工作区创建文件 | 写入成功，无需审批 |
| DSH-SBX-002 | 工作区外写文件被拦截 | P0 | 同上 | 让 agent 写 `$HOME` 下任意文件（如 `~/.dsh/...`） | 操作被拒绝，返回 `file access denied under workspace-write mode`，不产生部分写入 |
| DSH-SBX-003 | 拒绝后升级审批 | P1 | 审批策略为 ask | 沙箱拒绝后按策略请求更宽权限 | 弹出审批请求；用户批准后同一命令重试成功 |
| DSH-SBX-004 | 用户拒绝审批 | P1 | 同上 | 审批弹窗选择拒绝 | 该命令最终失败，agent 不绕过、不重试同一命令 |
| DSH-SBX-005 | 路径穿越防护 | P1 | 同上 | 尝试 `../../` 越界路径写入 | 被沙箱拦截，无法逃逸工作区 |
| DSH-SBX-006 | 读取工作区外文件 | P2 | 同上 | 读取工作区外普通文件 | 按当前策略放行或拒绝，行为与策略一致 |

### 模块七：会话管理

| 用例编号 | 用例标题 | 优先级 | 前置条件 | 测试步骤 | 预期结果 |
|---|---|---|---|---|---|
| DSH-SES-001 | 会话列表加载 | P1 | 存在多个历史会话 | 打开会话列表 | 会话按时间正确排序展示 |
| DSH-SES-002 | 会话恢复上下文 | P1 | 历史会话含多轮对话 | resume 会话后继续提问 | 模型能看到历史上下文，回答连贯 |
| DSH-SES-003 | 损坏会话文件容错 | P2 | 构造损坏的会话记录 | 加载该会话 | 明确报错提示，不导致整个服务崩溃 |
| DSH-SES-004 | 目标（goal）跨轮继续 | P2 | 创建长任务 goal | 观察自动续轮 | goal 状态（进行中/完成/阻塞）正确流转，达到轮次上限或完成后停止 |

---

## 3. 测试环境

| 项 | 值 |
|---|---|
| OS | macOS（darwin arm64） |
| Node.js | v22.x |
| DSH 版本 | 0.1.2-rc.1 |
| 安装方式 | `npm i -g @deepseek-ai/dsh` |
| DSH_HOME | `~/.dsh` |

## 4. 简历表述参考（项目经历 bullet）

- 针对 Agent 运行时启动器 DeepSeek Harness 设计 **46 条功能与异常用例**（等价类/边界值/错误推测），其中 **29 条自动化**为可持续回归套件（argv 断言 + headless 行为断言），覆盖 CLI、配置组合、插件管理、沙箱审批等 7 大模块；
- 扩展自研评测框架 agentloop 的用例 runner：新增环境隔离（`DSH_HOME` 夹具）、文件系统断言与**「跑真实 agent 任务 → diff 会话录制 → 确定性审计」的行为级断言**，把框架从轨迹评测升级为 Agent 黑盒 E2E 测试；
- 对 **195 个真实会话（40100 条消息、19931 次工具调用）** 做批量确定性审计，测得工具错误率 3.6%；对 18 个代表性会话执行 LLM 裁判，验证裁判可靠性层拦截 **522 条幻觉证据**并自动降级 119 条无证据结论；
- 黑盒测试共发现 **3 个 DSH 缺陷**（错误堆栈泄漏、误导性报错文案等）与 **6 个评测框架自身缺陷**（含 LLM null 参数崩溃、证据提取信封兼容、交付物判定被 system-reminder 注入污染），全部修复并配回归测试，项目测试数 156 → 174。

---

## 5. 执行结果（2026-09-27，DSH 0.1.2-rc.1，macOS arm64 / Node v22.23.0）

### 5.1 自动化执行（agentloop `run-cases`）

| 套件 | 用例数 | 结果 | 报告 |
|---|---|---|---|
| `cases/dsh-cli.json`（argv 套件） | 24 | 17 passed / 0 failed / 7 xfail | `cases/results/dsh-cli-*.md` |
| `cases/dsh-agent.json`（headless 行为套件） | 5 | 5 passed | `cases/results/dsh-agent-*.md` |

argv 套件全部在隔离 `DSH_HOME` 下运行，不污染真实环境；行为套件通过
「跑真实 headless 任务 → diff 会话录制 → 确定性审计」断言沙箱拦截等运行时行为。

### 5.2 缺陷清单

| 缺陷 | 严重级 | 表现 | 关联用例 |
|---|---|---|---|
| DSH-BUG-001 | 中 | 配置/参数/校验错误抛未捕获异常，Node 堆栈与源码行泄漏到 stderr | DSH-CLI-004、DSH-BOOT-003/009/010、DSH-WEB-012 |
| DSH-BUG-002 | 低 | 未知 flag 报出与被拒参数无关的 `--profile` 必填错误，未指出真正的问题参数 | DSH-CLI-009 |
| DSH-BUG-003 | 低 | plugin 模式未拦截 app 专属选项，`--port` 被透传给 pnpm，报出误导性错误（"Did you mean 'sort'?"） | DSH-CLI-005 |

### 5.3 Web GUI 实测（CDP 驱动的真实浏览器）

| 用例 | 结果 | 证据 |
|---|---|---|
| DSH-WEB-001 服务启动 | ✅ | 独立实例 `dsh web --port 18081`，打印带 token 的 URL |
| DSH-WEB-002 首页加载 | ✅ | 无 token 401；token→303→签名 cookie→200，`window.__DSH_BOOT__` 注入 |
| DSH-WEB-003 自定义端口 | ✅ | 18081 正常监听 |
| DSH-WEB-005 新建会话并对话 | ✅ | 流式回复渲染正常，token 用量/耗时卡片正确 |
| DSH-WEB-006 会话持久化与恢复 | ✅ | 整页 reload 后历史会话与消息完整 |
| DSH-WEB-008 / DSH-SBX-003 / DSH-SBX-004 审批流 | ✅ | 越权写 → 沙箱拒绝 → 提权审批弹窗（拒绝/允许一次）→ 拒绝后 agent 明确不绕过、不重试 |
| DSH-WEB-010 多会话隔离 | ✅ | 切换会话上下文互不串扰 |

### 5.4 用例自动化映射

- **已自动化（argv 套件）**：DSH-CLI-001/002/003/004/005/006/008/009、DSH-BOOT-001/003/004/005/006/007/008/009/010、DSH-PLG-001/002/003/004/005、DSH-WEB-011/012
- **已自动化（行为套件）**：DSH-HL-001/003/004、DSH-SBX-001/002/005
- **GUI 实测通过**：DSH-WEB-001/002/003/005/006/008/010、DSH-SBX-003/004
- **探测验证（报告记录）**：DSH-BOOT-011（端口冲突报错，属 DSH-BUG-001）、DSH-PLG-006（离线失败提示）
- **保留手工**：DSH-BOOT-002/012、DSH-HL-002、DSH-WEB-004/007/009、DSH-SES-001/002/003/004、DSH-SBX-006
