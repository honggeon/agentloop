<script setup lang="ts">
import { computed, inject, onMounted, ref, watch, type Ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import VChart from "vue-echarts";
import { use } from "echarts/core";
import { BarChart, LineChart, PieChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import {
  apiDownload,
  apiGet,
  apiPostJson,
  fetchJudgeEngines,
  type Finding,
  type JudgeEngineInfo,
  type JudgeReport,
  type LlmCallDetail,
  type RunItem,
  type RuntimeMetrics,
  type RuntimeTrends
} from "../api";

use([BarChart, LineChart, PieChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const route = useRoute();
const router = useRouter();
const reloadRuns = inject<() => Promise<void>>("reloadRuns")!;
const deleteRunById = inject<(runId: string, confirm?: boolean) => Promise<boolean>>("deleteRunById")!;
const runs = inject<Ref<RunItem[]>>("runs")!;

const summary = ref<Record<string, unknown> | null>(null);
const audit = ref<Record<string, any> | null>(null);
const charts = ref<Record<string, any> | null>(null);
const judge = ref<JudgeReport | null>(null);
const runtime = ref<RuntimeMetrics | null>(null);
const runtimeTrends = ref<RuntimeTrends | null>(null);
const loading = ref(false);
const judging = ref(false);
const deleting = ref(false);
const exporting = ref(false);
const error = ref("");
const selectedTraceNode = ref("");
const selectedLlmCallId = ref("");
const expandedLlmCallIds = ref<string[]>([]);
const judgeEngines = ref<JudgeEngineInfo[]>([]);
const judgeEngine = ref("deepagent");

// 后端授权清单决定可选裁判引擎；DSH 裁判未配置时禁用，前端不自行猜测。
const judgeEngineChoices = computed(() => {
  const engines = judgeEngines.value.filter((item) => item.id === "deepagent" || item.available);
  return engines.length ? engines : [{ id: "deepagent", label: "DeepAgent 裁判", available: true, disabled_reason: null }];
});
const judgeEngineLabel = computed(
  () => judgeEngineChoices.value.find((item) => item.id === judgeEngine.value)?.label || "裁判"
);

const runId = computed(() => String(route.params.runId || ""));
const selectedMeta = computed(() => runs.value.find((item) => item.name === runId.value));
const serverHost = computed(() => {
  const fromMeta = selectedMeta.value?.server_host;
  if (fromMeta) return fromMeta;
  const fromSummary = summary.value?.server_host;
  return typeof fromSummary === "string" && fromSummary ? fromSummary : "未知来源";
});

const findings = computed<Finding[]>(() => {
  const agentFindings = judge.value?.findings || [];
  if (agentFindings.length) return agentFindings;
  return (audit.value?.findings || []).map((item: any) => ({
    severity: item.severity,
    category: item.code,
    title: item.code,
    message: item.message
  }));
});

const runtimeSummaryCards = computed(() => {
  const metrics = runtime.value;
  return [
    {
      label: "Status",
      value: statusLabel(metrics?.status || "running"),
      status: metrics?.status || "running",
      detail: `trace ${metrics?.traceId || runId.value || "-"}`
    },
    { label: "Duration", value: formatDuration(metrics?.duration), detail: `${metrics?.startTime || "-"} → ${metrics?.endTime || "-"}` },
    { label: "Agent Steps", value: formatNumber(metrics?.executionMetrics.total_steps), detail: "messages and runtime steps" },
    { label: "LLM Calls", value: formatNumber(metrics?.llmMetrics.calls), detail: `${formatDuration(metrics?.llmMetrics.averageLatency)} avg latency` },
    { label: "Tool Calls", value: formatNumber(metrics?.toolMetrics.calls), detail: `${metrics?.toolMetrics.success || 0} success / ${metrics?.toolMetrics.failed || 0} failed` },
    { label: "Total Tokens", value: formatNumber(metrics?.llmMetrics.totalTokens), detail: `${formatNumber(metrics?.llmMetrics.inputTokens)} in / ${formatNumber(metrics?.llmMetrics.outputTokens)} out` },
    { label: "Estimated Cost", value: formatCost(metrics?.costMetrics.totalCost), detail: "token-cost estimate" }
  ];
});

const llmSummaryCards = computed(() => {
  const summary = runtime.value?.llmDashboard?.summary;
  return [
    { label: "LLM Calls", value: formatNumber(summary?.calls), detail: "总调用次数" },
    { label: "Models", value: summary?.models?.join(" / ") || "-", detail: "使用模型" },
    { label: "Input Tokens", value: formatNumber(summary?.inputTokens), detail: "Prompt token" },
    { label: "Output Tokens", value: formatNumber(summary?.outputTokens), detail: "Completion token" },
    { label: "Average Latency", value: formatDuration(summary?.averageLatency), detail: "平均响应时间" },
    { label: "Max Latency", value: formatDuration(summary?.maxLatency), detail: "最大耗时" },
    { label: "Estimated Cost", value: formatCost(summary?.estimatedCost), detail: "预计成本" },
    { label: "Error Rate", value: formatPercent(summary?.errorRate), detail: "失败调用占比" }
  ];
});

const selectedLlmCall = computed<LlmCallDetail | null>(() => {
  const calls = runtime.value?.llmDashboard?.calls || [];
  return calls.find((call) => call.callId === selectedLlmCallId.value) || calls[0] || null;
});

const executionCards = computed(() => {
  const metrics = runtime.value?.executionMetrics;
  return [
    { label: "总步骤", value: formatNumber(metrics?.total_steps) },
    { label: "成功", value: formatNumber(metrics?.completed_steps) },
    { label: "失败", value: formatNumber(metrics?.failed_steps) },
    { label: "重试", value: formatNumber(metrics?.retry_count) },
    { label: "超时", value: formatNumber(metrics?.timeout_count) }
  ];
});

const healthParts = computed(() => {
  const health = runtime.value?.healthScore;
  return [
    { label: "任务完成", value: health?.taskSuccess || 0 },
    { label: "工具稳定", value: health?.toolStability || 0 },
    { label: "性能", value: health?.performance || 0 },
    { label: "成本", value: health?.costEfficiency || 0 }
  ];
});

const tokenRows = computed(() => {
  const analytics = runtime.value?.llmDashboard?.tokenAnalytics;
  return (analytics?.totalTrend || []).map((item, index) => ({
    label: `#${index + 1} ${item.callId}`,
    input: analytics?.inputTrend?.[index]?.value || 0,
    output: analytics?.outputTrend?.[index]?.value || 0,
    total: item.value
  }));
});

const performanceRows = computed(() =>
  (runtime.value?.performanceBreakdown || []).map((item) => ({
    label: item.label,
    value: formatDuration(item.value),
    percent: `${item.percent}%`
  }))
);

const lifecycleRows = computed(() =>
  (runtime.value?.lifecycleStages || []).map((stage) => ({
    label: stage.name,
    duration: formatDuration(stage.duration),
    status: statusLabel(stage.status)
  }))
);

const toolDistributionRows = computed(() =>
  sortMetricRows(
    (charts.value?.tool_distribution || []).map((item: any) => ({
      label: String(item.tool || "unknown"),
      rawValue: Number(item.count || 0),
      value: formatNumber(item.count)
    }))
  )
);

const phaseRows = computed(() =>
  sortMetricRows(
    (charts.value?.phase_timeline || []).map((item: any, index: number) => ({
      label: `${index + 1}. ${item.phase || "unknown"}`,
      rawValue: Number(item.tool_count || 0),
      value: formatNumber(item.tool_count)
    }))
  )
);

const findingSeverityRows = computed(() => {
  const data = judge.value?.charts?.finding_severity || charts.value?.deterministic_finding_severity || [];
  return sortMetricRows(
    (data as any[]).map((item) => ({
      label: String(item.severity || "unknown"),
      rawValue: Number(item.count || 0),
      value: formatNumber(item.count)
    }))
  );
});

const DIMENSION_LABELS: Record<string, string> = {
  intent_recognition: "意图识别",
  task_planning: "任务规划",
  tool_selection: "工具选择",
  parameter_generation: "参数生成",
  evidence_citation: "证据引用",
  state_tracking: "状态更新",
  final_response: "最终表达",
  context_management: "上下文管理",
  execution_closure: "执行闭环",
  error_recovery: "错误恢复",
  verification: "验证能力",
  safety_permission: "安全与权限",
  efficiency: "成本与效率"
};

const dimensionRows = computed(() =>
  Object.entries(judge.value?.dimensions || {}).map(([key, item]) => ({
    key,
    label: DIMENSION_LABELS[key] || key,
    score: Number(item.score || 0),
    status: item.status || "unknown",
    source: item.source || "missing",
    summary: item.summary || "",
    recommendation: item.recommendation || ""
  }))
);

const reliabilityRows = computed(() => {
  const reliability = judge.value?.judge_reliability;
  if (!reliability) return [];
  return [
    { label: "Evidence Coverage", value: formatPercent(reliability.evidence_coverage) },
    { label: "Unsupported Claims", value: formatNumber(reliability.unsupported_claim_count) },
    { label: "Invalid Evidence", value: formatNumber(reliability.invalid_evidence_count) },
    { label: "Fact/Inference Mismatch", value: formatNumber(reliability.fact_inference_mismatch_count) },
    { label: "Valid Evidence", value: formatNumber(reliability.valid_evidence_count) },
    { label: "Total Evidence", value: formatNumber(reliability.total_evidence_count) }
  ];
});

function sortMetricRows<T extends { rawValue: number; label: string; value: string }>(rows: T[]): T[] {
  return [...rows].sort((left, right) => right.rawValue - left.rawValue || left.label.localeCompare(right.label));
}

const tokenInsight = computed(() => {
  const rows = tokenRows.value;
  if (!rows.length) return "暂无 LLM 调用数据。";
  const peak = rows.reduce((current, item) => (item.total > current.total ? item : current), rows[0]);
  return `${peak.label} 的 Token 最高，共 ${formatNumber(peak.total)}；优先检查这次调用的 Prompt 和输出。`;
});

const performanceInsight = computed(() => {
  const rows = runtime.value?.performanceBreakdown || [];
  if (!rows.length) return "暂无耗时构成数据。";
  const top = rows.reduce((current, item) => (item.percent > current.percent ? item : current), rows[0]);
  return `${top.label} 占用最多耗时，约 ${top.percent}%。`;
});

const lifecycleInsight = computed(() => {
  const stages = runtime.value?.lifecycleStages || [];
  if (!stages.length) return "暂无阶段耗时数据。";
  const slowest = stages.reduce((current, item) => (item.duration > current.duration ? item : current), stages[0]);
  return `${slowest.name} 最耗时，用了 ${formatDuration(slowest.duration)}。`;
});

const toolInsight = computed(() => {
  const rows = toolDistributionRows.value;
  if (!rows.length) return "暂无工具调用数据。";
  return `调用最多的是 ${rows[0].label}，共 ${rows[0].value} 次。`;
});

const phaseInsight = computed(() => {
  const rows = phaseRows.value;
  if (!rows.length) return "暂无阶段时间线数据。";
  return `工具调用最多的阶段是 ${rows[0].label}，共 ${rows[0].value} 次。`;
});

const severityInsight = computed(() => {
  const rows = findingSeverityRows.value;
  if (!rows.length) return "暂无发现项等级数据。";
  return `主要发现项等级是 ${rows[0].label}，共 ${rows[0].value} 条。`;
});

const performanceChart = computed(() => ({
  tooltip: { trigger: "axis", formatter: "{b}: {c}%" },
  grid: { left: 118, right: 16, top: 12, bottom: 22 },
  xAxis: {
    type: "value",
    max: 100,
    axisLabel: { color: "#5a6b7d", formatter: "{value}%" },
    splitLine: { lineStyle: { color: "rgba(20, 32, 51, 0.08)" } }
  },
  yAxis: {
    type: "category",
    data: (runtime.value?.performanceBreakdown || []).map((item) => item.label),
    axisLabel: { color: "#5a6b7d" }
  },
  series: [
    {
      type: "bar",
      data: (runtime.value?.performanceBreakdown || []).map((item) => item.percent),
      itemStyle: { color: "#1f6feb", borderRadius: [0, 5, 5, 0] },
      barMaxWidth: 18
    }
  ]
}));

const performancePie = computed(() => ({
  tooltip: { trigger: "item", formatter: "{b}: {d}%" },
  legend: { bottom: 0, textStyle: { color: "#5a6b7d" } },
  series: [
    {
      type: "pie",
      radius: ["46%", "70%"],
      data: (runtime.value?.performanceBreakdown || []).map((item) => ({ name: item.label, value: item.percent })),
      color: ["#1f6feb", "#0b7f74", "#e39b2f", "#5a6b7d"]
    }
  ]
}));

const lifecycleChart = computed(() => ({
  tooltip: { trigger: "axis", formatter: "{b}: {c}s" },
  grid: { left: 118, right: 14, top: 12, bottom: 22 },
  xAxis: {
    type: "value",
    axisLabel: { color: "#5a6b7d", formatter: "{value}s" },
    splitLine: { lineStyle: { color: "rgba(20, 32, 51, 0.08)" } }
  },
  yAxis: {
    type: "category",
    data: (runtime.value?.lifecycleStages || []).map((item) => item.name),
    axisLabel: { color: "#5a6b7d" }
  },
  series: [
    {
      type: "bar",
      data: (runtime.value?.lifecycleStages || []).map((item) => item.duration),
      itemStyle: {
        color: (params: any) => {
          const stage = runtime.value?.lifecycleStages[params.dataIndex];
          return stage?.status === "failed" ? "#d35a2a" : stage?.status === "running" ? "#e39b2f" : "#0b7f74";
        },
        borderRadius: [0, 5, 5, 0]
      },
      barMaxWidth: 16
    }
  ]
}));

const tokenAnalyticsChart = computed(() => {
  const analytics = runtime.value?.llmDashboard?.tokenAnalytics;
  const labels = (analytics?.totalTrend || []).map((item, index) => `${index + 1}. ${item.callId}`);
  return {
    tooltip: { trigger: "axis" },
    legend: { bottom: 0, textStyle: { color: "#5a6b7d" } },
    grid: { left: 58, right: 16, top: 18, bottom: 46 },
    xAxis: { type: "category", data: labels, axisLabel: { color: "#5a6b7d" } },
    yAxis: {
      type: "value",
      axisLabel: { color: "#5a6b7d" },
      splitLine: { lineStyle: { color: "rgba(20, 32, 51, 0.08)" } }
    },
    series: [
      {
        name: "Input",
        type: "line",
        smooth: true,
        data: (analytics?.inputTrend || []).map((item) => item.value),
        itemStyle: { color: "#1f6feb" },
        lineStyle: { color: "#1f6feb", width: 2.5 }
      },
      {
        name: "Output",
        type: "line",
        smooth: true,
        data: (analytics?.outputTrend || []).map((item) => item.value),
        itemStyle: { color: "#0b7f74" },
        lineStyle: { color: "#0b7f74", width: 2.5 }
      },
      {
        name: "Total",
        type: "line",
        smooth: true,
        data: (analytics?.totalTrend || []).map((item) => item.value),
        itemStyle: { color: "#d35a2a" },
        lineStyle: { color: "#d35a2a", width: 2.5 }
      }
    ]
  };
});

const toolChart = computed(() => ({
  tooltip: { trigger: "axis" },
  grid: { left: 38, right: 12, top: 20, bottom: 42 },
  xAxis: {
    type: "category",
    data: (charts.value?.tool_distribution || []).map((item: any) => item.tool),
    axisLabel: { interval: 0, rotate: 24, color: "#5a6b7d" }
  },
  yAxis: {
    type: "value",
    splitLine: { lineStyle: { color: "rgba(20, 32, 51, 0.08)" } },
    axisLabel: { color: "#5a6b7d" }
  },
  series: [
    {
      type: "bar",
      data: (charts.value?.tool_distribution || []).map((item: any) => item.count),
      itemStyle: { color: "#0b7f74", borderRadius: [4, 4, 0, 0] },
      barMaxWidth: 28
    }
  ]
}));

const phaseChart = computed(() => ({
  tooltip: { trigger: "axis" },
  grid: { left: 38, right: 12, top: 20, bottom: 34 },
  xAxis: {
    type: "category",
    data: (charts.value?.phase_timeline || []).map((item: any, index: number) => `${index + 1}. ${item.phase}`),
    axisLabel: { color: "#5a6b7d" }
  },
  yAxis: {
    type: "value",
    splitLine: { lineStyle: { color: "rgba(20, 32, 51, 0.08)" } },
    axisLabel: { color: "#5a6b7d" }
  },
  series: [
    {
      type: "line",
      smooth: true,
      data: (charts.value?.phase_timeline || []).map((item: any) => item.tool_count),
      itemStyle: { color: "#1f6feb" },
      lineStyle: { width: 2.5, color: "#1f6feb" },
      areaStyle: { color: "rgba(31, 111, 235, 0.14)" },
      symbolSize: 7
    }
  ]
}));

const severityChart = computed(() => {
  const data = judge.value?.charts?.finding_severity || charts.value?.deterministic_finding_severity || [];
  return {
    tooltip: { trigger: "item" },
    legend: { bottom: 0, textStyle: { color: "#5a6b7d" } },
    series: [
      {
        type: "pie",
        radius: ["48%", "72%"],
        data: (data as any[]).map((item) => ({ name: item.severity, value: item.count })),
        color: ["#d35a2a", "#e39b2f", "#0b7f74", "#5a6b7d"]
      }
    ]
  };
});

async function loadRun(id: string) {
  if (!id) return;
  error.value = "";
  loading.value = true;
  try {
    const [runData, auditData, chartData, judgeData, runtimeData, trendData] = await Promise.all([
      apiGet<{ summary: Record<string, unknown> }>(`/api/runs/${id}`),
      apiGet<Record<string, any>>(`/api/runs/${id}/audit`),
      apiGet<Record<string, any>>(`/api/runs/${id}/charts`),
      apiGet<JudgeReport>(`/api/runs/${id}/judge`),
      apiGet<RuntimeMetrics>(`/api/runs/${id}/runtime-metrics`),
      apiGet<RuntimeTrends>("/api/runtime-trends")
    ]);
    summary.value = runData.summary;
    audit.value = auditData;
    charts.value = chartData;
    judge.value = judgeData.status === "missing" ? null : judgeData;
    runtime.value = runtimeData;
    runtimeTrends.value = trendData;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    summary.value = null;
    audit.value = null;
    charts.value = null;
    judge.value = null;
    runtime.value = null;
    runtimeTrends.value = null;
  } finally {
    loading.value = false;
  }
}

async function loadJudgeEngines() {
  try {
    const options = await fetchJudgeEngines();
    judgeEngines.value = options.engines;
    const preferred = options.selected || options.default_engine || "deepagent";
    const usable = options.engines.filter((item) => item.id === "deepagent" || item.available);
    judgeEngine.value = usable.some((item) => item.id === preferred)
      ? preferred
      : usable[0]?.id || "deepagent";
  } catch {
    judgeEngines.value = [{ id: "deepagent", label: "DeepAgent 裁判", available: true, disabled_reason: null }];
    judgeEngine.value = "deepagent";
  }
}

async function runJudge() {
  if (!runId.value) return;
  judging.value = true;
  error.value = "";
  try {
    const result = await apiPostJson<{ report: JudgeReport }>(`/api/runs/${runId.value}/judge`, {
      engine: judgeEngine.value
    });
    judge.value = result.report;
    charts.value = (result.report.charts as Record<string, any>) || charts.value;
    runtime.value = await apiGet<RuntimeMetrics>(`/api/runs/${runId.value}/runtime-metrics`);
    await reloadRuns();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    judging.value = false;
  }
}

function selectMetricTarget(target: string) {
  selectedTraceNode.value = target;
}

function selectLlmCall(callId: string) {
  selectedLlmCallId.value = callId;
  selectMetricTarget(callId);
}

function toggleLlmCall(callId: string) {
  const current = new Set(expandedLlmCallIds.value);
  if (current.has(callId)) current.delete(callId);
  else current.add(callId);
  expandedLlmCallIds.value = Array.from(current);
  selectLlmCall(callId);
}

function isLlmCallExpanded(callId: string) {
  return expandedLlmCallIds.value.includes(callId);
}

function formatDuration(value: number | undefined) {
  const duration = Number(value || 0);
  if (!duration) return "-";
  if (duration < 1) return `${Math.round(duration * 1000)}ms`;
  return `${duration.toFixed(duration >= 10 ? 1 : 2)}s`;
}

function formatNumber(value: number | undefined) {
  const parsed = Number(value || 0);
  return Number.isFinite(parsed) ? parsed.toLocaleString() : "-";
}

function formatCost(value: number | undefined) {
  const parsed = Number(value || 0);
  return `$${parsed.toFixed(parsed >= 1 ? 2 : 4)}`;
}

function formatPercent(value: number | undefined) {
  return `${Math.round(Number(value || 0) * 100)}%`;
}

function statusLabel(status: string) {
  if (status === "success") return "Success";
  if (status === "failed") return "Failed";
  if (status === "running") return "Running";
  return status || "-";
}

function formatTime(value: string) {
  if (!value) return "-";
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

async function deleteCurrentRun() {
  if (!runId.value || deleting.value) return;
  deleting.value = true;
  error.value = "";
  try {
    await deleteRunById(runId.value);
  } finally {
    deleting.value = false;
  }
}

async function exportMarkdown() {
  if (!runId.value || exporting.value) return;
  exporting.value = true;
  error.value = "";
  try {
    const blob = await apiDownload(`/api/runs/${encodeURIComponent(runId.value)}/export.md`);
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${runId.value}-evaluation.md`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    exporting.value = false;
  }
}

watch(
  runId,
  (id) => {
    void loadRun(id);
  },
  { immediate: true }
);

onMounted(() => {
  if (!runs.value.length) void reloadRuns();
  void loadJudgeEngines();
});
</script>

<template>
  <div class="page run-page">
    <header class="page-hero">
      <div class="hero-copy">
        <button class="back-link" type="button" @click="router.push({ name: 'batch' })">
          ← 返回批量总览
        </button>
        <p class="eyebrow">Run inspection · {{ serverHost }}</p>
        <h2 class="run-title">{{ runId || "暂无运行" }}</h2>
        <p class="lede">
          {{ selectedMeta?.has_judge ? "已生成裁判分析" : "尚未分析 · 可运行裁判评测" }}
        </p>
      </div>
      <div class="hero-actions">
        <button class="ghost" type="button" :disabled="!runId" @click="router.push({ name: 'trajectory', params: { runId } })">
          查看轨迹
        </button>
        <button class="ghost danger" type="button" :disabled="!runId || deleting" @click="deleteCurrentRun">
          {{ deleting ? "删除中" : "删除运行" }}
        </button>
        <button class="ghost" type="button" :disabled="!runId || loading" @click="loadRun(runId)">
          {{ loading ? "加载中" : "重新加载" }}
        </button>
        <button class="ghost" type="button" :disabled="!runId || exporting" @click="exportMarkdown">
          {{ exporting ? "导出中" : "导出 Markdown" }}
        </button>
        <label class="judge-engine-picker">
          <span class="judge-engine-label">裁判引擎</span>
          <select v-model="judgeEngine" class="judge-engine-select" :disabled="!runId || judging" aria-label="裁判引擎">
            <option v-for="item in judgeEngineChoices" :key="item.id" :value="item.id">
              {{ item.label }}
            </option>
          </select>
        </label>
        <button class="primary" type="button" :disabled="!runId || judging" @click="runJudge">
          {{ judging ? "分析中" : `运行 ${judgeEngineLabel} 分析` }}
        </button>
      </div>
    </header>

    <p v-if="error" class="banner error">{{ error }}</p>
    <p v-if="loading" class="banner loading">正在读取运行数据</p>

    <template v-if="!loading || summary || audit">
      <section class="ops-bar reveal" aria-label="Run filters and context">
        <div class="ops-left">
          <span class="filter-pill active">全部信号</span>
          <span class="filter-pill">Trace</span>
          <span class="filter-pill">LLM</span>
          <span class="filter-pill">Tools</span>
          <span class="filter-pill">Findings</span>
        </div>
        <div class="ops-right">
          <span class="context-token">host: {{ serverHost }}</span>
          <span class="context-token">run: {{ selectedMeta?.has_judge ? "judged" : "pending judge" }}</span>
        </div>
      </section>

      <section class="panel runtime-health-dashboard reveal" aria-label="Agent Runtime Summary">
        <div class="panel-head">
          <div>
            <p class="eyebrow">Agent Runtime Summary</p>
            <h3>Runtime Health Dashboard</h3>
          </div>
          <span class="chip" :data-status="runtime?.status || 'running'">{{ statusLabel(runtime?.status || "running") }}</span>
        </div>
        <div class="runtime-summary runtime-summary-wide">
          <button
            v-for="card in runtimeSummaryCards"
            :key="card.label"
            class="runtime-summary-cell"
            type="button"
            :title="card.detail"
            @click="selectMetricTarget(card.label)"
          >
            <span>{{ card.label }}</span>
            <strong :data-status="card.status">{{ card.value }}</strong>
            <small>{{ card.detail }}</small>
          </button>
        </div>
      </section>

      <section class="panel llm-dashboard reveal" aria-label="LLM Runtime Summary">
        <div class="panel-head">
          <div>
            <p class="eyebrow">LLM Runtime Summary</p>
            <h3>LLM Metrics Dashboard</h3>
          </div>
          <span class="chip quiet">trace {{ runtime?.llmDashboard.summary.traceId || runtime?.traceId || "-" }}</span>
        </div>
        <div class="runtime-summary runtime-summary-wide">
          <button
            v-for="card in llmSummaryCards"
            :key="card.label"
            class="runtime-summary-cell"
            type="button"
            :title="card.detail"
            @click="selectMetricTarget(card.label)"
          >
            <span>{{ card.label }}</span>
            <strong>{{ card.value }}</strong>
            <small>{{ card.detail }}</small>
          </button>
        </div>
      </section>

      <section class="llm-observability-grid reveal">
        <div class="section-label">
          <span>Agent architecture</span>
          <strong>Trace first, metrics second</strong>
        </div>
        <div class="panel llm-timeline-panel">
          <div class="panel-head">
            <h3>LLM Execution Timeline</h3>
            <span class="chip quiet">{{ runtime?.llmDashboard.timeline.length || 0 }} calls</span>
          </div>
          <div class="llm-timeline">
            <div class="llm-start">Agent Start</div>
            <button
              v-for="call in runtime?.llmDashboard.timeline || []"
              :key="call.callId"
              class="llm-timeline-node"
              :class="{ active: selectedLlmCall?.callId === call.callId, failed: call.status === 'failed' }"
              type="button"
              @click="toggleLlmCall(call.callId)"
            >
              <span class="llm-node-index">#{{ call.index }}</span>
              <strong>{{ call.name }}</strong>
              <small>{{ call.model }} · {{ formatDuration(call.latency) }} · {{ formatNumber(call.tokens) }} tokens</small>
              <em :data-status="call.status">{{ statusLabel(call.status) }}</em>
              <div v-if="isLlmCallExpanded(call.callId)" class="llm-node-expand">
                <span>Provider: {{ call.provider }}</span>
                <span>Started: {{ formatTime(call.startTime) }}</span>
                <span v-if="call.error">Error: {{ call.error.type }}</span>
              </div>
            </button>
          </div>
        </div>

        <div class="panel llm-detail-panel">
          <div class="panel-head">
            <h3>LLM Call Detail</h3>
            <span class="chip" :data-status="selectedLlmCall?.status || 'running'">{{ statusLabel(selectedLlmCall?.status || "-") }}</span>
          </div>
          <template v-if="selectedLlmCall">
            <div class="llm-detail-grid">
              <div><span>Call ID</span><strong>{{ selectedLlmCall.callId }}</strong></div>
              <div><span>Model</span><strong>{{ selectedLlmCall.model }}</strong></div>
              <div><span>Provider</span><strong>{{ selectedLlmCall.provider }}</strong></div>
              <div><span>Temperature</span><strong>{{ selectedLlmCall.temperature ?? "-" }}</strong></div>
              <div><span>Max Tokens</span><strong>{{ selectedLlmCall.maxTokens ?? "-" }}</strong></div>
              <div><span>Cost</span><strong>{{ formatCost(selectedLlmCall.cost) }}</strong></div>
            </div>
            <div class="llm-detail-blocks">
              <section>
                <h4>Prompt</h4>
                <dl>
                  <div><dt>Version</dt><dd>{{ selectedLlmCall.prompt.version }}</dd></div>
                  <div><dt>Template</dt><dd>{{ selectedLlmCall.prompt.template || "-" }}</dd></div>
                  <div><dt>Hash</dt><dd>{{ selectedLlmCall.prompt.systemPromptHash || "-" }}</dd></div>
                </dl>
                <pre>{{ selectedLlmCall.prompt.system || "No system prompt captured." }}</pre>
                <pre>{{ selectedLlmCall.prompt.user || selectedLlmCall.prompt.context || "No user/context prompt captured." }}</pre>
              </section>
              <section>
                <h4>Token & Latency</h4>
                <div class="metric-list dense">
                  <div><span>Input Tokens</span><strong>{{ formatNumber(selectedLlmCall.tokenUsage.inputTokens) }}</strong></div>
                  <div><span>Output Tokens</span><strong>{{ formatNumber(selectedLlmCall.tokenUsage.outputTokens) }}</strong></div>
                  <div><span>Total Tokens</span><strong>{{ formatNumber(selectedLlmCall.tokenUsage.totalTokens) }}</strong></div>
                  <div><span>Queue Time</span><strong>{{ formatDuration(selectedLlmCall.latency.queueTime) }}</strong></div>
                  <div><span>Generation Time</span><strong>{{ formatDuration(selectedLlmCall.latency.generationTime) }}</strong></div>
                  <div><span>Total</span><strong>{{ formatDuration(selectedLlmCall.latency.total) }}</strong></div>
                </div>
              </section>
              <section>
                <h4>Response</h4>
                <pre>{{ selectedLlmCall.response || "No response captured." }}</pre>
                <p v-if="selectedLlmCall.error" class="llm-error-line">
                  {{ selectedLlmCall.error.type }} · {{ selectedLlmCall.error.message || "No detail" }}
                </p>
              </section>
            </div>
          </template>
          <p v-else class="empty">No LLM calls captured.</p>
        </div>
      </section>

      <section class="llm-analysis-grid reveal">
        <div class="section-label">
          <span>LLM observability</span>
          <strong>Prompt, token, latency, cost</strong>
        </div>
        <div class="panel">
          <div class="panel-head">
            <h3>Token 趋势</h3>
            <span class="chip quiet">{{ runtime?.llmDashboard.filters.join(" / ") }}</span>
          </div>
          <p class="chart-insight">{{ tokenInsight }}</p>
          <VChart class="chart compact-chart" :option="tokenAnalyticsChart" autoresize />
          <table class="chart-data-table">
            <thead><tr><th>调用</th><th>输入</th><th>输出</th><th>总量</th></tr></thead>
            <tbody>
              <tr v-for="row in tokenRows" :key="row.label">
                <td>{{ row.label }}</td><td>{{ formatNumber(row.input) }}</td><td>{{ formatNumber(row.output) }}</td><td>{{ formatNumber(row.total) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="metric-strip">
            <button type="button" @click="selectMetricTarget('Average Input Token')"><span>平均 Input Token</span><strong>{{ formatNumber(runtime?.llmDashboard.tokenAnalytics.averageInputTokens) }}</strong></button>
            <button type="button" @click="selectMetricTarget('Average Output Token')"><span>平均 Output Token</span><strong>{{ formatNumber(runtime?.llmDashboard.tokenAnalytics.averageOutputTokens) }}</strong></button>
            <button type="button" @click="selectMetricTarget('Max Context')"><span>最大 Context</span><strong>{{ formatNumber(runtime?.llmDashboard.tokenAnalytics.maxContext) }}</strong></button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>Model Performance</h3>
            <span class="chip quiet">{{ runtime?.llmDashboard.modelPerformance.length || 0 }} models</span>
          </div>
          <div class="model-table">
            <div class="model-row head">
              <span>模型</span><span>调用</span><span>成功率</span><span>平均耗时</span><span>Token</span><span>Cost</span>
            </div>
            <button
              v-for="model in runtime?.llmDashboard.modelPerformance || []"
              :key="model.model"
              class="model-row"
              type="button"
              @click="selectMetricTarget(model.model)"
            >
              <strong>{{ model.model }}</strong>
              <span>{{ formatNumber(model.calls) }}</span>
              <span>{{ formatPercent(model.successRate) }}</span>
              <span>{{ formatDuration(model.averageLatency) }}</span>
              <span>{{ formatNumber(model.tokens) }}</span>
              <span>{{ formatCost(model.cost) }}</span>
            </button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>Prompt Version Tracking</h3>
            <span class="chip quiet">{{ runtime?.llmDashboard.promptVersions.length || 0 }} versions</span>
          </div>
          <div class="prompt-version-list">
            <button
              v-for="prompt in runtime?.llmDashboard.promptVersions || []"
              :key="`${prompt.version}-${prompt.systemPromptHash}`"
              type="button"
              @click="selectMetricTarget(prompt.version)"
            >
              <strong>{{ prompt.version }}</strong>
              <span>{{ prompt.template || "no-template" }}</span>
              <small>{{ prompt.systemPromptHash || "no-hash" }}</small>
              <em>{{ formatPercent(prompt.successRate) }} · {{ formatNumber(prompt.averageTokens) }} tokens · {{ formatCost(prompt.cost) }}</em>
            </button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>LLM Quality Metrics</h3>
            <span class="chip quiet">score</span>
          </div>
          <div class="quality-score">
            <strong>{{ runtime?.llmDashboard.qualityMetrics.qualityScore || 0 }}</strong>
            <span>/ 100</span>
          </div>
          <div class="metric-list">
            <div><span>Response Length</span><strong>{{ formatNumber(runtime?.llmDashboard.qualityMetrics.responseLength) }}</strong></div>
            <div><span>Format Compliance</span><strong>{{ formatPercent(runtime?.llmDashboard.qualityMetrics.formatCompliance) }}</strong></div>
            <div><span>Hallucination Risk</span><strong>{{ formatPercent(runtime?.llmDashboard.qualityMetrics.hallucinationRisk) }}</strong></div>
            <div><span>Instruction Following</span><strong>{{ formatPercent(runtime?.llmDashboard.qualityMetrics.instructionFollowing) }}</strong></div>
            <div><span>Self Evaluation Score</span><strong>{{ runtime?.llmDashboard.qualityMetrics.selfEvaluationScore || 0 }}</strong></div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>LLM Error Analysis</h3>
            <span class="chip quiet">trace linked</span>
          </div>
          <div class="error-matrix">
            <button type="button" @click="selectMetricTarget('Timeout')"><span>Timeout</span><strong>{{ runtime?.llmDashboard.errorAnalysis.timeout || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('Rate Limit')"><span>Rate Limit</span><strong>{{ runtime?.llmDashboard.errorAnalysis.rateLimit || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('Context Length Exceeded')"><span>Context Overflow</span><strong>{{ runtime?.llmDashboard.errorAnalysis.contextLengthExceeded || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('Invalid Response')"><span>Invalid Response</span><strong>{{ runtime?.llmDashboard.errorAnalysis.invalidResponse || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('JSON Parse Error')"><span>JSON Parse Error</span><strong>{{ runtime?.llmDashboard.errorAnalysis.jsonParseError || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('Content Filter')"><span>Content Filter</span><strong>{{ runtime?.llmDashboard.errorAnalysis.contentFilter || 0 }}</strong></button>
          </div>
        </div>
      </section>

      <section class="runtime-grid reveal">
        <div class="panel health-panel">
          <div class="panel-head">
            <h3>Agent Health Score</h3>
            <span class="chip quiet">composite</span>
          </div>
          <div class="health-score">
            <div class="ring" :style="{ '--pct': runtime?.healthScore.total || 0 }">
              <div class="ring-core">
                <strong>{{ runtime?.healthScore.total || 0 }}</strong>
                <span>/ 100</span>
              </div>
            </div>
            <div class="health-breakdown">
              <div v-for="part in healthParts" :key="part.label">
                <span>{{ part.label }}</span>
                <strong>{{ part.value }}</strong>
                <i :style="{ width: `${part.value}%` }"></i>
              </div>
            </div>
          </div>
        </div>

        <div class="panel trend-panel">
          <div class="panel-head">
            <h3>最近100次运行</h3>
            <span class="chip quiet">{{ runtimeTrends?.sampleSize || 0 }} runs</span>
          </div>
          <div class="trend-metrics">
            <div>
              <span>Success Rate</span>
              <strong>{{ Math.round((runtimeTrends?.successRate || 0) * 100) }}%</strong>
            </div>
            <div>
              <span>Average Duration</span>
              <strong>{{ formatDuration(runtimeTrends?.averageDuration) }}</strong>
            </div>
            <div>
              <span>Average Token</span>
              <strong>{{ formatNumber(runtimeTrends?.averageTokens) }}</strong>
            </div>
            <div>
              <span>Average Cost</span>
              <strong>{{ formatCost(runtimeTrends?.averageCost) }}</strong>
            </div>
          </div>
        </div>
      </section>

      <section class="panel lifecycle-panel reveal" aria-label="Agent Lifecycle Timeline">
        <div class="panel-head">
          <h3>Agent Lifecycle Timeline</h3>
          <span class="chip quiet">linked to trace</span>
        </div>
        <div class="lifecycle-track">
          <button
            v-for="stage in runtime?.lifecycleStages || []"
            :key="stage.name"
            class="lifecycle-stage"
            :class="stage.status"
            type="button"
            :title="`${stage.name}: ${formatTime(stage.startTime)} - ${formatTime(stage.endTime)}`"
            @click="selectMetricTarget(stage.name)"
          >
            <span class="stage-dot"></span>
            <strong>{{ stage.name }}</strong>
            <small>Duration: {{ formatDuration(stage.duration) }}</small>
            <em :data-status="stage.status">Status: {{ statusLabel(stage.status) }}</em>
          </button>
        </div>
      </section>

      <section class="runtime-detail-grid reveal">
        <div class="panel">
          <div class="panel-head">
            <h3>Execution Metrics</h3>
            <span class="chip quiet">{{ selectedTraceNode || "trace ready" }}</span>
          </div>
          <div class="metric-strip">
            <button v-for="card in executionCards" :key="card.label" type="button" @click="selectMetricTarget(card.label)">
              <span>{{ card.label }}</span>
              <strong>{{ card.value }}</strong>
            </button>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>LLM Runtime Metrics</h3>
          </div>
          <div class="metric-list">
            <div><span>LLM Calls</span><strong>{{ formatNumber(runtime?.llmMetrics.calls) }}</strong></div>
            <div><span>Input Tokens</span><strong>{{ formatNumber(runtime?.llmMetrics.inputTokens) }}</strong></div>
            <div><span>Output Tokens</span><strong>{{ formatNumber(runtime?.llmMetrics.outputTokens) }}</strong></div>
            <div><span>Total Tokens</span><strong>{{ formatNumber(runtime?.llmMetrics.totalTokens) }}</strong></div>
            <div><span>Average Latency</span><strong>{{ formatDuration(runtime?.llmMetrics.averageLatency) }}</strong></div>
            <div><span>Max Latency</span><strong>{{ formatDuration(runtime?.llmMetrics.maxLatency) }}</strong></div>
          </div>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>Tool Runtime Metrics</h3>
          </div>
          <div class="metric-list">
            <div><span>Tool Calls</span><strong>{{ formatNumber(runtime?.toolMetrics.calls) }}</strong></div>
            <div><span>Success</span><strong>{{ formatNumber(runtime?.toolMetrics.success) }}</strong></div>
            <div><span>Failed</span><strong>{{ formatNumber(runtime?.toolMetrics.failed) }}</strong></div>
            <div><span>Average Tool Latency</span><strong>{{ formatDuration(runtime?.toolMetrics.averageLatency) }}</strong></div>
            <div><span>Timeout</span><strong>{{ formatNumber(runtime?.toolMetrics.timeout) }}</strong></div>
          </div>
        </div>
      </section>

      <section class="runtime-analysis-grid reveal">
        <div class="section-label">
          <span>Monitoring</span>
          <strong>Errors, latency share, lifecycle</strong>
        </div>
        <div class="panel">
          <div class="panel-head">
            <h3>Runtime Error Analysis</h3>
            <span class="chip quiet">{{ runtime?.errors.length || 0 }} types</span>
          </div>
          <div class="error-matrix">
            <button type="button" @click="selectMetricTarget('LLM Error')"><span>LLM Error</span><strong>{{ runtime?.errorMetrics.llmError || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('Tool Error')"><span>Tool Error</span><strong>{{ runtime?.errorMetrics.toolError || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('Timeout')"><span>Timeout</span><strong>{{ runtime?.errorMetrics.timeout || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('Invalid Output')"><span>Invalid Output</span><strong>{{ runtime?.errorMetrics.invalidOutput || 0 }}</strong></button>
            <button type="button" @click="selectMetricTarget('Validation Failed')"><span>Validation Failed</span><strong>{{ runtime?.errorMetrics.validationFailed || 0 }}</strong></button>
          </div>
          <ul v-if="runtime?.errors.length" class="runtime-errors">
            <li v-for="item in runtime.errors" :key="item.type">
              <button type="button" @click="selectMetricTarget(item.traceNodeId || item.type)">
                <strong>{{ item.type }}</strong>
                <span>{{ item.count }}</span>
                <small>{{ item.message || "Click to inspect related trace node" }}</small>
              </button>
            </li>
          </ul>
          <p v-else class="empty">No runtime errors detected.</p>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>耗时构成</h3>
            <span class="chip quiet">latency share</span>
          </div>
          <p class="chart-insight">{{ performanceInsight }}</p>
          <div class="performance-charts">
            <VChart class="chart compact-chart" :option="performanceChart" autoresize />
            <VChart class="chart compact-chart" :option="performancePie" autoresize />
          </div>
          <table class="chart-data-table">
            <thead><tr><th>环节</th><th>耗时</th><th>占比</th></tr></thead>
            <tbody>
              <tr v-for="row in performanceRows" :key="row.label">
                <td>{{ row.label }}</td><td>{{ row.value }}</td><td>{{ row.percent }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>阶段耗时</h3>
            <span class="chip quiet">timeline</span>
          </div>
          <p class="chart-insight">{{ lifecycleInsight }}</p>
          <VChart class="chart compact-chart" :option="lifecycleChart" autoresize />
          <table class="chart-data-table">
            <thead><tr><th>阶段</th><th>耗时</th><th>状态</th></tr></thead>
            <tbody>
              <tr v-for="row in lifecycleRows" :key="row.label">
                <td>{{ row.label }}</td><td>{{ row.duration }}</td><td>{{ row.status }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="charts reveal">
        <div class="chart-guide">
          <strong>图表怎么看</strong>
          <span>图表用来看趋势，表格用来看具体数值；红色表示失败或高风险，数值越大越需要优先检查。</span>
        </div>
        <div class="panel">
          <div class="panel-head">
            <h3>工具分布</h3>
          </div>
          <p class="chart-insight">{{ toolInsight }}</p>
          <VChart class="chart" :option="toolChart" autoresize />
          <table class="chart-data-table">
            <thead><tr><th>工具</th><th>调用次数</th></tr></thead>
            <tbody>
              <tr v-for="row in toolDistributionRows" :key="row.label">
                <td>{{ row.label }}</td><td>{{ row.value }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="panel">
          <div class="panel-head">
            <h3>阶段调用趋势</h3>
          </div>
          <p class="chart-insight">{{ phaseInsight }}</p>
          <VChart class="chart" :option="phaseChart" autoresize />
          <table class="chart-data-table">
            <thead><tr><th>阶段</th><th>工具调用</th></tr></thead>
            <tbody>
              <tr v-for="row in phaseRows" :key="row.label">
                <td>{{ row.label }}</td><td>{{ row.value }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="panel compact">
          <div class="panel-head">
            <h3>发现项等级</h3>
          </div>
          <p class="chart-insight">{{ severityInsight }}</p>
          <VChart class="chart" :option="severityChart" autoresize />
          <table class="chart-data-table">
            <thead><tr><th>等级</th><th>数量</th></tr></thead>
            <tbody>
              <tr v-for="row in findingSeverityRows" :key="row.label">
                <td>{{ row.label }}</td><td>{{ row.value }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <section class="report-grid reveal">
        <div class="panel">
          <div class="panel-head">
            <h3>评测维度</h3>
            <span class="chip quiet">{{ dimensionRows.length }}</span>
          </div>
          <div v-if="dimensionRows.length" class="dimension-list">
            <div
              v-for="row in dimensionRows"
              :key="row.key"
              class="dimension-row"
            >
              <span>{{ row.label }}</span>
              <strong v-if="row.status !== 'unknown'">{{ row.score }}</strong>
              <strong v-else>—</strong>
              <em :data-status="row.status">{{ row.status === "unknown" ? "未评估" : row.status }}</em>
              <small>{{ row.summary || row.recommendation || row.source }}</small>
            </div>
          </div>
          <p v-else class="empty">暂无维度评分。</p>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>Judge 可信度</h3>
            <span class="chip quiet">evidence</span>
          </div>
          <div v-if="reliabilityRows.length" class="metric-list">
            <div v-for="row in reliabilityRows" :key="row.label">
              <span>{{ row.label }}</span>
              <strong>{{ row.value }}</strong>
            </div>
          </div>
          <p v-else class="empty">暂无可信度指标。</p>
        </div>

        <div class="panel">
          <div class="panel-head">
            <h3>Agent 判断</h3>
          </div>
          <p class="report">{{ judge?.summary || "尚未生成裁判分析报告。" }}</p>
          <dl v-if="judge" class="judge-meta">
            <div>
              <dt>状态</dt>
              <dd>{{ judge.overall_status }}</dd>
            </div>
            <div>
              <dt>置信度</dt>
              <dd>{{ judge.confidence }}</dd>
            </div>
            <div v-if="judge.judge?.engine">
              <dt>裁判引擎</dt>
              <dd>{{ judge.judge.engine }}</dd>
            </div>
          </dl>
        </div>
        <div class="panel">
          <div class="panel-head">
            <h3>发现项</h3>
            <span class="chip quiet">{{ findings.length }}</span>
          </div>
          <ul v-if="findings.length" class="findings">
            <li v-for="(finding, index) in findings" :key="index">
              <span class="severity" :data-level="String(finding.severity || '').toLowerCase()">
                {{ finding.severity }}
              </span>
              <div>
                <strong>{{ finding.title || finding.category || "finding" }}</strong>
                <p>{{ finding.recommendation || finding.message }}</p>
              </div>
            </li>
          </ul>
          <p v-else class="empty">暂无发现项。</p>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.dimension-list {
  display: grid;
  gap: 8px;
}

.dimension-row {
  display: grid;
  grid-template-columns: minmax(130px, 1fr) 56px 76px;
  gap: 10px;
  align-items: center;
  width: 100%;
  padding: 10px 12px;
  border: 1px solid rgba(20, 32, 51, 0.1);
  border-radius: 6px;
  background: #fff;
  color: inherit;
  text-align: left;
}

.dimension-row small {
  grid-column: 1 / -1;
  color: #5a6b7d;
}

.dimension-row em[data-status="unknown"] {
  color: #5a6b7d;
  font-style: normal;
}

.judge-engine-picker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border: 1px solid rgba(20, 32, 51, 0.14);
  border-radius: 6px;
  background: #fff;
}

.judge-engine-label {
  color: #5a6b7d;
  font-size: 12px;
  white-space: nowrap;
}

.judge-engine-select {
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}

.judge-engine-select:disabled {
  cursor: not-allowed;
  color: #8a97a6;
}
</style>
