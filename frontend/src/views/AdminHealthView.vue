<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  apiErrorMessage,
  fetchReadiness,
  READINESS_CHECK_STATUS_LABELS,
  READINESS_STATUS_LABELS,
  type ReadinessReport
} from "../api";

const report = ref<ReadinessReport | null>(null);
const loading = ref(false);
const error = ref("");

// fetchReadiness 对 503 也解析 body：not_ready 时 report 仍然完整，
// 页面顶部显示「平台不可用」红色横幅，下方继续展示各项检查明细。
const isNotReady = computed(() => report.value?.status === "not_ready");

function statusLabel(status: string): string {
  return READINESS_STATUS_LABELS[status] || status || "未知";
}

function checkStatusLabel(status: string): string {
  return READINESS_CHECK_STATUS_LABELS[status] || status || "未知";
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    report.value = await fetchReadiness();
  } catch (err) {
    report.value = null;
    error.value = apiErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

onMounted(load);
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">平台管理</p>
        <h2>平台健康</h2>
        <p class="lede">
          依赖就绪（readiness）报告：数据库、队列、平台存储、runs 根目录、审计日志与 worker。
          健康检查不暴露连接串、绝对路径等敏感信息。
        </p>
      </div>
      <div class="hero-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </div>
    </header>

    <p v-if="isNotReady" class="banner error" role="alert">
      平台不可用：关键依赖未就绪（not_ready），请优先处理下方标记为「失败」的关键检查项。
    </p>
    <p v-else-if="report?.status === 'degraded'" class="banner warning" role="alert">
      平台处于降级状态：{{ report.degraded_reasons.join("、") || "存在非关键依赖异常" }}。
    </p>
    <p v-if="error" class="banner error" role="alert">加载平台健康失败：{{ error }}</p>

    <template v-if="report">
      <section class="metrics agent-kpis" aria-label="总状态">
        <div class="metric">
          <span>总状态</span>
          <strong><span class="chip" :data-status="report.status">{{ statusLabel(report.status) }}</span></strong>
          <small>{{ report.timestamp || "—" }}</small>
        </div>
        <div class="metric">
          <span>部署模式</span>
          <strong>{{ report.deployment_mode }}</strong>
          <small>deployment_mode</small>
        </div>
        <div class="metric">
          <span>存储模式</span>
          <strong>{{ report.storage_mode }}</strong>
          <small>storage_mode</small>
        </div>
        <div class="metric">
          <span>队列模式</span>
          <strong>{{ report.queue_mode }}</strong>
          <small>queue_mode</small>
        </div>
        <div class="metric">
          <span>认证模式</span>
          <strong>{{ report.auth_mode }}</strong>
          <small>auth_mode · {{ report.single_tenant ? "单租户" : "多租户" }}</small>
        </div>
      </section>

      <section class="panel platform-panel" aria-label="依赖检查">
        <div class="panel-head">
          <h3>依赖检查</h3>
          <span class="chip quiet">{{ report.checks.length }} 项</span>
        </div>
        <table class="platform-table">
          <thead>
            <tr>
              <th>检查项</th>
              <th>状态</th>
              <th>关键</th>
              <th>说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="check in report.checks" :key="check.name">
              <td><span class="mono">{{ check.name }}</span></td>
              <td><span class="chip" :data-status="check.status">{{ checkStatusLabel(check.status) }}</span></td>
              <td>{{ check.critical ? "关键" : "非关键" }}</td>
              <td>{{ check.detail || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel platform-panel" aria-label="运行指标">
        <div class="panel-head">
          <h3>运行指标</h3>
        </div>
        <p class="hint">
          失联导入任务：<strong>{{ report.operational.stale_import_jobs }}</strong>
          ；队列降级任务：<strong>{{ report.operational.queue_degraded_jobs }}</strong>
        </p>
        <p class="hint">
          索引 watcher：{{ report.operational.index_watcher?.status || "未知" }}
          <template v-if="report.operational.index_watcher?.paused">（已暂停）</template>
          ；运行索引：{{ report.operational.run_index?.status || "未知" }}
        </p>
        <template v-if="report.operational.recent_import_failures.length">
          <p class="hint">最近导入失败：</p>
          <table class="platform-table">
            <thead>
              <tr>
                <th>导入 ID</th>
                <th>来源</th>
                <th>状态</th>
                <th>错误码</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="failure in report.operational.recent_import_failures" :key="failure.import_id">
                <td><span class="mono">{{ failure.import_id }}</span></td>
                <td>{{ failure.source_system || "—" }}</td>
                <td>{{ failure.status || "—" }}</td>
                <td><span class="mono">{{ failure.error_code || "—" }}</span></td>
              </tr>
            </tbody>
          </table>
        </template>
        <p v-else class="hint">暂无最近导入失败记录。</p>
        <p v-if="report.degraded_reasons.length" class="hint">
          降级原因：<span v-for="reason in report.degraded_reasons" :key="reason" class="chip quiet">{{ reason }}</span>
        </p>
      </section>
    </template>
    <p v-else-if="loading" class="banner loading">加载平台健康中…</p>
  </div>
</template>
