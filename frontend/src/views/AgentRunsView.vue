<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  fetchAgentRuns,
  type AgentOverviewRange,
  type AgentRunRow,
  type AgentRunsPage,
  type AgentRunStatusFilter
} from "../api";

const route = useRoute();
const router = useRouter();

const agentId = computed(() => String(route.params.agentId || ""));
const page = ref<AgentRunsPage | null>(null);
const loading = ref(false);
const error = ref("");

const range = ref<AgentOverviewRange>("7d");
const status = ref<AgentRunStatusFilter>("all");
const offset = ref(0);
const LIMIT = 20;

const RANGE_OPTIONS: Array<{ value: AgentOverviewRange; label: string }> = [
  { value: "7d", label: "近 7 天" },
  { value: "30d", label: "近 30 天" },
  { value: "all", label: "全部" }
];

const STATUS_OPTIONS: Array<{ value: AgentRunStatusFilter; label: string }> = [
  { value: "all", label: "全部状态" },
  { value: "completed", label: "已完成" },
  { value: "failed", label: "失败" },
  { value: "timed_out", label: "超时" },
  { value: "cancelled", label: "已取消" },
  { value: "stale", label: "已中断（stale）" }
];

// Run 生命周期与 Judge overall 是两套枚举，分开映射；unknown 显示「未评估」。
const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  timed_out: "超时",
  cancelled: "已取消",
  skipped: "已跳过"
};

const JUDGE_STATUS_LABELS: Record<string, string> = {
  success: "通过",
  partial: "部分通过",
  failed: "未通过",
  unknown: "未评估"
};

const total = computed(() => page.value?.total ?? 0);
const hasPrev = computed(() => offset.value > 0);
const hasNext = computed(() => offset.value + LIMIT < total.value);

const partialWarning = computed(() => {
  const data = page.value;
  if (!data || (!data.partial && !data.errors.length)) return "";
  const first = data.errors[0];
  const suffix = first ? `：${first.stage} 阶段 — ${first.message}` : "";
  return `部分运行数据扫描失败，当前展示为局部数据${suffix}`;
});

function runStatusLabel(value: string | null): string {
  if (!value) return "—";
  return RUN_STATUS_LABELS[value] || value;
}

function judgeStatusLabel(value: string | null): string {
  if (!value) return "—";
  return JUDGE_STATUS_LABELS[value] || "未评估";
}

function formatScore(value: number | null): string {
  return value === null ? "—" : String(value);
}

function formatDuration(value: number | null): string {
  if (value === null) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function setRange(value: AgentOverviewRange) {
  if (range.value === value) return;
  range.value = value;
  offset.value = 0;
}

function setStatus(value: AgentRunStatusFilter) {
  if (status.value === value) return;
  status.value = value;
  offset.value = 0;
}

function prevPage() {
  if (hasPrev.value) offset.value = Math.max(offset.value - LIMIT, 0);
}

function nextPage() {
  if (hasNext.value) offset.value += LIMIT;
}

function openRun(row: AgentRunRow) {
  void router.push({ name: "run", params: { runId: row.run_id } });
}

function backToDetail() {
  void router.push({ name: "agent-detail", params: { agentId: agentId.value } });
}

async function loadRuns() {
  loading.value = true;
  error.value = "";
  try {
    page.value = await fetchAgentRuns(agentId.value, {
      range: range.value,
      status: status.value,
      limit: LIMIT,
      offset: offset.value
    });
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

watch([range, status, offset], () => {
  void loadRuns();
});

watch(agentId, () => {
  range.value = "7d";
  status.value = "all";
  offset.value = 0;
  void loadRuns();
});

onMounted(loadRuns);
</script>

<template>
  <div class="page agent-page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">Agent 工作区</p>
        <h2 class="run-title">Agent 运行记录</h2>
        <p class="lede">
          <span class="mono">{{ agentId }}</span> 的全部关联 Run，支持时间范围与状态筛选；同一 Case 的多次重试 Run 都可查看。
        </p>
      </div>
      <div class="hero-actions">
        <button type="button" class="back-link" @click="backToDetail">返回 Agent 详情</button>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">{{ error }}</p>
    <p v-else-if="partialWarning" class="banner error" role="status">{{ partialWarning }}</p>
    <p v-if="loading" class="banner loading">加载运行记录中…</p>

    <section class="panel" aria-label="筛选与分页">
      <div class="panel-head runs-toolbar">
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
        <div class="range-switch" role="group" aria-label="运行状态">
          <button
            v-for="option in STATUS_OPTIONS"
            :key="option.value"
            type="button"
            class="ghost"
            :class="{ active: status === option.value }"
            :aria-pressed="status === option.value"
            @click="setStatus(option.value)"
          >
            {{ option.label }}
          </button>
        </div>
      </div>

      <table v-if="page && page.items.length" class="run-table">
        <thead>
          <tr>
            <th>Run ID</th>
            <th>Case</th>
            <th>Job / Attempt</th>
            <th>Run 状态</th>
            <th>Judge overall</th>
            <th>完成度</th>
            <th>开始时间</th>
            <th>耗时</th>
            <th>工具错误</th>
            <th>确定性发现</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in page.items"
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
              <span v-if="row.legacy_inferred" class="chip quiet">旧格式</span>
            </td>
            <td><span class="mono">{{ row.case_id || "—" }}</span></td>
            <td>
              <template v-if="row.job_id">
                <span class="mono">{{ row.job_id }}</span>
                <br />
                <span class="mono">{{ row.attempt_id || "—" }}</span>
              </template>
              <span v-else>—</span>
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
            <td>{{ row.deterministic_finding_count }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!loading && !error" class="empty">该筛选条件下没有运行记录。</p>

      <div class="runs-pagination">
        <button type="button" class="ghost small" :disabled="!hasPrev" aria-label="上一页" @click="prevPage">
          上一页
        </button>
        <span class="hint">
          第 {{ page && page.items.length ? offset + 1 : 0 }}–{{ offset + (page?.items.length || 0) }} 条，共 {{ total }} 条
        </span>
        <button type="button" class="ghost small" :disabled="!hasNext" aria-label="下一页" @click="nextPage">
          下一页
        </button>
      </div>
    </section>
  </div>
</template>
