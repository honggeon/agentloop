from pathlib import Path


APP_VUE = Path("frontend/src/App.vue")
ROUTER = Path("frontend/src/router.ts")
API_TS = Path("frontend/src/api.ts")
VITE_CONFIG = Path("frontend/vite.config.ts")
BATCH_VIEW = Path("frontend/src/views/BatchView.vue")
RUN_VIEW = Path("frontend/src/views/RunView.vue")
LIVE_VIEW = Path("frontend/src/views/LiveEvalView.vue")
PLATFORM_VIEW = Path("frontend/src/views/PlatformView.vue")


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
    assert "<template v-else-if=\"provider === 'dsh'\">" in source
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
    assert 'provider.value === "dsh" || provider.value === "acp"' in source


def test_live_eval_exposes_the_acp_interactive_workbench():
    source = LIVE_VIEW.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")
    styles = Path("frontend/src/styles.css").read_text(encoding="utf-8")

    # ACP 契约集中在 api.ts：能力、会话、SSE prompt、一次性权限应答
    assert '"remote" | "dsh" | "acp"' in api_source
    assert "export interface DshAcpManifest" in api_source
    assert "export interface DshAcpCapabilities" in api_source
    assert "export interface DshAcpSession" in api_source
    assert "export interface DshAcpPermissionRequest" in api_source
    assert "export interface DshAcpCompletedPayload" in api_source
    assert "capabilities_route" in api_source
    assert "/api/dsh/acp/options" in api_source
    assert "/api/dsh/acp/sessions" in api_source
    assert "/api/dsh/acp/session/new" in api_source
    assert "/api/dsh/acp/session/resume" in api_source
    assert "/api/dsh/acp/prompt" in api_source
    assert "/api/dsh/acp/cancel" in api_source
    assert "/api/dsh/acp/permission/respond" in api_source
    assert "fetchDshAcpOptions" in api_source
    assert "fetchDshAcpSessions" in api_source
    assert "createDshAcpSession" in api_source
    assert "resumeDshAcpSession" in api_source
    assert "closeDshAcpSession" in api_source
    assert "streamDshAcpPrompt" in api_source
    assert "cancelDshAcpOperation" in api_source
    assert "respondDshAcpPermission" in api_source
    assert "acp: DshAcpManifest" in api_source

    # 第三个提供方入口与 ACP 工作台面板
    assert "Local DSH ACP" in source
    assert "selectProvider('acp')" in source
    assert "acpEnabled" in source
    assert "ACP 交互工作台" in source
    assert "acp-capabilities" in source
    assert "acp-session-list" in source
    assert "newAcpSession" in source
    assert "resumeAcpSession" in source
    assert "closeAcpSession" in source
    assert "startAcpTurn" in source
    assert "stopAcpTurn" in source
    assert "handleAcpEvent" in source
    assert "继续 ACP 会话" in source

    # 能力必须来自本次连接的协商结果，不得在前端写死
    assert "acpCapabilities" in source
    assert "protocol_version" in source
    assert "supports_images" in source

    # 权限只提供一次性 allow / reject，绝不出现 allow-always 或自动批准
    assert "answerAcpPermission" in source
    assert "allow-once" in source
    assert "reject-once" in source
    assert "不提供 allow-always，也不会自动批准" in source
    assert "'allow-always'" not in source
    assert '"allow-always"' not in source
    assert "allowAlways" not in source
    assert "allow_always: true" not in source
    assert "dshAcpPermission" in source
    assert "dshAcpPermissionBusy" in source
    # 后端返回的权限策略是只读展示，前端不自行拼选项
    assert "permission_policy" in api_source
    assert "allow_always" in api_source

    # completed 不等于成功：只有 run_imported 视为已发布 Auditrail run
    assert "dshAcpStopReason" in source
    assert "dshAcpPhase" in source
    assert "run_imported" in source
    assert "completed 只表示 ACP prompt 已结束" in source
    assert "回合已结束，但还没有发布运行" in source
    assert "已发布 Auditrail run" in source
    assert "settled" in source

    # ACP 面板样式
    assert ".acp-outcome" in styles
    assert ".acp-permission" in styles
    assert ".acp-session-list" in styles
    assert ".acp-capabilities" in styles


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


def test_platform_view_route_and_sidebar_entry_exist():
    router_source = ROUTER.read_text(encoding="utf-8")
    app_source = APP_VUE.read_text(encoding="utf-8")

    assert PLATFORM_VIEW.exists()
    assert 'path: "/platform"' in router_source
    assert 'name: "platform"' in router_source
    assert "PlatformView" in router_source
    assert "测试平台" in app_source
    assert 'to="/platform"' in app_source


