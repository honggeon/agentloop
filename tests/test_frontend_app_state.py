from pathlib import Path


APP_VUE = Path("frontend/src/App.vue")
ROUTER = Path("frontend/src/router.ts")
API_TS = Path("frontend/src/api.ts")
VITE_CONFIG = Path("frontend/vite.config.ts")
BATCH_VIEW = Path("frontend/src/views/BatchView.vue")
RUN_VIEW = Path("frontend/src/views/RunView.vue")
LIVE_VIEW = Path("frontend/src/views/LiveEvalView.vue")


def test_batch_overview_is_home_route():
    source = ROUTER.read_text(encoding="utf-8")

    assert 'path: "/"' in source
    assert 'name: "batch"' in source
    assert "BatchView" in source
    assert 'path: "/runs/:runId"' in source
    assert 'name: "run"' in source


def test_batch_overview_does_not_auto_select_first_run():
    app_source = APP_VUE.read_text(encoding="utf-8")
    batch_source = BATCH_VIEW.read_text(encoding="utf-8")

    assert "selectRun(runs.value[0].name" not in app_source
    assert "selectRun(runs.value[0].name" not in batch_source
    assert 'onMounted(loadRuns)' in app_source


def test_sidebar_links_to_batch_overview_and_runs():
    source = APP_VUE.read_text(encoding="utf-8")

    assert "批量评测总览" in source
    assert 'to="/"' in source
    assert "goRun(run.name)" in source
    assert "route.name === 'batch'" in source
    assert 'name: "batch"' in source
    assert "runGroups" in source
    assert "server_host" in source
    assert "deleteRun" in source


def test_run_view_can_navigate_back_to_batch():
    source = RUN_VIEW.read_text(encoding="utf-8")

    assert "返回批量总览" in source
    assert "name: 'batch'" in source


def test_trajectory_view_route_and_entry_exist():
    trajectory_view = Path("frontend/src/views/TrajectoryView.vue")
    router_source = ROUTER.read_text(encoding="utf-8")
    run_source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert trajectory_view.exists()
    assert 'path: "/runs/:runId/trajectory"' in router_source
    assert 'name: "trajectory"' in router_source
    assert "TrajectoryView" in router_source
    assert "查看轨迹" in run_source
    assert "name: 'trajectory'" in run_source
    assert "TrajectoryMessage" in api_source
    assert "/api/runs/" in trajectory_view.read_text(encoding="utf-8")
    assert "/trajectory" in trajectory_view.read_text(encoding="utf-8")


def test_run_view_contains_runtime_health_dashboard_sections():
    source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "Agent Runtime Summary" in source
    assert "Agent Lifecycle Timeline" in source
    assert "Agent Health Score" in source
    assert "Runtime Error Analysis" in source
    assert "耗时构成" in source
    assert "RuntimeMetrics" in api_source


def test_run_view_can_export_markdown_report():
    source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "导出 Markdown" in source
    assert "exportMarkdown" in source
    assert "/export.md" in source
    assert "apiDownload" in api_source


def test_run_view_explains_charts_with_insights_and_tables():
    source = RUN_VIEW.read_text(encoding="utf-8")

    assert "图表怎么看" in source
    assert "图表用来看趋势，表格用来看具体数值" in source
    assert "Token 趋势" in source
    assert "耗时构成" in source
    assert "阶段耗时" in source
    assert "工具分布" in source
    assert "chart-insight" in source
    assert "chart-data-table" in source
    assert "tokenInsight" in source
    assert "toolDistributionRows" in source
    assert "sortMetricRows" in source
    assert "findingSeverityRows" in source


def test_run_view_shows_evaluation_dimensions_and_judge_reliability():
    source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "export interface JudgeDimension" in api_source
    assert "export interface JudgeReliability" in api_source
    assert "invalid_evidence" in api_source
    assert "评测维度" in source
    assert "Judge 可信度" in source
    assert "dimensionRows" in source
    assert "reliabilityRows" in source
    assert "未评估" in source
    assert "意图识别" in source


def test_run_view_can_select_the_judge_engine():
    source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")

    assert "export interface JudgeEngineInfo" in api_source
    assert "export interface JudgeEngineOptions" in api_source
    assert "export interface JudgeProvenance" in api_source
    assert "fetchJudgeEngines" in api_source
    assert "/api/judge/engines" in api_source
    assert "裁判引擎" in source
    assert "judgeEngineChoices" in source
    assert "judgeEngineLabel" in source
    assert "engine: judgeEngine.value" in source
    assert "apiPostJson" in source
    assert "judge?.judge?.engine" in source or "judge.judge?.engine" in source


