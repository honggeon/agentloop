<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import VChart from "vue-echarts";
import { use } from "echarts/core";
import { BarChart } from "echarts/charts";
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import {
  fetchAgentDetail,
  fetchAgentOverview,
  type AgentDetailPayload,
  type AgentHealthMetric,
  type AgentOverview,
  type AgentOverviewRange,
  type AgentRunRow
} from "../api";
import { formatPercent } from "../utils";

use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

const route = useRoute();
const router = useRouter();

const agentId = computed(() => String(route.params.agentId || ""));
const detail = ref<AgentDetailPayload | null>(null);
const overview = ref<AgentOverview | null>(null);
const range = ref<AgentOverviewRange>("7d");
const loading = ref(false);
const overviewLoading = ref(false);
const error = ref("");

const RANGE_OPTIONS: Array<{ value: AgentOverviewRange; label: string }> = [
  { value: "7d", label: "近 7 天" },
  { value: "30d", label: "近 30 天" },
  { value: "all", label: "全部" }
];

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  timed_out: "超时",
  cancelled: "已取消",
  skipped: "已跳过"
};

// Judge overall 与 Run 生命周期严格不同；unknown 显示「未评估」，null 显示「—」。
const JUDGE_STATUS_LABELS: Record<string, string> = {
  success: "通过",
  partial: "部分通过",
  failed: "未通过",
  unknown: "未评估"
};

const HEALTH_SECTIONS: Array<{ key: keyof AgentOverview["health"]; label: string }> = [
  { key: "task_completion", label: "任务完成" },
  { key: "tool_stability", label: "工具稳定" },
  { key: "performance", label: "性能" },
  { key: "cost_efficiency", label: "成本效率" },
  { key: "deterministic_finding_count", label: "确定性发现数" },
  { key: "high_severity_finding_count", label: "高严重度发现数" }
];

const agent = computed(() => overview.value?.agent || detail.value?.agent || null);

const partialWarning = computed(() => {
  const parts: string[] = [];
  if (detail.value && (detail.value.partial || detail.value.errors.length)) parts.push("Agent 详情");
  if (overview.value && (overview.value.partial || overview.value.errors.length)) parts.push("指标聚合");
  if (!parts.length) return "";
  const first = overview.value?.errors?.[0] || detail.value?.errors?.[0];
  const suffix = first ? `：${first.stage} 阶段 — ${first.message}` : "";
  return `部分数据扫描失败，${parts.join("、")}当前展示为局部数据${suffix}`;
});

const kpiCards = computed(() => {
  const kpis = overview.value?.kpis;
  if (!kpis) return [];
  return [
    { label: "Trace 数量", value: String(kpis.trace_count), hint: "选定范围内的 Trace 数" },
    { label: "Run 数量", value: String(kpis.run_count), hint: "选定范围内的运行数" },
    {
      label: "错误率",
      value: kpis.error_rate === null ? "—" : formatPercent(kpis.error_rate),
      hint: "统计口径：失败/超时运行占全部运行的比例"
    },
    {
      label: "完成度分数",
      value: kpis.completion_score === null ? "—" : String(kpis.completion_score),
      hint: "Judge completion_score 均值；无 Judge 数据时为 —"
    },
    {
      label: "Token 总量",
      value: kpis.token_count === null ? "—" : String(kpis.token_count),
      hint: "观测到的输入 + 输出 token"
    },
    { label: "总成本", value: formatCost(kpis.total_cost, kpis.total_cost_source), hint: costHint(kpis.total_cost_source) },
    {
      label: "Judge 证据覆盖率",
      value: kpis.judge_evidence_coverage === null ? "—" : formatPercent(kpis.judge_evidence_coverage),
      hint: "Judge 结论中可回指证据的比例"
    }
  ];
});

const healthItems = computed(() => {
  const health = overview.value?.health;
  if (!health) return [];
  return HEALTH_SECTIONS.map((section) => {
    const metric: AgentHealthMetric | undefined = health[section.key];
    return {
      key: section.key,
      title: section.label,
      label: metric?.label || "未评估",
      status: metric?.status || "unknown",
      value: metric && metric.value !== null ? String(metric.value) : "—",
      detail: metric?.detail || "暂无足够数据进行评估。",
      source: metric?.source || "missing"
    };
  });
});

// 图表只负责看趋势，具体数值以下方表格为准（可访问性要求）。
const traceChart = computed(() => {
  const traces = overview.value?.traces || [];
  return {
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 40, right: 16, top: 36, bottom: 28 },
    xAxis: { type: "category", data: traces.map((item) => item.date) },
    yAxis: { type: "value", minInterval: 1 },
    series: [
      { name: "已完成", type: "bar", stack: "runs", itemStyle: { color: "#0b7f74" }, data: traces.map((item) => item.completed_count) },
      { name: "失败", type: "bar", stack: "runs", itemStyle: { color: "#d35a2a" }, data: traces.map((item) => item.failed_count) },
      { name: "超时", type: "bar", stack: "runs", itemStyle: { color: "#e39b2f" }, data: traces.map((item) => item.timed_out_count) },
      { name: "已取消", type: "bar", stack: "runs", itemStyle: { color: "#5a6b7d" }, data: traces.map((item) => item.cancelled_count) }
    ]
  };
});