def test_platform_view_uses_centralized_api_contracts():
    source = PLATFORM_VIEW.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")

    # api.ts 类型与请求函数集中定义
    assert "export interface AgentConfig" in api_source
    assert "export interface TestCase" in api_source
    assert "export interface RunStatusPayload" in api_source
    assert "export interface CreateRunResult" in api_source
    assert "export type PlatformRunStatus" in api_source
    assert "fetchAgents" in api_source
    assert "createAgent" in api_source
    assert "fetchTestCases" in api_source
    assert "createTestCase" in api_source
    assert "fetchTestCase" in api_source
    assert "createPlatformRun" in api_source
    assert "fetchRunStatus" in api_source
    assert "/api/agents" in api_source
    assert "/api/test-cases" in api_source
    assert '"/api/runs"' in api_source
    assert "/status" in api_source
    assert "Idempotency-Key" in api_source

    # 页面关键文案与元素
    assert "Agents" in source
    assert "Test Cases" in source
    assert "发起运行" in source
    assert "发起单次运行" in source
    assert "打开运行详情" in source
    assert "排队中" in source
    assert "运行中" in source
    assert "超时" in source
    assert "stale" in source
    assert "executeRun" not in source  # 视图不直接执行，只调 API


def test_run_view_shows_platform_run_status():
    source = RUN_VIEW.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")

    assert "platform-run-status" in source
    assert "runState" in source
    assert "runStatusLabel" in source
    assert "PLATFORM_RUN_STATUS_LABELS" in source
    assert "legacy_inferred" in source
    assert "RunStatusPayload" in api_source
    # 无证据维度 score 允许 null，不显示 0
    assert "score: number | null" in api_source
    assert 'row.score ?? "—"' in source


def test_platform_view_exposes_test_suites_and_suite_jobs():
    source = PLATFORM_VIEW.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")

    # api.ts 集中定义 Phase 1 契约：TestSuite CRUD + SuiteJob 批量运行
    assert "export interface TestSuite" in api_source
    assert "export interface SuiteJobAttempt" in api_source
    assert "export interface SuiteJobStats" in api_source
    assert "export interface SuiteJob" in api_source
    assert "export interface CreateSuiteJobResult" in api_source
    assert "export type SuiteJobStatus" in api_source
    assert "fetchTestSuites" in api_source
    assert "createTestSuite" in api_source
    assert "fetchTestSuite" in api_source
    assert "updateTestSuite" in api_source
    assert "deleteTestSuite" in api_source
    assert "launchSuiteJob" in api_source
    assert "fetchSuiteJobs" in api_source
    assert "fetchSuiteJob" in api_source
    assert "cancelSuiteJob" in api_source
    assert "/api/test-suites" in api_source
    assert "/api/suite-jobs" in api_source
    assert "/cancel" in api_source

    # 测试集 CRUD 面板
    assert "Test Suites" in source
    assert "保存测试集" in source
    assert "更新测试集" in source
    assert "toggleSuiteCase" in source
    assert "editSuite" in source
    assert "removeSuite" in source

    # 批量运行面板：顺序执行、单 case 失败不阻断、取消与 skipped 展示
    assert "批量运行" in source
    assert "发起批量运行" in source
    assert "取消批量运行" in source
    assert "SUITE_JOB_STATUS_LABELS" in source
    assert "activeJobAttempts" in source
    assert "launchSuite" in source
    assert "cancelActiveJob" in source
    assert "已跳过" in source
    assert "未评估" in source
    assert "cancel_requested" in source
    assert "stale" in source
    assert "打开运行详情" in source


def test_platform_view_exposes_phase11_batch_reliability():
    source = PLATFORM_VIEW.read_text(encoding="utf-8")
    api_source = API_TS.read_text(encoding="utf-8")

    # api.ts 集中定义 Phase 1.1 契约：重试 / 预算 / 限流 / 恢复 / 幂等
    assert "export interface SuiteJobBudget" in api_source
    assert "export interface SuiteJobBudgetUsed" in api_source
    assert "export interface SuiteJobLimits" in api_source
    assert "export interface LaunchSuiteJobPayload" in api_source
    assert "retry_index" in api_source
    assert "idempotent_replay" in api_source
    assert "max_retries" in api_source
    assert "budget_used" in api_source
    assert "recovered" in api_source
    assert "recoverStaleSuiteJobs" in api_source
    assert "/api/suite-jobs/recover" in api_source
    assert "Idempotency-Key" in api_source

    # 批量运行表单：重试次数、套件总超时、执行/成本预算、幂等键
    assert "失败重试次数" in source
    assert "套件总超时" in source
    assert "执行预算" in source
    assert "成本预算" in source
    assert "batchMaxRetries" in source
    assert "batchTimeoutSeconds" in source
    assert "batchBudgetAttempts" in source
    assert "batchBudgetTokens" in source
    assert "batchIdempotencyKey" in source

    # Job 展示：限流上限、预算用量、恢复与取消状态、重试徽标
    assert "并发上限" in source
    assert "预算" in source
    assert "已恢复" in source
    assert "恢复中断的批量任务" in source
    assert "recoverJobs" in source
    assert "重试 #" in source
    assert "stats.retries" in source
    assert "hasStaleJobs" in source
    assert "已请求取消" in source


AGENT_LIST_VIEW = Path("frontend/src/views/AgentListView.vue")
AGENT_DETAIL_VIEW = Path("frontend/src/views/AgentDetailView.vue")
AGENT_RUNS_VIEW = Path("frontend/src/views/AgentRunsView.vue")