def test_run_view_lists_and_compares_multiple_judge_reports():
    source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")

    assert "export interface JudgeReportSummary" in api_source
    assert "export interface JudgeReportList" in api_source
    assert "export interface JudgeComparisonReport" in api_source
    assert "judge_engine" in api_source
    assert "fetchJudgeReports" in api_source
    assert "fetchJudgeReport" in api_source
    assert "fetchJudgeComparison" in api_source
    assert "activateJudgeReport" in api_source
    assert "/api/runs/" in api_source
    assert "/judges" in api_source
    assert "/judges/compare" in api_source
    assert "/active" in api_source

    # 面板、对比表与分歧标注，且不自动选择更优裁判
    assert "多 Judge 对比" in source
    assert "judgeOverallRows" in source
    assert "judgeDimensionRows" in source
    assert "judgeDisagreementDimensions" in source
    assert "judgeDisagreementRows" in source
    assert "judgeView" in source
    assert "viewingJudgeEngine" in source
    assert "selectJudgeView" in source
    assert "activateJudgeView" in source
    assert "设为当前报告" in source
    assert "不自动选择更优裁判" in source
    assert "未评估" in source
    assert "unknown" in source



def test_live_eval_can_auto_continue_confirmations():
    source = LIVE_VIEW.read_text(encoding="utf-8")
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "auto_continue: true" in source
    assert "自动确认 / 继续" in source
    assert "auto_continue_limit" in source
    assert 'event === "auto_interaction"' in source
    assert "auto_continue?: boolean" in api_source


def test_live_eval_is_provider_aware_for_dsh_workbench():
    source = LIVE_VIEW.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")

    # provider 类型与 DSH SSE 契约集中在 api.ts
    assert "RuntimeProviderId" in api_source
    assert "export interface DshOptions" in api_source
    assert "export interface DshChatPayload" in api_source
    assert "export interface DshRuntimeEvent" in api_source
    assert "/api/dsh/options" in api_source
    assert "/api/dsh/chat" in api_source
    assert "fetchDshOptions" in api_source
    assert "streamDshChat" in api_source
    assert "cancelDshChat" in api_source

    # 只呈现后端批准的 profile / workspace 选项
    assert "provider-switch" in source
    assert "Local DSH" in source
    assert "selectProvider" in source
    assert "dshEnabled" in source
    assert "Profile（后端授权）" in source
    assert "工作目录（后端授权）" in source
    assert "工作目录（必须位于授权根目录内）" in source

    # DSH 模式只显示本地字段：远程连接项被隐藏
    assert """<template v-if="provider === 'remote'">""" in source
    assert "<template v-else>" in source
    assert "provider === 'remote' && (pendingClarify" in source

    # 停止 / 新建会话 / 继续会话
    assert "stopDshTurn" in source
    assert "newDshSession" in source
    assert "继续 DSH 会话" in source

    # 事件流渲染：thinking 默认折叠，工具调用与结果分行展示
    assert "DSH 对话流" in source
    assert "tool_call" in source
    assert "tool_result" in source
    assert "chat-collapsed" in source
    assert "思考内容默认折叠" in source
    assert "appendDshRuntimeEvent" in source

    # 区分已取消 / 失败 / 已导入 / 已评测
    assert '"cancelled"' in source
    assert '"cancelled";' in source
    assert "已取消" in source
    assert "已导入运行" in source
    assert "已评测" in source

    # 导入的 run 可跳转到审计与轨迹视图
    assert "openTrajectory" in source
    assert 'name: "trajectory"' in source
    assert "打开运行（审计）" in source

    # 通用 DSH 会话不启用 PPT 续跑启发式
    assert 'provider.value === "dsh") return buildDshDiagnosis()' in source


def test_live_eval_route_and_sidebar_entry_exist():
    router_source = ROUTER.read_text(encoding="utf-8")
    app_source = APP_VUE.read_text(encoding="utf-8")

    assert LIVE_VIEW.exists()
    assert 'path: "/live"' in router_source
    assert 'name: "live"' in router_source
    assert "LiveEvalView" in router_source
    assert "运行时评测" in app_source
    assert 'to="/live"' in app_source


def test_backend_down_shows_launcher_prompt():
    app_source = APP_VUE.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")
    vite_source = VITE_CONFIG.read_text(encoding="utf-8")

    assert "后端服务未启动" in app_source
    assert "一键启动服务" in app_source
    assert "backendDown" in app_source
    assert "startServices" in app_source
    assert "canLaunchLocally" in api_source
    assert "checkBackendHealth" in api_source
    assert "startLocalServices" in api_source
    assert "/api/health" in api_source
    assert "/__launcher/start" in api_source
    assert "/__launcher/start" in vite_source
    assert "./start.sh" in vite_source
