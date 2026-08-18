# dsh-agentloop · DSH 上的 Agentloop 评测插件

把 [agentloop](https://github.com/)（本仓库）接入 DeepSeek Harness：在 DSH Web
「设置」里新增 **Agent 评测** 页面，列出本机 DSH 录制的 agent 会话，一键导入
agentloop 做**确定性工具审计**，并跳转 agentloop 看板查看轨迹 / 裁判报告。

## 工作原理

```
~/.dsh/sessions/<workspace>/<session>/session.jsonl.zstd   (DSH 落盘的事件流)
        │  agentloop.dsh_source（zstd 解压 + 事件 → 消息映射）
        ▼
runs/<run_id>/  (session.json / trajectory.jsonl / summary.json / audit.json …)
        ▲
agentloop FastAPI 后端  GET /api/dsh/sessions · POST /api/dsh/import
        ▲  HTTP（本插件 client bundle 直接 fetch）
DSH 设置页「Agent 评测」（本插件，纯客户端，Host 侧为空操作）
```

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
若设置页没有出现「Agent 评测」，重启 DSH web 后刷新 `http://127.0.0.1:3080`。

卸载：`bash dsh-plugin/uninstall.sh`。

## 使用

1. 打开 DSH Web → 设置 → **Agent 评测**。
2. 确认「后端地址」（默认 `http://127.0.0.1:18080`）与「看板地址」
   （默认 `http://127.0.0.1:6173`），点「测试连接」。
3. 会话列表里点「导入并审计」，完成后在「最近导入」里打开看板报告或轨迹。

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