def test_agent_workspace_routes_exist_with_name_and_title():
    source = ROUTER.read_text(encoding="utf-8")
    assert 'path: "/agents"' in source
    assert 'name: "agents"' in source
    assert 'meta: { title: "Agent 列表" }' in source
    assert 'path: "/agents/:agentId"' in source
    assert 'name: "agent-detail"' in source
    assert 'meta: { title: "Agent 详情" }' in source
    assert 'path: "/agents/:agentId/runs"' in source
    assert 'name: "agent-runs"' in source
    assert 'meta: { title: "Agent 运行记录" }' in source
    # 既有路由保持
    assert 'path: "/runs/:runId"' in source
    assert 'path: "/runs/:runId/trajectory"' in source
    assert 'path: "/platform"' in source


def test_agent_workspace_uses_centralized_api_contracts():
    source = API_TS.read_text(encoding="utf-8")
    for name in (
        "AgentOverviewRange",
        "AgentRunStatusFilter",
        "AgentScanError",
        "AgentRunStats",
        "AgentListItem",
        "DerivedAgentSummary",
        "AgentDirectoryPayload",
        "AgentHeader",
        "AgentDetailPayload",
        "AgentModelMetric",
        "AgentToolMetric",
        "AgentTraceDay",
        "AgentKpis",
        "AgentHealthMetric",
        "AgentHealth",
        "AgentRunRow",
        "AgentOverview",
        "AgentRunsPage",
    ):
        assert name in source, name
    for fn in ("fetchAgentDirectory", "fetchAgentDetail", "fetchAgentOverview", "fetchAgentRuns"):
        assert fn in source, fn
    assert '"/api/agents"' in source
    assert "/overview?range=" in source
    assert "/runs?" in source


def test_agent_workspace_sidebar_entry_exists():
    source = APP_VUE.read_text(encoding="utf-8")
    assert 'to="/agents"' in source
    assert "Agent 工作区" in source
    assert "agent-detail" in source
    assert "agent-runs" in source


def test_agent_list_view_states_and_derived_badge():
    source = AGENT_LIST_VIEW.read_text(encoding="utf-8")
    assert "来源于运行数据" in source
    assert "加载 Agent 列表中" in source
    assert "暂无 Agent" in source
    assert "局部数据" in source
    assert 'name: "agent-detail"' in source
    assert 'class="mono"' in source
    assert "aria-label" in source
    assert "@keydown.enter" in source
    assert "fetchAgentDirectory" in source
    assert "fetch(" not in source  # 组件不直接 fetch


def test_agent_detail_view_sections_and_semantics():
    source = AGENT_DETAIL_VIEW.read_text(encoding="utf-8")
    for section in ("Models", "Tools", "Traces per day", "Auditrail Health", "Recent Runs"):
        assert section in source, section
    # unknown/null/估算语义
    assert "未评估" in source
    assert '"—"' in source
    assert "估算" in source
    # 图表可访问性：数值摘要 + 表格
    assert "chart-insight" in source
    assert "chart-data-table" in source
    # 工具错误率与 Run 失败率口径区分
    assert "不是 run 失败率" in source
    # 跳转：Recent Run -> RunView -> 全部运行 -> AgentRunsView
    assert 'name: "run"' in source
    assert 'name: "agent-runs"' in source
    assert "查看全部运行" in source
    assert "第 {{ row.retry_index }} 次重试" in source
    assert "fetchAgentOverview" in source
    assert "fetch(" not in source


def test_agent_runs_view_filters_and_pagination():
    source = AGENT_RUNS_VIEW.read_text(encoding="utf-8")
    for label in ("近 7 天", "近 30 天", "全部", "全部状态", "已中断（stale）"):
        assert label in source, label
    assert "上一页" in source
    assert "下一页" in source
    assert "Judge overall" in source
    assert "Run 状态" in source
    assert "第 {{ row.retry_index }} 次重试" in source
    assert 'name: "run"' in source
    assert "fetchAgentRuns" in source
    assert "fetch(" not in source


# ---------------------------------------------------------------------------
# Phase 2：Agent 元数据、成本来源、SuiteJob 关联与运行索引提示
# ---------------------------------------------------------------------------


def test_phase2_api_contracts_exist():
    source = API_TS.read_text(encoding="utf-8")
    for name in (
        "RunIndexBlock",
        "CostSource",
        "CostBlock",
        "CostRecord",
        "CostSummary",
        "CostImportReport",
        "RunContextPayload",
        "SuiteJobRunsPayload",
        "SuiteJobAttemptPayload",
        "RunCostsPayload",
        "AgentCostsPayload",
        "RunIndexStatus",
    ):
        assert name in source, name
    for fn in (
        "updateAgent",
        "deleteAgent",
        "fetchRunContext",
        "fetchSuiteJobRuns",
        "fetchSuiteJobAttempt",
        "fetchRunCosts",
        "fetchAgentCosts",
        "importCosts",
        "fetchCostImport",
        "fetchRunIndexStatus",
        "rebuildRunIndex",
        "costSourceLabel",
    ):
        assert fn in source, fn
    for path in (
        "/context",
        "/api/costs/import",
        "/api/costs/imports/",
        "/api/admin/run-index/status",
        "/api/admin/run-index/rebuild",
        "/runs?${",
    ):
        assert path in source, path
    # 成本来源中文标签：观测/导入/估算/无数据/多来源
    for label in ("观测", "导入", "估算", "无数据", "多来源"):
        assert label in source, label


