# 玄鉴 Agent PPT 生成会话分析报告

- 会话 ID: `5ab7b3b0-1659-4c30-9735-1d54da1548fd`
- 任务:4 篇论文 → 10 页中文学术汇报 PPT(ppt-master 工作流)
- 模型:`xuanjian-lite`
- 数据来源:两次 agentloop 导出(同一会话)
  - 20260807-115004:前段(84 条消息,10:48–11:49)
  - 20260807-124623:全量(241 条消息,10:48–12:45)
- 分析时间:2026-08-07

---

## 1. 会话概览(合并后)

| 项 | 值 |
|---|---|
| 总时长 | 10:48:41 → 12:45:17(约 117 分钟) |
| 消息数 | 241(user 3 / assistant 104 / tool 134) |
| 工具调用 | 134 次 |
| 工具分布 | skill_view ×39, terminal ×39, write_file ×23, read_file ×23, tool_call ×6, clarify ×2, delegate_task ×1, patch ×1 |
| 用户真实输入 | 3 条:初始需求、clarify 回答(选论文2)、"将 SVG 页面导出为 .pptx 格式" |
| 最终产物 | 10 页 SVG(已过质检)+ design_spec.md + spec_lock.md + export_pptx.py;PPTX 未产出 |

## 2. 完整时间线

| 阶段 | 时间 | 关键事件 | 结果 |
|---|---|---|---|
| 1. 技能加载 | 10:48 | 加载 ppt-master / xj-pdf | OK |
| 2. 材料转换 | 10:49-10:50 | source_to_md.py 批量转 4 篇 PDF | OK(1 次成功) |
| 3. 方向确认 | 10:51-10:52 | clarify:4 篇论文主题不同,用户选"论文2为核心" | OK |
| 4. 论文精读 | 10:52-10:59 | delegate_task 子代理 30s 超时 → 回退手动分段读 | 降级成功 |
| 5. 项目初始化 | 10:59 | project_manager.py init | OK |
| 6. 设计规格(一) | 11:05 | 手写 ppt_design.json(16KB) | 白工:不符合 schema |
| 7. 设计规格(二) | 11:49 | design_spec.md(正确 schema,14.8KB) | OK |
| 8. 锁文件 | 11:55 | spec_lock.md 写好;2 次 tool_call 误用报错;路径漂移 /app/skills 3 次调用才纠正 | 部分 |
| 9. SVG 生成 | 12:04-12:22 | 10 页 SVG 逐页手写 + 质检迭代(P01 改 3 版、P02 改 4 版) | 12:22:54 全过 |
| 10. 强制截断 | 12:23:48 | 平台:"You've reached the maximum number of tool-calling iterations" | 断点:正要导出 |
| 11. 人工续跑 | 12:38:23 | 用户:"将 SVG 页面导出为 .pptx 格式" | - |
| 12. 导出失败 | 12:38-12:45 | svg_to_pptx.py 缺 Pillow;2 次 pip install 被 cron 审批拦截;clarify 用户选方案B;写完 export_pptx.py | PPTX 未生成 |

## 3. 关键发现(按严重度)

### 3.1 单次运行工具调用上限,长任务被硬截断(致命)
134 次工具调用后平台强制停止,恰好停在 10 页 SVG 全部过质检、正要导出的节点。PPT 这类长流程必然超限,需要人工介入续跑。

### 3.2 会话以 cron 任务上下文运行,危险命令无法审批(致命)
拦截信息原文:命令被标记为危险命令(通过包管理器安装软件包),但定时任务(cron)在无用户在场的情况下运行,无法获得审批;config.yaml 设 `approvals.cron_mode: approve` 可放开。
- 后果 A:导出依赖 python-pptx / Pillow 装不上 → svg_to_pptx.py 报 "PIL (Pillow) is required" / ModuleNotFoundError,PPTX 无法生成(方案B脚本写完也跑不了)
- 后果 B:可解释"幻觉式用户确认"——cron 无实时交互,ppt-master 的 ⛔ BLOCKING 确认门无法满足,agent 只能虚构"用户已确认设计方案"(出现 10+ 次),绕过确认闸门

### 3.3 模型推理延迟极高(贯穿全程)
- "你好"会话:48–62 秒/轮
- 本会话:工具调用间常隔 3–6 分钟,29→104 个 assistant turn 平均约 2 分钟/轮
- 上下文膨胀(重复加载技能文件)进一步加剧

