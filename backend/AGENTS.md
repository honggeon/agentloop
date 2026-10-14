# backend/ — FastAPI 装配层

本目录只做 HTTP 装配、输入校验、错误映射、SSE 输出，以及复用 `auditrail` 核心函数。采集、审计、裁判 schema 和 Markdown 渲染规则在 `auditrail/`，不要在这里重写。跨层总规则见仓库根 `AGENTS.md`。

## 职责

- `app/main.py`：创建 app、CORS、挂载路由、`GET /api/health`（返回 `{"status": "ok"}`）。不要在这里堆业务逻辑。
- `app/config.py`：读项目根 `.env`。`get_settings()` 提供 `runs_dir`、`backend_host`/`backend_port`、`cors_origins`。
- `app/routes/runs.py`：run 列表、批量摘要、导入 session、删除、详情、audit/judge/charts、runtime-metrics、Markdown 导出。
- `app/routes/judge.py`：触发 DeepAgent 裁判并写回 run 目录。
- `app/routes/live.py`：live 评测。用 `StreamingResponse` 输出 SSE。

API 前缀统一为 `/api`。环境变量集中在 `config.py`，不要在路由里直接 `os.getenv` 一套新配置。

## 路径安全

按 `run_id` 访问 `runs/<run_id>/` 必须走 `backend.app.config.resolve_run_path`（或同等校验）：

- 拒绝 `/`、`\`、空字符串、`.`、`..`。
- resolve 后必须落在 `settings.runs_dir` 之内。
- 不存在则 `FileNotFoundError` → HTTP 404；非法 id → HTTP 400。

禁止把用户传入的 `run_id` 直接拼到路径上。不要把 `auditrail.agents.tools._resolve_run_dir`（裁判工具用的宽松解析）用到 HTTP 入口。

## 复用与错误映射

路由层复用现有函数，不要复制实现：

- 写 run 产物：`auditrail.cli._write_run_outputs`
- 拉远程 session / live chat：`auditrail.client.AgentClient`
- 读 run / 图表 / 写裁判：`auditrail.agents.tools`（`list_runs`、`load_run_summary`、`load_audit`、`build_chart_data`、`write_judge_report`）
- 裁判归一化与维度/可信度 Markdown：`auditrail.agents.schemas`（`resolve_judge_report`、`render_dimensions_markdown`、`render_reliability_markdown`）
- judge prompt：`auditrail.runtime.build_agent_judge_prompt`

外部调用失败要显式变成 HTTP 错误，不要静默吞错。当前约定：导入远程 session 失败 → 502；缺 DeepAgent 运行时 → 503；其它裁判异常 → 500。

`runs.py` 里的批量摘要、runtime-metrics、export.md 是看板聚合，可以留在路由层；其中维度/可信度段落必须调用 schema 渲染函数，不要手写另一套 judge Markdown。

## Live SSE

- `POST /api/live/evaluate`、`/live/evaluate-with-files`、`/live/message/respond` 返回 `StreamingResponse`，`media_type="text/event-stream"`。
- 流式业务错误通过 `event: error` 返回（含 `detail`，必要时含 `stage` / `session_id`），不要改成普通 JSON 500 中断半截流。
- 校验失败（缺 `base_url` / `user_id` / `message` 等）同样用 `event: error`。

## 测试

- 用 `fastapi.testclient.TestClient`，见 `tests/test_backend_routes.py`、`tests/test_backend_config.py`。
- live SSE 用 monkeypatch 假 `AgentClient` 和 `Settings`，见 `tests/test_backend_live.py`。不要打真实 agent 服务。
- 夹具用 `tmp_path` 下手写 run 目录；不要硬编码真实 `runs/2026...`。

## 禁止

- 不要从核心包反向依赖本目录；也不要把确定性审计/轨迹归一化搬进 `backend/`。
- 不要引入未在 `pyproject.toml` 声明的 Web 框架或 ORM。当前后端就是 FastAPI + 文件系统上的 run 产物。
- 不要把一次性计划写进本文件。