def test_agent_list_view_shows_metadata_and_index_state():
    source = AGENT_LIST_VIEW.read_text(encoding="utf-8")
    assert "创建时间" in source
    assert "环境" in source
    assert "索引不可用，当前使用扫描结果" in source
    assert "运行索引就绪" in source
    assert "fetch(" not in source


def test_agent_detail_view_phase2_semantics():
    source = AGENT_DETAIL_VIEW.read_text(encoding="utf-8")
    # 成本来源标注扩展：导入 / 多来源；估算必须显式标注
    assert "costSourceLabel" in source
    assert "多来源" in source
    # 索引新鲜度与 fallback 提示
    assert "索引不可用，当前使用扫描结果" in source
    # Agent 元数据与配置状态
    assert "创建时间" in source
    assert "missing_configuration" in source
    # SuiteJob 下钻链接
    assert "openSuiteJob" in source
    assert 'name: "platform"' in source
    assert "fetch(" not in source


def test_agent_runs_view_phase2_linkage_and_cost():
    source = AGENT_RUNS_VIEW.read_text(encoding="utf-8")
    assert "openSuiteJob" in source
    assert "最终尝试" in source
    assert "共 {{ row.attempt_count }} 次尝试" in source
    assert "formatCost" in source
    assert "索引不可用，当前使用扫描结果" in source
    assert "fetch(" not in source


def test_platform_view_agent_metadata_editing():
    source = PLATFORM_VIEW.read_text(encoding="utf-8")
    for field in ("environment", "description", "tags"):
        assert "agentForm.value.%s" % field in source or 'agentForm.%s' % field in source, field
    assert "editingAgentId" in source
    assert "updateAgent" in source
    assert "deleteAgent" in source
    assert "更新 Agent" in source
    assert "取消编辑" in source
    # SuiteJob 详情展示 Agent 归属；旧任务明确 unknown
    assert "agent_environment" in source
    # 从 Agent 工作区经 query 定位 SuiteJob
    assert "route.query.job" in source
    assert "最终尝试" in source
    assert "fetch(" not in source


# ---------------------------------------------------------------------------
# Phase 3：SuiteJob 独立详情页、外部证据展示与外部评测面板
# ---------------------------------------------------------------------------

SUITE_JOB_DETAIL_VIEW = Path("frontend/src/views/SuiteJobDetailView.vue")


def test_suite_job_detail_route_exists_with_name_and_title():
    source = ROUTER.read_text(encoding="utf-8")
    assert SUITE_JOB_DETAIL_VIEW.exists()
    assert 'path: "/suite-jobs/:jobId"' in source
    assert 'name: "suite-job-detail"' in source
    assert "SuiteJobDetailView" in source
    assert 'meta: { title: "SuiteJob 详情" }' in source
    # 旧入口 /platform?job= 保持不变
    assert 'path: "/platform"' in source
    assert 'name: "platform"' in source


def test_phase3_api_contracts_exist():
    source = API_TS.read_text(encoding="utf-8")
    for name in (
        "ExternalEvaluation",
        "ExternalTrace",
        "ExternalSummary",
        "IntegrationImport",
        "IntegrationImportResult",
        "PriceVersion",
        "RunIndexWatcherStatus",
        "SuiteJobDetail",
        "CaseExecutionDetail",
        "AttemptDetail",
        "RunExternalEvaluations",
        "RunExternalTraces",
        "AgentExternalEvaluations",
        "SuiteJobDetailSummary",
        "SuiteJobBudgetBlock",
        "SuiteJobExternalEvidence",
        "SuiteJobConcurrencySnapshot",
    ):
        assert name in source, name
    for fn in (
        "fetchSuiteJobDetail",
        "fetchRunExternalEvaluations",
        "fetchRunExternalTraces",
        "fetchAgentExternalEvaluations",
        "fetchIntegrationImports",
        "fetchRunIndexStatus",
    ):
        assert fn in source, fn
    for path in (
        "/detail",
        "/external-evaluations",
        "/external-traces",
        "/api/integrations/imports",
    ):
        assert path in source, path
    # 外部证据中文标签常量：置信度 / 关联方式 / watcher 状态
    assert "EXTERNAL_CONFIDENCE_LABELS" in source
    assert "EXTERNAL_LINK_METHOD_LABELS" in source
    assert "WATCHER_STATUS_LABELS" in source
    for label in ("精确关联", "强关联", "弱关联", "未关联", "已关闭", "运行中", "已降级", "已停止"):
        assert label in source, label
    # watcher 是 RunIndexStatus 的可选独立块，且预算块含进程内并发快照
    assert "watcher?: RunIndexWatcherStatus" in source
    assert "concurrency?: SuiteJobConcurrencySnapshot" in source