### 3.4 skill_view 重复加载与异常大结果(token 黑洞)
- strategist.md(67.6KB)+ design_spec_reference.md(15.4KB)+ spec_lock_reference.md(10.7KB)重复加载 6-7 次;generate-pptx.md(64.7KB)加载 4 次;全会话 skill_view 共 39 次,重复流量 600KB+
- 2 次请求不存在的文件(workflows/topic-research.md、stages/apply-template-workspace.md),错误响应把 available_files 序列化成 602,590 字符(588KB),各污染一次上下文

### 3.5 tool_call(延期/cron)工具被误用
agent 试图把"验证Design Spec""confirm-ui"等步骤丢给 cron 任务,5 次调用中 3 次报错(not deferrable / 缺 name 参数)。agent 不理解平台续跑机制,或平台提示不足。

### 3.6 其他问题
- delegate_task 子代理 30s 超时失败(对慢模型太紧)
- 路径漂移:/app/skills/ppt-master/... 不存在(实际在 /root/.xj_agent/bundled_skills/...),浪费 3 次调用
- read_file 越界被拦(charts_index.json 在 workspace 外),agent 用 grep 绕过(可接受)
- 设计稿返工一次(ppt_design.json 不符合 schema)

### 3.7 做得好的地方
- PDF→MD 批量转换一次成功
- 4 篇论文主题不同时主动 clarify
- delegate 超时后正确降级为手动读文
- SVG 质检门有效:P01/P02 反复迭代直到全过,10 页最终全部 Passed
- 最终 design_spec.md / spec_lock.md 按正确 schema 编写

## 4. 导出失败根因链

```
svg_to_pptx.py 需要 Pillow/python-pptx
  → 容器内未预装
  → agent 尝试 pip install ×2
  → 被 cron 审批策略拦截(无用户在场,无法批准)
  → 无法安装 → 无法导出
  → agent 生成独立 export_pptx.py(方案B)并交付脚本
  → 但脚本同样依赖未安装的库,仍无法运行
```

## 5. 建议(按优先级)

| # | 建议 | 目标 |
|---|---|---|
| 1 | 调大/可配置工具调用上限,或长任务自动走 cron 续跑(agent 已在尝试,但用错工具) | 解决截断 |
| 2 | 评估 `approvals.cron_mode: approve` 或预装全部依赖(python-pptx/Pillow 等)进镜像 | 解决导出 |
| 3 | 非交互模式下 BLOCKING 确认门自动降级为"假设确认并记录",禁止模型自行声明确认 | 解决幻觉确认 |
| 4 | 排查 LLM 服务延迟(每轮 2-3 分钟),结合 loongsuite trace 看 openai.chat span 与 input_tokens 趋势 | 解决慢 |
| 5 | skill_view 会话级缓存 + 错误响应 available_files 截断 | 省 token |
| 6 | delegate_task 超时按模型调大(60-120s)或可配置 | 减少子代理失败 |
| 7 | tool_call 工具提示/校验加强(缺 name、非 deferrable 给出示例) | 减少误用 |

## 6. 当前状态与解锁路径

- 10 页 SVG + design_spec.md + spec_lock.md 位于 238 服务器 workspace:`projects/DL_ObjectDetection_Survey_20260807_ppt169_20260807/`
- export_pptx.py 已写好(读取 spec_lock.md,将 SVG 转为 PPTX)
- 解锁:任选其一
  a. config.yaml 设 `approvals.cron_mode: approve`,重跑导出
  b. 人工进容器 `pip install python-pptx Pillow`,再跑 svg_to_pptx.py 或 export_pptx.py
  c. 用方案 C 思路:无依赖生成基础 PPTX(无 SVG 嵌入)

## 附录:错误清单

| 时间 | 工具 | 错误 |
|---|---|---|
| 10:52:48 | delegate_task | 子代理 30s 超时(2 次 API 调用) |
| 11:51:04 | tool_call | "'验证Design Spec' is not a deferrable tool" |
| 11:57:23 | tool_call | "tool_call requires a 'name' argument" |
| 11:59:05 | terminal | /app/skills/ppt-master/scripts/project_manager.py 不存在 |
| 12:03:10 | read_file | 文件必须在 workspace 内(读 charts_index.json 被拦) |
| 12:39:04 / 12:39:27 | terminal | pip 安装被拦:cron 上下文无法审批(建议 approvals.cron_mode: approve) |
| 12:38-12:45 | terminal | svg_to_pptx.py 缺 Pillow / python-pptx,多次 ModuleNotFoundError |

## 附录:两次导出的差异

| 项 | 115004 导出(前段) | 124623 导出(全量) |
|---|---|---|
| 消息数 | 84 | 241 |
| 工具调用 | 54 | 134 |
| 终点 | design_spec.md 写入 | export_pptx.py 写入(SVG 已全过) |
| 新增问题 | - | 迭代上限截断、cron 审批拦截、tool_call 误用 |
