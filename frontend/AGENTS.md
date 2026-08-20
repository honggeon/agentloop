# frontend/ — Vue 看板

本目录是评测看板 UI。后端 API 在 `backend/`，run 文件契约和维度 key 在 `agentloop/`。跨层总规则见仓库根 `AGENTS.md`。

## 技术栈

Vue 3 + TypeScript + Vite + Vue Router + ECharts / `vue-echarts`。页面和布局使用 `<script setup lang="ts">` 与 Composition API。

当前脚本（`package.json`）：

- `npm run dev`：Vite
- `npm run build`：`vue-tsc --noEmit && vite build`
- `npm run preview`：Vite preview

没有独立的 eslint / prettier / vitest 命令，不要假设它们存在。`node_modules/` 被 gitignore；缺依赖且需联网时先确认再 `npm install`。

## 目录约定

- `src/api.ts`：API 类型、`apiGet` / `apiPost` / `apiPostJson` / `apiDelete` / `apiDownload`、SSE 解析（`streamPostJson`）。组件内不要散落 `fetch`。
- `src/router.ts`：命名路由。现有：`batch`（`/`）、`run`（`/runs/:runId`）、`live`（`/live`）。新页面必须有 `name` 和 `meta.title`；`afterEach` 负责 `document.title`。
- `src/views/`：`BatchView`、`RunView`、`LiveEvalView`。
- `src/styles.css`：全局样式。`src/utils.ts`：数字/百分比格式化。
- `src/App.vue`：侧栏、运行列表、删除、向子视图 `provide` runs。

新增或修改后端返回字段时：先改 `api.ts` 类型，再改视图。

## UI 与契约

- 界面文案保持中文。
- 维度 `unknown` 显示为「未评估」，不要当成失败或 0 分。`RunView.vue` 可用本地 `DIMENSION_LABELS`，但 key 必须与 `agentloop.agents.schemas.DIMENSION_DEFINITIONS` 一致。
- 环境变量只用 `import.meta.env.VITE_API_BASE_URL`。`vite.config.ts` 的 `envDir: ".."` 复用仓库根 `.env`，不要再引入一套前端 env 文件。

## 验证

前端改动后运行：

```bash
cd frontend && npm run build
```

该命令包含类型检查。改路由、关键文案、面板名、类型名或状态展示时，同步更新 `tests/test_frontend_app_state.py`（它读源码做字符串断言）。

## 禁止

- 不要在视图里直接请求后端或解析 SSE；走 `api.ts`。
- 不要在前端重写 audit / judge schema / Markdown 渲染规则。
- 不要提交 `dist/` 或 `node_modules/`。
- 不要把一次性计划写进本文件。