def test_suite_job_detail_view_sections_and_semantics():
    source = SUITE_JOB_DETAIL_VIEW.read_text(encoding="utf-8")
    # 页面分区：Header / 执行汇总 / 预算与限额 / Case 执行 / 外部证据
    assert "SuiteJob 详情" in source
    assert "执行汇总" in source
    assert "预算与限额" in source
    assert "Case 执行" in source
    assert "外部证据" in source
    assert "外部 Trace" in source
    assert "外部评测" in source
    # 外部证据与确定性审计明确分隔的说明文案与空态
    assert "不影响确定性审计与裁判结论" in source
    assert "未关联外部 Trace" in source
    # unknown 显示「未评估」，最终尝试徽标与历史 attempt 区分
    assert "未评估" in source
    assert "最终尝试" in source
    assert "历史尝试" in source
    # 并发活跃快照展示（进程内快照）
    assert "进程内快照" in source
    assert "global_active" in source
    assert "agent_active" in source
    # 下钻：Run / Agent / 返回平台（模板属性内使用单引号路由名）
    assert "name: 'run'" in source
    assert "name: 'agent-detail'" in source
    assert "name: 'platform'" in source
    assert "返回平台" in source
    assert "打开运行详情" in source
    # 状态标签与轮询
    assert "SUITE_JOB_STATUS_LABELS" in source
    assert "RUN_STATUS_LABELS" in source
    assert "FINAL_QUALITY_LABELS" in source
    assert "fetchSuiteJobDetail" in source
    assert "fetch(" not in source  # 组件不直接 fetch


def test_platform_view_links_to_suite_job_detail_page():
    source = PLATFORM_VIEW.read_text(encoding="utf-8")
    assert "suite-job-detail" in source
    assert "打开详情页" in source
    # ?job= 旧入口逻辑保持不变
    assert "route.query.job" in source


def test_agent_detail_view_shows_external_evaluations_panel():
    source = AGENT_DETAIL_VIEW.read_text(encoding="utf-8")
    assert "外部评测" in source
    assert "暂无外部评测证据" in source
    assert "外部 score 与内部 completion_score 相互独立" in source
    assert "不参与质量封顶" in source
    assert "external_summary" in source
    assert "EXTERNAL_CONFIDENCE_LABELS" in source
    assert "fetchAgentExternalEvaluations" in source
    assert "fetch(" not in source


# ---------------------------------------------------------------------------
# Phase 4：导入任务、promptfoo 草稿审核、审计日志与运行索引健康
# ---------------------------------------------------------------------------

IMPORT_LIST_VIEW = Path("frontend/src/views/ImportListView.vue")
IMPORT_DETAIL_VIEW = Path("frontend/src/views/ImportDetailView.vue")
PROMPTFOO_DRAFTS_VIEW = Path("frontend/src/views/PromptfooDraftsView.vue")
AUDIT_EVENTS_VIEW = Path("frontend/src/views/AuditEventsView.vue")


def test_phase4_routes_exist_with_name_and_title():
    source = ROUTER.read_text(encoding="utf-8")
    assert IMPORT_LIST_VIEW.exists()
    assert IMPORT_DETAIL_VIEW.exists()
    assert PROMPTFOO_DRAFTS_VIEW.exists()
    assert AUDIT_EVENTS_VIEW.exists()
    assert 'path: "/integrations/imports"' in source
    assert 'name: "integration-imports"' in source
    assert "ImportListView" in source
    assert 'meta: { title: "导入任务" }' in source
    assert 'path: "/integrations/imports/:importId"' in source
    assert 'name: "integration-import-detail"' in source
    assert "ImportDetailView" in source
    assert 'meta: { title: "导入详情" }' in source
    assert 'path: "/integrations/promptfoo/drafts"' in source
    assert 'name: "promptfoo-drafts"' in source
    assert "PromptfooDraftsView" in source
    assert 'meta: { title: "promptfoo 草稿审核" }' in source
    assert 'path: "/admin/audit-events"' in source
    assert 'name: "audit-events"' in source
    assert "AuditEventsView" in source
    assert 'meta: { title: "审计日志" }' in source
    # 既有路由保持不变
    assert 'path: "/suite-jobs/:jobId"' in source
    assert 'name: "suite-job-detail"' in source


