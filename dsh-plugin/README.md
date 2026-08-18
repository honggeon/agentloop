# dsh-agentloop · DSH 上的 Agentloop 评测插件

把 [agentloop](https://github.com/)（本仓库）接入 DeepSeek Harness：在 DSH Web
**侧边栏底部**放一个「菜单」按钮，点击弹出纵向菜单，内含「设置」（转发打开
shell 原生设置面板）和「Agent测评」两个按钮。「Agent测评」点击后在**中间栏
（conversation 列）区域内**打开评测面板——不遮挡侧边栏，Esc 或 ✕ 关闭。
面板列出本机 DSH 录制的 agent 会话，一键导入 agentloop 做**确定性工具审计**，
并且**指标直接在面板内展示**——审计概览、审计发现、工具调用明细、阶段划分、
轨迹预览，以及 DeepAgent 裁判的 13 维评分（裁判需模型凭据，可选运行）。
也可以从面板跳转 agentloop 看板看完整报告。

## 工作原理

```
~/.dsh/sessions/<workspace>/<session>/session.jsonl.zstd   (DSH 落盘的事件流)
        │  agentloop.dsh_source（zstd 解压 + 事件 → 消息映射）
        ▼
runs/<run_id>/  (session.json / trajectory.jsonl / summary.json / audit.json …)
        ▲
agentloop FastAPI 后端  GET /api/dsh/sessions · POST /api/dsh/import
        ▲  HTTP（本插件 client bundle 直接 fetch）
DSH 侧边栏「菜单」→「Agent测评」→ shell.overlay 全屏面板（本插件，纯客户端，Host 侧空操作）
```

- UI 入口使用两个官方插槽：`sidebar.footer.action`（list 型插槽）放「菜单」
  按钮，点击弹出纵向菜单（`position:fixed`，按按钮视口矩形定位），内含
  「设置」「Agent测评」；shell 原生设置按钮用一段 CSS 压成零尺寸裁切隐藏（保持
  挂载），菜单里的「设置」项通过 JS 点击它打开原生设置面板（面板是
  `position:fixed` 覆盖层，不受裁切影响）。`shell.overlay`（框架级浮层）承载
  面板，面板矩形通过 `ResizeObserver` 跟踪中间栏（`[class*='centerCol']`）
  的视口位置，只覆盖中间栏区域，侧边栏保持可见可用。
- 审计、裁判、看板完全复用 agentloop 现有能力，插件只做入口。
- 数据流走 agentloop 后端 HTTP API，因此 **不需要改 DSH 源码**，也不依赖
  DSH 的 host remote 机制；CORS 已默认放行 `http://127.0.0.1:3080`。

## 安装

```bash
# 1. 启动 agentloop 后端（本仓库根目录）
./start.sh

# 2. 安装插件到 DSH web profile
bash dsh-plugin/install.sh
```

install.sh 会把本目录作为 file: 依赖装进 `~/.dsh/profiles/web` 并把注册块追加到
该 profile 的 `cordis.patch.yml`。DSH host 热 watch patch 层，通常几秒内生效；
若侧边栏没有出现「菜单」按钮，重启 DSH web 后刷新 `http://127.0.0.1:3080`。

卸载：`bash dsh-plugin/uninstall.sh`。

## 使用

1. 点击侧边栏底部的 **菜单** 按钮 → 菜单中选 **Agent测评**，评测面板打开
   （菜单条里的 **设置** 会打开 DSH 原生设置面板）。
2. 确认「后端地址」（默认 `http://127.0.0.1:18080`）与「看板地址」
   （默认 `http://127.0.0.1:6173`），点「测试连接」。
3. 「DSH 会话录制」里点「导入并审计」，导入后自动展开该 run 的指标。
4. 「连接其他 Agent」卡片可以测试任何兼容 xuanjian-agent HTTP API 的远程
   agent 服务，两种模式：
   - **导入已有会话**：填服务地址 / 用户 ID / 会话 ID（Token 可选，且不写
     localStorage），导入后与 DSH 会话一样审计、展示指标；
   - **实时对话评测**：填服务地址 / 用户 ID / 测试消息，面板内实时显示
     SSE 进度（建会话 → 对话 → 落盘 → 审计），完成后自动展开指标。
5. 「评测结果」列表展示全部 run（DSH 来源带蓝色徽章，远程来源显示主机名），
   点「查看指标」，面板内直接展示：
   - 审计概览（消息/工具调用/缺失结果/最终回答字数等）
   - 审计发现（按 severity 着色）、阶段划分、工具调用明细表
   - 「展开轨迹」查看完整消息轨迹
   - DeepAgent 裁判：未裁判时可点「运行裁判」（调用 LLM），已裁判则展示
     总体结论、完成度得分和 13 个评测维度的状态/得分条
6. 每行的「外部看板」链接可跳到 agentloop 完整看板（图表、Markdown 导出等）。
7. Esc 或右上角 ✕ 关闭面板。

## 命令行等价用法

不装插件也可以用 CLI 达到同样效果：

```bash
# 列出本机 DSH 会话
python3 -m agentloop.cli list-dsh-sessions

# 导入某个会话为 run（随后可 render-run / judge-run）
python3 -m agentloop.cli import-dsh-session --session-id session-<uuid>
python3 -m agentloop.cli judge-run --run runs/<run-dir>
```

## 开发迭代

改 `lib/client.js` 后无需重启也不用刷新页面：DSH 的 client-hmr 会检测 bundle
哈希变化并在几秒内自动重载本插件。新增/删除插件注册仍需重启 web 服务。

## 文件

| 文件 | 作用 |
| --- | --- |
| `package.json` | 包清单，含 `dsh.client` 元数据与 `exports["./client"]` |
| `lib/client.js` | 客户端 bundle（`window.__ModuleLoader__.load` 格式，无构建步骤） |
| `lib/index.js` | Host 侧入口（空操作，Loader 要求存在） |
| `cordis.patch.yml` | Loader 注册片段 |
| `install.sh` / `uninstall.sh` | 一键安装 / 卸载 |
