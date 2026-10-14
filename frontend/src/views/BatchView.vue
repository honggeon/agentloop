<script setup lang="ts">
import { computed, inject, onMounted, ref, type Ref } from "vue";
import { useRouter } from "vue-router";
import VChart from "vue-echarts";
import { use } from "echarts/core";
import { BarChart } from "echarts/charts";
import { GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import {
  apiGet,
  apiPostJson,
  type BatchSummary,
  type ImportSessionPayload,
  type ImportSessionResult,
  type RunItem
} from "../api";
import { formatNumber, formatPercent } from "../utils";

use([BarChart, GridComponent, TooltipComponent, CanvasRenderer]);

const router = useRouter();
const runs = inject<Ref<RunItem[]>>("runs")!;
const reloadRuns = inject<() => Promise<void>>("reloadRuns")!;

const batchSummary = ref<BatchSummary | null>(null);
const loading = ref(false);
const importing = ref(false);
const error = ref("");
const importMessage = ref("");
const importOpen = ref(false);
const importForm = ref<ImportSessionPayload>({
  base_url: "",
  user_id: "",
  session_id: "",
  token: "",
  timeout: 60
});

const rootCauseChart = computed(() => ({
  tooltip: { trigger: "axis" },
  grid: { left: 132, right: 18, top: 12, bottom: 22 },
  xAxis: {
    type: "value",
    splitLine: { lineStyle: { color: "rgba(20, 32, 51, 0.08)" } },
    axisLabel: { color: "#5a6b7d" }
  },
  yAxis: {
    type: "category",
    data: (batchSummary.value?.root_causes || []).map((item) => item.label).reverse(),
    axisLabel: { width: 118, overflow: "truncate", color: "#142033" },
    axisLine: { show: false },
    axisTick: { show: false }
  },
  series: [
    {
      type: "bar",
      data: (batchSummary.value?.root_causes || []).map((item) => item.count).reverse(),
      itemStyle: {
        color: {
          type: "linear",
          x: 0,
          y: 0,
          x2: 1,
          y2: 0,
          colorStops: [
            { offset: 0, color: "#d35a2a" },
            { offset: 1, color: "#f08a4b" }
          ]
        },
        borderRadius: [0, 4, 4, 0]
      },
      barMaxWidth: 18
    }
  ]
}));

const successArc = computed(() => {
  const rate = batchSummary.value?.success_rate;
  if (typeof rate !== "number") return 0;
  return Math.max(0, Math.min(100, Math.round(rate * 1000) / 10));
});

async function loadBatch() {
  error.value = "";
  loading.value = true;
  try {
    batchSummary.value = await apiGet<BatchSummary>("/api/runs/batch-summary");
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

async function openRun(runId: string) {
  await router.push({ name: "run", params: { runId } });
}

async function importRemoteSession() {
  error.value = "";
  importMessage.value = "";
  importing.value = true;
  try {
    const result = await apiPostJson<ImportSessionResult>("/api/runs/import-session", {
      ...importForm.value,
      base_url: importForm.value.base_url.trim(),
      user_id: importForm.value.user_id.trim(),
      session_id: importForm.value.session_id.trim(),
      token: importForm.value.token?.trim() || undefined
    });
    batchSummary.value = result.batch_summary;
    await reloadRuns();
    importMessage.value = `已导入 ${result.run.name}`;
    await openRun(result.run.name);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    importing.value = false;
  }
}

onMounted(async () => {
  await Promise.all([loadBatch(), runs.value.length ? Promise.resolve() : reloadRuns()]);
});
</script>

<template>
  <div class="page batch-page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">Batch evaluation</p>
        <h2>批量评测总览</h2>
        <p class="lede">汇总全部运行的成功率、工具错误与失败根因，点击任意运行进入单次评测。</p>
      </div>
      <div class="hero-actions">
        <button class="ghost" type="button" :disabled="loading" @click="loadBatch">
          {{ loading ? "刷新中" : "刷新数据" }}
        </button>
        <button class="primary" type="button" @click="importOpen = !importOpen">
          {{ importOpen ? "收起导入" : "导入远端会话" }}
        </button>
      </div>
    </header>

    <p v-if="error" class="banner error">{{ error }}</p>
    <p v-if="loading && !batchSummary" class="banner loading">正在读取批量评测数据</p>

    <section class="ops-bar reveal" aria-label="Batch filters">
      <div class="ops-left">
        <span class="filter-pill active">全部运行</span>
        <span class="filter-pill">失败</span>
        <span class="filter-pill">已分析</span>
        <span class="filter-pill">高错误率</span>
      </div>
      <div class="ops-right">
        <span class="context-token">runs: {{ batchSummary?.total_runs ?? runs.length }}</span>
        <span class="context-token">success: {{ formatPercent(batchSummary?.success_rate) }}</span>
      </div>
    </section>

    <section v-if="importOpen" class="import-strip reveal">
      <div class="import-head">
        <h3>导入远端会话</h3>
        <p>从其他 agent 服务器拉取 session，并写入当前评测目录。</p>
      </div>
      <form class="import-form" @submit.prevent="importRemoteSession">
        <label>
          <span>服务器地址</span>
          <input v-model="importForm.base_url" type="url" placeholder="http://192.168.1.238:8788" required />
        </label>
        <label>
          <span>用户 ID</span>
          <input v-model="importForm.user_id" type="text" placeholder="auditrail-test" required />
        </label>
        <label>
          <span>Session ID</span>
          <input v-model="importForm.session_id" type="text" placeholder="5ab7b3b0-..." required />
        </label>
        <label>
          <span>Token</span>
          <input v-model="importForm.token" type="password" autocomplete="off" placeholder="可选" />
        </label>
        <label class="short-field">
          <span>超时秒数</span>
          <input v-model.number="importForm.timeout" type="number" min="1" step="1" required />
        </label>
        <button class="primary" type="submit" :disabled="importing">
          {{ importing ? "导入中" : "导入会话" }}
        </button>
      </form>
      <p v-if="importMessage" class="banner success">{{ importMessage }}</p>
    </section>

    <section class="signal-board reveal">
      <article class="signal-hero">
        <div class="ring" :style="{ '--pct': `${successArc}` }">
          <div class="ring-core">
            <strong>{{ formatPercent(batchSummary?.success_rate) }}</strong>
            <span>成功率</span>
          </div>
        </div>
        <div class="signal-copy">
          <h3>评测健康度</h3>
          <p>
            {{ batchSummary?.successful_runs ?? 0 }} 次成功 /
            {{ batchSummary?.failed_runs ?? 0 }} 次失败，共
            {{ batchSummary?.total_runs ?? 0 }} 次运行
          </p>
        </div>
      </article>
      <article class="signal-metric">
        <span>工具错误率</span>
        <strong>{{ formatPercent(batchSummary?.tool_error_rate) }}</strong>
      </article>
      <article class="signal-metric">
        <span>平均调用次数</span>
        <strong>{{ formatNumber(batchSummary?.average_tool_calls) }}</strong>
      </article>
      <article class="signal-metric">
        <span>运行总数</span>
        <strong>{{ batchSummary?.total_runs ?? "-" }}</strong>
      </article>
    </section>

    <section class="batch-grid reveal">
      <div class="panel">
        <div class="panel-head">
          <h3>常见失败根因</h3>
          <span class="chip">{{ batchSummary?.root_causes.length || 0 }} 类</span>
        </div>
        <VChart v-if="batchSummary?.root_causes.length" class="chart" :option="rootCauseChart" autoresize />
        <p v-else class="empty">暂无失败根因。</p>
      </div>

      <div class="panel table-panel">
        <div class="panel-head">
          <h3>批量结果</h3>
          <span class="chip quiet">点击行进入评测</span>
        </div>
        <table class="run-table">
          <thead>
            <tr>
              <th>服务器</th>
              <th>运行</th>
              <th>状态</th>
              <th>评分</th>
              <th>调用</th>
              <th>错误率</th>
              <th>失败根因</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="run in batchSummary?.runs || []"
              :key="run.run_id"
              tabindex="0"
              @click="openRun(run.run_id)"
              @keydown.enter="openRun(run.run_id)"
            >
              <td class="mono">{{ run.server_host || "未知来源" }}</td>
              <td class="mono">{{ run.run_id }}</td>
              <td>
                <span class="status" :class="{ ok: run.is_success }">
                  {{ run.is_success ? "成功" : "失败" }}
                </span>
              </td>
              <td>{{ run.completion_score ?? "-" }}</td>
              <td>{{ run.tool_call_count }}</td>
              <td>{{ formatPercent(run.tool_error_rate) }}</td>
              <td>{{ run.failure_root_cause || "-" }}</td>
            </tr>
            <tr v-if="!(batchSummary?.runs || []).length">
              <td colspan="7" class="empty-row">暂无运行数据。可先导入远端会话。</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