def test_phase4_api_contracts_exist():
    source = API_TS.read_text(encoding="utf-8")
    for name in (
        "AuthContext",
        "ImportJobSummary",
        "ImportJob",
        "ImportJobStatus",
        "ImportJobPhase",
        "ImportCheckpoint",
        "ImportJobError",
        "PromptfooDraft",
        "PromptfooAssertion",
        "PromptfooProviderDeclaration",
        "AuditEvent",
        "AuditEventList",
        "ImportListPayload",
        "DraftListPayload",
    ):
        assert name in source, name
    for fn in (
        "fetchAuthContext",
        "fetchImportJobs",
        "fetchImportJob",
        "fetchImportJobStatus",
        "cancelImportJob",
        "retryImportJob",
        "resumeImportJob",
        "fetchPromptfooDrafts",
        "fetchPromptfooDraft",
        "approvePromptfooDraft",
        "rejectPromptfooDraft",
        "archivePromptfooDraft",
        "fetchAuditEvents",
        "fetchRunIndexHealth",
        "pauseRunIndex",
        "resumeRunIndex",
    ):
        assert fn in source, fn
    for path in (
        "/api/auth/context",
        "/api/integrations/imports",
        "/api/integrations/promptfoo/drafts",
        "/api/audit-events",
        "/api/admin/run-index/health",
        "/api/admin/run-index/pause",
        "/api/admin/run-index/resume",
    ):
        assert path in source, path
    # 中文状态标签常量
    assert "IMPORT_JOB_STATUS_LABELS" in source
    assert "IMPORT_JOB_PHASE_LABELS" in source
    assert "DRAFT_STATUS_LABELS" in source
    for label in ("排队中", "校验中", "已暂停", "部分完成", "已取消", "已失联", "归一化", "落盘", "已通过", "已拒绝", "已归档"):
        assert label in source, label
    # RunIndexWatcherStatus 的 Phase 4 可选字段
    assert "paused?: boolean" in source
    assert "failure_count?: number" in source
    assert "last_error?: string | null" in source
    assert "last_refresh_reason?: string | null" in source


def test_phase4_views_render_key_copy_without_direct_fetch():
    views = (IMPORT_LIST_VIEW, IMPORT_DETAIL_VIEW, PROMPTFOO_DRAFTS_VIEW, AUDIT_EVENTS_VIEW)
    for view in views:
        source = view.read_text(encoding="utf-8")
        assert "fetch(" not in source, view  # 组件不直接 fetch
        assert "fetchAuthContext" in source, view
        assert "本地开发模式" in source, view

    list_source = IMPORT_LIST_VIEW.read_text(encoding="utf-8")
    assert "导入任务" in list_source
    assert "进行中" in list_source
    assert "部分完成" in list_source
    assert "已失联" in list_source
    assert "上一页" in list_source
    assert "下一页" in list_source
    assert "integration-import-detail" in list_source
    assert "fetchImportJobs" in list_source

    detail_source = IMPORT_DETAIL_VIEW.read_text(encoding="utf-8")
    assert "导入详情" in detail_source
    assert "取消导入" in detail_source
    assert "从断点恢复" in detail_source
    assert "重试导入" in detail_source
    assert "checkpoint" in detail_source
    assert "失败原因" in detail_source
    assert "fetchImportJobStatus" in detail_source
    assert "cancelImportJob" in detail_source
    assert "resumeImportJob" in detail_source
    assert "retryImportJob" in detail_source

    drafts_source = PROMPTFOO_DRAFTS_VIEW.read_text(encoding="utf-8")
    assert "promptfoo 草稿审核" in drafts_source
    assert "仅声明，不执行" in drafts_source
    assert "声明了可执行内容，不能 approve" in drafts_source
    assert "未映射" in drafts_source
    assert "input_override" in drafts_source
    assert "approvePromptfooDraft" in drafts_source
    assert "rejectPromptfooDraft" in drafts_source
    assert "archivePromptfooDraft" in drafts_source
    assert "DRAFT_STATUS_LABELS" in drafts_source

    audit_source = AUDIT_EVENTS_VIEW.read_text(encoding="utf-8")
    assert "审计日志" in audit_source
    assert "本地开发模式：认证已禁用" in audit_source
    assert "append-only 本地审计日志" in audit_source
    assert "resource_type" in audit_source
    assert "fetchAuditEvents" in audit_source
    assert "fetchRunIndexHealth" in audit_source
    assert "pauseRunIndex" in audit_source
    assert "resumeRunIndex" in audit_source


def test_phase4_sidebar_entries_exist():
    source = APP_VUE.read_text(encoding="utf-8")
    assert 'to="/integrations/imports"' in source
    assert "导入任务" in source
    assert 'to="/integrations/promptfoo/drafts"' in source
    assert "草稿审核" in source
    assert 'to="/admin/audit-events"' in source
    assert "审计日志" in source


# ---------------------------------------------------------------------------
# Phase 5：前端生产化 —— 登录/无权限页、readiness 横幅、平台管理页、导入暂停
# ---------------------------------------------------------------------------

LOGIN_VIEW = Path("frontend/src/views/LoginView.vue")
FORBIDDEN_VIEW = Path("frontend/src/views/ForbiddenView.vue")
ADMIN_HEALTH_VIEW = Path("frontend/src/views/AdminHealthView.vue")
ADMIN_WORKERS_VIEW = Path("frontend/src/views/AdminWorkersView.vue")
ADMIN_MIGRATIONS_VIEW = Path("frontend/src/views/AdminMigrationsView.vue")
ADMIN_BACKUPS_VIEW = Path("frontend/src/views/AdminBackupsView.vue")
ADMIN_USERS_VIEW = Path("frontend/src/views/AdminUsersView.vue")


