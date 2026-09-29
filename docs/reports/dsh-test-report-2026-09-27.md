# DeepSeek Harness (DSH) 黑盒测试报告

- 测试对象：`@deepseek-ai/dsh` **0.1.2-rc.1**（Agent 运行时启动器：CLI 引导 / profile 组合 / 插件管理 / Web GUI / headless / 文件沙箱 / 会话管理）
- 测试工具：`agentloop`（本仓库）——用例套件 runner、DSH 会话黑盒导入、确定性工具审计、DeepAgent 13 维裁判、Web 看板
- 环境：macOS arm64 / Node v22.23.0 / 模型路由 Kimi K3；裁判模型 deepseek-flash
- 日期：2026-09-27

## 1. 测试设计

| 测试项 | 方法 | 覆盖 |
|---|---|---|
| CLI/启动器 | 等价类+边界值+错误推测，argv 断言套件 | 帮助/版本/别名等价/参数归属/非法输入退出码 |
| 配置组合 | 隔离 DSH_HOME 夹具 + dump 对比 | 自动初始化、补丁层 bundle→home→--patch 顺序、overlay 错误 |
| 插件管理 | 隔离 profile + 本地包/404 包 | 安装/移除/缺参/错误透传 |
| Agent 运行时行为 | headless 真跑 → diff 会话录制 → 确定性审计 | 沙箱拦截、路径穿越、会话落盘、空任务 |
| Web GUI | 真实浏览器（CDP 驱动 Chrome） | 信任围栏、对话渲染、持久化、审批流、多会话隔离 |
| Agent 行为质量 | 195 个真实会话批量审计 + 18 会话 LLM 裁判 | 工具错误率、13 维评分、裁判证据可靠性 |

设计原则：**确定性检查优先于 LLM 语义判断**；所有预期值来自真实运行的实测捕获，不抄文档。

## 2. 执行结果总览

| 层 | 规模 | 结果 |
|---|---|---|
| argv 用例套件 `cases/dsh-cli.json` | 24 条 | **17 passed / 0 failed / 7 xfail**（3 个缺陷族） |
| 行为套件 `cases/dsh-agent.json` | 5 条 | **5 passed**（沙箱/穿越/落盘全符合预期） |
| Web GUI 实测 | 8 项 | **全部通过** |
| 批量确定性审计 | 195 会话 / 40100 消息 / 19931 工具调用 | 见 §4 |
| LLM 裁判 | 18 个代表性会话 | 见 §5 |

用例自动化映射与逐条状态见 `dsh-test-cases.md` §5；套件报告在 `cases/results/`。

## 3. DSH 缺陷清单

| 缺陷 | 严重级 | 表现 | 关联用例 |
|---|---|---|---|
| DSH-BUG-001 | 中 | 配置/参数/校验错误抛未捕获异常，Node 堆栈与源码行泄漏到 stderr（5 处触发面） | DSH-CLI-004、DSH-BOOT-003/009/010、DSH-WEB-012 |
| DSH-BUG-002 | 低 | 未知 flag 报出与被拒参数无关的 `--profile` 必填错误 | DSH-CLI-009 |
| DSH-BUG-003 | 低 | plugin 模式未拦截 app 专属选项，`--port` 透传给 pnpm 产生误导性错误 | DSH-CLI-005 |

安全与健壮性正向结论：默认 workspace-write 沙箱越权写入与 `../` 穿越均被拦截且无部分写入；审批流「拒绝后不重试、不绕过」；无 token 访问 Web GUI 返回 401；空任务/非数字端口报错干净；pnpm 404 详情完整透传。

## 4. 批量确定性审计（195 个真实 DSH 会话）

| 指标 | 值 |
|---|---|
| 会话 / 消息 / 工具调用 | 195 / 40100 / 19931 |
| 含错误发现的会话 | 126（65%） |
| 工具错误总数 / 错误率 | 717 / **3.6%** |
| 缺失 tool result 的会话 | 5 |
| 无最终回答的会话 | 3 |
| 危险工具告警 | 0 |

工具错误分类：代码异常/Traceback 为主，少量超时、沙箱拒绝、文件缺失。错误集中在超长会话（工具调用 500+ 的会话错误发现最多），符合「长轨迹漂移」经验规律。

> 注：本次审计启用了结构化 `is_error` 信号（修复前基于内容启发式，误差方向相反——会把打印了 "Traceback" 字样的 PASS 日志误判为工具错误）。

## 5. LLM 裁判（18 个代表性会话，deepseek-flash 裁判）

总体结论分布：failed 13 / partial 3 / unknown 1 / success 1。

| 维度 | pass | partial | fail | unknown |
|---|---:|---:|---:|---:|
| tool_selection 工具选择 | 9 | 4 | 0 | 5 |
| execution_closure 执行闭环 | 1 | 15 | 2 | 0 |
| error_recovery 错误恢复 | 0 | 13 | 0 | 5 |
| intent_recognition 意图识别 | 0 | 4 | 0 | 14 |
| evidence_citation 证据引用 | 0 | 0 | 18 | 0 |
| 其余维度（上下文/效率/状态跟踪等） | — | 多数 unknown | 少量 fail | 为主 |

**裁判可靠性层的工作量是本次最有价值的测量**：裁判模型平均证据覆盖率仅 12.4%，18 份报告共拦截 522 条无效证据引用，确定性降级机制把 **119 条无有效证据的 "fact" findings 降级为 inference**——包括裁判幻觉出的「缺失 .pptx 交付物」式结论。这验证了「确定性检查优先、LLM 判断需证据回指」的架构必要性：换成更弱的裁判模型时，系统输出不会变得更误导。

## 6. 测试中修复的 agentloop 自身缺陷（边测边完善）

| # | 缺陷 | 发现方式 |
|---|---|---|
| 1 | 裁判工具 `int(None)` 崩溃（LLM 对可选参数传 null） | judge-run 冒烟 |
| 2 | 裁判报告被包在未知信封键（`judge_report`）下时提取失败 | 批量裁判 3 次失败 |
| 3 | 裁判完全漏掉 `overall_status` 时整跑失败 | 批量裁判重跑 |
| 4 | 交付物提取被 `<system-reminder>` 技能目录注入污染（幻觉 pptx/docx 需求） | 平凡会话被判 failed |
| 5 | run 目录里 agentloop 自己的报告被算作「已交付 .md」 | 同上 |
| 6 | 审计启发式忽略 DSH 结构化 `is_error` 信号，误报/漏报并存 | 工具错误分类分析 |

均已修复并配回归测试；测试总数 156 → **174**。

## 7. 遗留与建议

- judge-run 对 LLM 工具调用抖动无重试（本次靠外层 sweep 补跑）；建议 CLI 加一次自动重试。
- 裁判模型为轻量模型时维度分噪声大（如成功会话 score=0）；建议 completion_score 与 overall_status 做一致性约束。
- 保留手工用例：DSH-WEB-004/007/009、DSH-SES-001~004、DSH-BOOT-002/012、DSH-HL-002、DSH-SBX-006、DSH-PLG-006。
