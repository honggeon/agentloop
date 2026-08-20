# 轨迹视图（Trajectory View）设计与结论

日期：2026-08-17

## 目标

在看板中新增「轨迹」时间线页面，展示黑盒采集到的归一化消息轨迹（参照 DeepSeek Harness 的会话轨迹样式），补齐现有 RunView 只展示指标、不展示原始对话与工具调用轨迹的缺口。

## 数据契约

数据源：`runs/<run_id>/trajectory.jsonl`，每行一条消息，由 `agentloop.trajectory.normalize_message` 生成。

- 必含字段：`index`、`role`、`content`、`kind`，以及 `message_id` / `message_uuid` / `session_id` / `timestamp`。
- 可选字段：`tool_call_id`、`tool_name`、`tool_calls`、`finish_reason`、`reasoning`、`reasoning_content`。
- `role` ∈ `{user, assistant, tool}`；`kind` ∈ `{user_message, assistant_message, assistant_tool_request, tool_result}`。
- `tool_calls` 为数组，元素含 `id` / `call_id` 与 `function.{name, arguments}`；`arguments` 是 JSON 字符串。
- `timestamp` 通常为毫秒时间戳；工具结果 `content` 多为 JSON 字符串。

## 实现要点

后端（`backend/app/routes/runs.py`）：

- 新增 `GET /api/runs/{run_id}/trajectory`，返回 `{run_id, messages}`。
- 复用已有的 `_read_jsonl` 读文件；路径安全走 `_run_path_or_404` → `resolve_run_path`。
- 纯新增端点，不改变任何旧字段，向后兼容。

前端：

- `api.ts` 新增 `TrajectoryMessage` / `TrajectoryToolCall` / `TrajectoryPayload` 类型。
- 新增 `views/TrajectoryView.vue`：垂直时间线 + 角色着色（用户蓝 / 助手绿 / 工具橙）；助手「思考过程」默认折叠可展开；工具调用参数与工具结果 JSON 自动格式化；顶部按角色筛选（全部 / 用户 / 助手 / 工具）。
- 路由 `/runs/:runId/trajectory`（`name: "trajectory"`，`meta.title: "轨迹"`）；`RunView.vue` 头部新增「查看轨迹」入口按钮。

测试：

- `tests/test_backend_routes.py`：轨迹端点（有消息 + 缺文件返回空数组）两条。
- `tests/test_frontend_app_state.py`：新增视图 / 路由 / 类型 / 入口的源码断言。

## 关键结论（重启后注意）

1. **分层边界**：轨迹页是只读展示，不重写 audit / judge / schema / Markdown 渲染规则；后端只加路由并复用 `_read_jsonl`，未在 `backend/` 里搬入归一化逻辑。
2. **验证命令**：本机系统 `python3` 是 3.9（无 fastapi），`uv` 因 `~/.cache/uv` 无写权限初始化失败。正确做法是用仓库自带 `.venv`：
   - `.venv/bin/python -m pytest -q`（Python 3.14 + fastapi 0.141）→ 本次全量 111 passed。
   - `cd frontend && npm run build`（node 22 + npm 10，`node_modules` 已存在）→ 通过。
3. 前端构建的「chunk > 500 kB」提示是既有问题，与本次改动无关。
4. 既有无关改动（未触碰、未提交）：`docs/superpowers/plans/2026-08-15-agent-judge-reliability-dimensions.md` 被他人修改；仓库内多个层级的 `AGENTS.md` 为未跟踪文件。
5. 若要调整轨迹展示默认值（思考过程默认展开、工具结果默认折叠等），改 `TrajectoryView.vue` 的 `expandedReasoning` 初始值与对应 CSS max-height，不影响数据契约。