def test_phase5_routes_exist_with_name_and_title():
    source = ROUTER.read_text(encoding="utf-8")
    assert 'path: "/login"' in source
    assert 'name: "login"' in source
    assert 'meta: { title: "登录" }' in source
    assert 'path: "/login/callback"' in source
    assert 'name: "login-callback"' in source
    assert "LoginView" in source
    assert 'path: "/403"' in source
    assert 'name: "forbidden"' in source
    assert 'meta: { title: "无权限" }' in source
    assert 'path: "/admin/health"' in source
    assert 'name: "admin-health"' in source
    assert 'meta: { title: "平台健康" }' in source
    assert 'path: "/admin/workers"' in source
    assert 'name: "admin-workers"' in source
    assert 'meta: { title: "Worker 状态" }' in source
    assert 'path: "/admin/migrations"' in source
    assert 'name: "admin-migrations"' in source
    assert 'meta: { title: "数据库迁移" }' in source
    assert 'path: "/admin/backups"' in source
    assert 'name: "admin-backups"' in source
    assert 'meta: { title: "备份" }' in source
    assert 'path: "/admin/users"' in source
    assert 'name: "admin-users"' in source
    assert 'meta: { title: "用户管理" }' in source
    # catch-all 重定向与既有路由保持不变
    assert 'path: "/:pathMatch(.*)*"' in source
    assert 'redirect: "/"' in source
    assert 'path: "/admin/audit-events"' in source


def test_phase5_auth_token_and_response_handling_in_api():
    source = API_TS.read_text(encoding="utf-8")
    # token 只存 sessionStorage，key 固定
    assert '"auditrail.auth_token"' in source
    assert "sessionStorage" in source
    assert "getAuthToken" in source
    assert "setAuthToken" in source
    assert "clearAuthToken" in source
    assert "export function getAuthToken" in source
    # 请求注入 Authorization: Bearer
    assert "Authorization: `Bearer ${token}`" in source
    # 统一 401 / 403 处理：清 token 跳登录（带 next），403 跳无权限页
    assert "handleAuthRedirect" in source
    assert "/login?next=" in source
    assert '"/403"' in source
    # AuthContext 扩展为 Phase 5 完整形状
    for field in ("capabilities", "authenticated", "login_url", "logout_url", "production_ready"):
        assert field in source, field
    # readiness 与平台管理契约
    for name in (
        "ReadinessReport",
        "ReadinessCheck",
        "ReadinessOperational",
        "AdminWorkersPayload",
        "AdminMigrationsState",
        "AdminBackupsPayload",
        "AdminUsersPayload",
        "AdminUser",
    ):
        assert name in source, name
    for fn in (
        "fetchAuthContext",
        "logout",
        "fetchReadiness",
        "fetchAdminHealth",
        "fetchAdminWorkers",
        "fetchAdminMigrations",
        "applyMigrations",
        "fetchAdminBackups",
        "createAdminBackup",
        "fetchAdminUsers",
        "updateUserRoles",
        "pauseImport",
        "pauseCostImport",
        "resumeCostImport",
        "retryCostImport",
        "cancelCostImport",
        "fetchCostImport",
    ):
        assert fn in source, fn
    for path in (
        "/api/auth/context",
        "/api/auth/logout",
        "/api/health/ready",
        "/api/admin/health",
        "/api/admin/workers",
        "/api/admin/migrations",
        "/api/admin/backups",
        "/api/admin/users",
        "/pause",
        "/api/costs/imports/",
    ):
        assert path in source, path
    # not_ready 时 503 的 body 也照常解析
    assert "response.status === 503" in source
    # ImportJob 状态机新增字段
    for field in ("lease_version", "attempt_count", "max_attempts", "next_retry_at", "pause_requested", "queue_degraded"):
        assert field in source, field


def test_phase5_login_and_forbidden_views():
    login_source = LOGIN_VIEW.read_text(encoding="utf-8")
    forbidden_source = FORBIDDEN_VIEW.read_text(encoding="utf-8")

    assert "登录" in login_source
    assert "fetchAuthContext" in login_source
    assert "setAuthToken" in login_source
    assert "SSO 登录" in login_source
    assert "login_url" in login_source
    assert "进入看板" in login_source
    assert "本地模式无需登录" in login_source
    assert "仅开发调试" in login_source
    # OIDC 回调 token 在 URL fragment 中解析
    assert "window.location.hash" in login_source
    assert "token" in login_source
    # next 回跳目标页
    assert "next" in login_source
    assert "fetch(" not in login_source

    assert "无权限" in forbidden_source
    assert "当前角色" in forbidden_source
    assert "返回首页" in forbidden_source
    assert 'name: \'batch\'' in forbidden_source or 'name: "batch"' in forbidden_source
    assert "fetch(" not in forbidden_source