const traceSummary = computed(() => {
  const traces = overview.value?.traces || [];
  const total = traces.reduce((sum, item) => sum + item.run_count, 0);
  const failed = traces.reduce((sum, item) => sum + item.failed_count + item.timed_out_count, 0);
  const errors = traces.reduce((sum, item) => sum + item.error_count, 0);
  return `选定范围内共 ${total} 次运行，失败/超时 ${failed} 次，工具与运行错误 ${errors} 个。`;
});

function runStatusLabel(status: string | null): string {
  if (!status) return "—";
  return RUN_STATUS_LABELS[status] || status;
}

function judgeStatusLabel(status: string | null): string {
  if (!status) return "—";
  return JUDGE_STATUS_LABELS[status] || "未评估";
}

function formatLatency(value: number | null): string {
  if (value === null || Number.isNaN(value)) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function formatDuration(value: number | null): string {
  return formatLatency(value);
}

// cost_source=estimated 的成本必须标注「估算」；missing/null 显示「—」。
function formatCost(cost: number | null, source: string | null): string {
  if (cost === null || source === null || source === "missing") return "—";
  const amount = `$${cost.toFixed(4)}`;
  return source === "estimated" ? `估算 ${amount}` : amount;
}

function costHint(source: string | null): string {
  if (source === "estimated") return "按 token 估算的成本，非账单观测值";
  if (source === "observed") return "运行产物中观测到的成本";
  return "暂无成本数据";
}

function formatScore(value: number | null): string {
  return value === null ? "—" : String(value);
}

function setRange(value: AgentOverviewRange) {
  if (range.value === value) return;
  range.value = value;
}

function openRun(row: AgentRunRow) {
  void router.push({ name: "run", params: { runId: row.run_id } });
}

function openAllRuns() {
  void router.push({ name: "agent-runs", params: { agentId: agentId.value } });
}

function backToAgents() {
  void router.push({ name: "agents" });
}

async function loadOverview() {
  overviewLoading.value = true;
  try {
    overview.value = await fetchAgentOverview(agentId.value, range.value);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    overviewLoading.value = false;
  }
}

async function loadAll() {
  loading.value = true;
  error.value = "";
  try {
    detail.value = await fetchAgentDetail(agentId.value);
    await loadOverview();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

watch(range, () => {
  void loadOverview();
});

watch(agentId, () => {
  range.value = "7d";
  void loadAll();
});

onMounted(loadAll);
</script>

<template>
  <div class="page agent-page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">Agent 详情</p>
        <h2 class="run-title">
          {{ agent?.name || agentId }}
          <span v-if="agent?.derived" class="chip quiet derived-badge">来源于运行数据</span>
        </h2>
        <p class="lede agent-header-meta">
          <span class="mono">{{ agent?.agent_id || agentId }}</span>
          <span v-if="agent?.type">类型：{{ agent.type }}</span>
          <span v-if="agent?.environment">环境：{{ agent.environment }}</span>
          <span v-if="agent?.source_host">来源主机：{{ agent.source_host }}</span>
          <span>最近运行：{{ agent?.last_run_at || "—" }}</span>
          <span v-if="agent?.last_run_status" class="chip" :data-status="agent.last_run_status">
            {{ runStatusLabel(agent.last_run_status) }}
          </span>
        </p>
      </div>
      <div class="hero-actions">
        <button type="button" class="back-link" @click="backToAgents">返回 Agent 列表</button>
        <div class="range-switch" role="group" aria-label="时间范围">
          <button
            v-for="option in RANGE_OPTIONS"
            :key="option.value"
            type="button"
            class="ghost"
            :class="{ active: range === option.value }"
            :aria-pressed="range === option.value"
            @click="setRange(option.value)"
          >
            {{ option.label }}
          </button>
        </div>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">{{ error }}</p>
    <p v-else-if="partialWarning" class="banner error" role="status">{{ partialWarning }}</p>
    <p v-if="loading" class="banner loading">加载 Agent 详情中…</p>
    <p v-else-if="overviewLoading" class="banner loading">刷新指标中…</p>

    <template v-if="overview">
      <section class="metrics agent-kpis" aria-label="关键指标">
        <div v-for="card in kpiCards" :key="card.label" class="metric">
          <span>{{ card.label }}</span>
          <strong>{{ card.value }}</strong>
          <small>{{ card.hint }}</small>
        </div>
      </section>

      <div class="agent-grid">
        <section class="panel agent-span-6" aria-label="Models">
          <div class="panel-head">
            <h3>Models</h3>
            <span class="chip quiet">{{ overview.models.length }}</span>
          </div>
          <table v-if="overview.models.length" class="run-table">
            <thead>
              <tr>
                <th>模型</th>
                <th>Provider</th>
                <th>LLM 调用</th>
                <th>Token（入/出）</th>
                <th>平均延迟</th>
                <th>错误数</th>
                <th>成本</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="model in overview.models" :key="model.model">
                <td><span class="mono">{{ model.model }}</span></td>
                <td>{{ model.provider || "—" }}</td>
                <td>{{ model.llm_call_count }}</td>
                <td>{{ model.input_tokens }} / {{ model.output_tokens }}</td>
                <td>{{ formatLatency(model.average_latency_ms) }}</td>
                <td>{{ model.error_count }}</td>
                <td>{{ formatCost(model.cost, model.cost_source) }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty">该范围内没有 LLM 调用数据。</p>
        </section>

        <section class="panel agent-span-6" aria-label="Tools">
          <div class="panel-head">
            <h3>Tools</h3>
            <span class="chip quiet">{{ overview.tools.length }}</span>
          </div>
          <table v-if="overview.tools.length" class="run-table">
            <thead>
              <tr>
                <th>工具</th>
                <th>调用次数</th>
                <th>成功 / 失败</th>
                <th>错误率</th>
                <th>危险调用</th>
                <th>审计来源</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="tool in overview.tools" :key="tool.tool_name">
                <td><span class="mono">{{ tool.tool_name }}</span></td>
                <td>{{ tool.call_count }}</td>
                <td>{{ tool.success_count }} / {{ tool.error_count }}</td>
                <td>{{ tool.error_rate === null ? "—" : formatPercent(tool.error_rate) }}</td>
                <td>{{ tool.dangerous_count }}</td>
                <td>{{ tool.audit_source }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty">该范围内没有工具调用数据。</p>
          <p class="hint">错误率口径：工具调用错误数 / 工具调用次数，不是 run 失败率。</p>
        </section>

        <section class="panel agent-span-12" aria-label="Traces per day">
          <div class="panel-head">
            <h3>Traces per day</h3>
            <span class="chip quiet">{{ overview.data_range }}</span>
          </div>
          <template v-if="overview.traces.length">
            <p class="chart-insight">{{ traceSummary }}</p>
            <VChart class="chart compact-chart" :option="traceChart" autoresize aria-label="每日运行数量柱状图" />
            <table class="chart-data-table">
              <thead>
                <tr>
                  <th>日期</th>
                  <th>运行数</th>
                  <th>完成</th>
                  <th>失败</th>
                  <th>超时</th>
                  <th>取消</th>
                  <th>错误数</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="day in overview.traces" :key="day.date">
                  <td>{{ day.date }}</td>
                  <td>{{ day.run_count }}</td>
                  <td>{{ day.completed_count }}</td>
                  <td>{{ day.failed_count }}</td>
                  <td>{{ day.timed_out_count }}</td>
                  <td>{{ day.cancelled_count }}</td>
                  <td>{{ day.error_count }}</td>
                </tr>
              </tbody>
            </table>
          </template>
          <p v-else class="empty">该范围内没有运行记录，无法绘制每日趋势。</p>
        </section>

        <section class="panel agent-span-12" aria-label="Auditrail Health">
          <div class="panel-head">
            <h3>Auditrail Health</h3>
          </div>
          <div class="health-grid">
            <div v-for="item in healthItems" :key="item.key" class="health-item" :data-status="item.status">
              <div class="health-item-head">
                <span>{{ item.title }}</span>
                <span class="chip health-status" :data-status="item.status">{{ item.label }}</span>
              </div>
              <strong>{{ item.value }}</strong>
              <p>{{ item.detail }}</p>
              <small>来源：{{ item.source }}</small>
            </div>
          </div>
        </section>

        <section class="panel agent-span-12" aria-label="Recent Runs">
          <div class="panel-head">
            <h3>Recent Runs</h3>
            <button type="button" class="ghost small" @click="openAllRuns">查看全部运行</button>
          </div>
          <table v-if="overview.recent_runs.length" class="run-table">
            <thead>
              <tr>
                <th>Run ID</th>
                <th>状态</th>
                <th>Judge overall</th>
                <th>完成度</th>
                <th>开始时间</th>
                <th>耗时</th>
                <th>工具错误</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="row in overview.recent_runs"
                :key="row.run_id"
                tabindex="0"
                :aria-label="`打开运行 ${row.run_id}`"
                @click="openRun(row)"
                @keydown.enter.prevent="openRun(row)"
                @keydown.space.prevent="openRun(row)"
              >
                <td>
                  <span class="mono">{{ row.run_id }}</span>
                  <span v-if="row.retry_index && row.retry_index > 0" class="chip quiet">第 {{ row.retry_index }} 次重试</span>
                  <span v-if="row.stale" class="chip quiet">已中断（stale）</span>
                </td>
                <td><span class="chip" :data-status="row.status">{{ runStatusLabel(row.status) }}</span></td>
                <td>
                  <span v-if="row.judge_overall_status" class="chip" :data-status="row.judge_overall_status">
                    {{ judgeStatusLabel(row.judge_overall_status) }}
                  </span>
                  <span v-else>—</span>
                </td>
                <td>{{ formatScore(row.completion_score) }}</td>
                <td>{{ row.started_at || "—" }}</td>
                <td>{{ formatDuration(row.duration_ms) }}</td>
                <td>{{ row.tool_error_count }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="empty">该范围内没有运行记录。</p>
        </section>
      </div>
    </template>
  </div>
</template>