def test_phase5_admin_views_render_key_copy_without_direct_fetch():
    views = (ADMIN_HEALTH_VIEW, ADMIN_WORKERS_VIEW, ADMIN_MIGRATIONS_VIEW, ADMIN_BACKUPS_VIEW, ADMIN_USERS_VIEW)
    for view in views:
        source = view.read_text(encoding="utf-8")
        assert "fetch(" not in source, view  # 组件不直接 fetch

    health_source = ADMIN_HEALTH_VIEW.read_text(encoding="utf-8")
    assert "平台健康" in health_source
    # not_ready（503）整页红色横幅，而不是空数据
    assert "平台不可用" in health_source
    assert "not_ready" in health_source
    assert "degraded" in health_source
    assert "依赖检查" in health_source
    assert "deployment_mode" in health_source
    assert "storage_mode" in health_source
    assert "queue_mode" in health_source
    assert "auth_mode" in health_source
    assert "stale_import_jobs" in health_source
    assert "queue_degraded_jobs" in health_source
    assert "index_watcher" in health_source
    assert "run_index" in health_source
    assert "recent_import_failures" in health_source
    assert "fetchReadiness" in health_source

    workers_source = ADMIN_WORKERS_VIEW.read_text(encoding="utf-8")
    assert "Worker 状态" in workers_source
    assert "queue_mode" in workers_source
    assert "queue_reachable" in workers_source
    assert "embedded_worker" in workers_source
    assert "registry_available" in workers_source
    assert "当前为 file 存储模式，无跨进程 worker 注册表" in workers_source
    assert "seconds_since_heartbeat" in workers_source
    assert "fetchAdminWorkers" in workers_source

    migrations_source = ADMIN_MIGRATIONS_VIEW.read_text(encoding="utf-8")
    assert "数据库迁移" in migrations_source
    assert "current_version" in migrations_source
    assert "schema_version_target" in migrations_source
    assert "pending" in migrations_source
    assert "applied_now" in migrations_source
    assert "执行迁移" in migrations_source
    assert "window.confirm" in migrations_source
    assert "applyMigrations" in migrations_source
    assert "fetchAdminMigrations" in migrations_source

    backups_source = ADMIN_BACKUPS_VIEW.read_text(encoding="utf-8")
    assert "备份" in backups_source
    assert "立即备份" in backups_source
    assert "backup_root_configured" in backups_source
    assert "contains_secrets" in backups_source
    assert "runs/ 证据" in backups_source
    assert "createAdminBackup" in backups_source
    assert "fetchAdminBackups" in backups_source

    users_source = ADMIN_USERS_VIEW.read_text(encoding="utf-8")
    assert "用户管理" in users_source
    assert "users:admin" in users_source
    assert "owner" in users_source
    assert "updateUserRoles" in users_source
    assert "fetchAdminUsers" in users_source
    assert "last_seen_at" in users_source


def test_phase5_app_shell_shows_identity_banners_and_admin_nav():
    source = APP_VUE.read_text(encoding="utf-8")
    # 侧栏顶部当前用户 + 角色徽标
    assert "fetchAuthContext" in source
    assert "currentUserLabel" in source
    assert "currentRole" in source
    assert "display_name" in source
    # disabled 模式横幅
    assert "本地开发：认证已禁用" in source
    # readiness 降级 / 不可用横幅
    assert "fetchReadiness" in source
    assert "degraded_reasons" in source
    assert "降级" in source
    assert "平台不可用" in source
    # oidc 登出
    assert "logout" in source
    assert "clearAuthToken" in source
    assert "登出" in source
    # 平台管理导航入口
    assert 'to="/admin/health"' in source
    assert "平台健康" in source
    assert 'to="/admin/workers"' in source
    assert 'to="/admin/migrations"' in source
    assert "迁移" in source
    assert 'to="/admin/backups"' in source
    assert "备份" in source
    assert 'to="/admin/users"' in source
    assert "用户" in source


def test_phase5_import_views_pause_and_backoff_polling():
    list_source = IMPORT_LIST_VIEW.read_text(encoding="utf-8")
    detail_source = IMPORT_DETAIL_VIEW.read_text(encoding="utf-8")

    # 列表：paused 徽标（进行中说明）与逐行「暂停」按钮（operator+）
    assert "已暂停" in list_source
    assert "pauseImport" in list_source
    assert "暂停" in list_source
    assert "canPauseJob" in list_source
    assert "permissions" in list_source

    # 详情：暂停按钮、成本导入端点选择、新增状态字段展示
    assert "暂停导入" in detail_source
    assert "pauseImport" in detail_source
    assert "pauseCostImport" in detail_source
    assert "resumeCostImport" in detail_source
    assert "retryCostImport" in detail_source
    assert "cancelCostImport" in detail_source
    assert "fetchCostImport" in detail_source
    assert "cost_records" in detail_source
    assert "attempt_count" in detail_source
    assert "max_attempts" in detail_source
    assert "lease_version" in detail_source
    assert "next_retry_at" in detail_source
    assert "queue_degraded" in detail_source
    assert "pause_requested" in detail_source

    # 轮询降频：页面不可见暂停、连续失败 3 次指数退避、最长 30s
    assert "document.hidden" in detail_source
    assert "visibilitychange" in detail_source
    assert "pollFailures" in detail_source
    assert "30000" in detail_source
    assert "fetch(" not in detail_source
