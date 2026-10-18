<script setup lang="ts">
import { onMounted, ref } from "vue";
import { apiErrorMessage, fetchAdminWorkers, type AdminWorkersPayload } from "../api";

const payload = ref<AdminWorkersPayload | null>(null);
const loading = ref(false);
const error = ref("");

async function load() {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchAdminWorkers();
  } catch (err) {
    payload.value = null;
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
        <h2>Worker 状态</h2>
        <p class="lede">队列模式、内嵌 worker 与跨进程 worker 注册表。心跳超时未上报的 worker 视为失联。</p>
      </div>
      <div class="hero-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">加载 Worker 状态失败：{{ error }}</p>

    <template v-if="payload">
      <section class="metrics agent-kpis" aria-label="队列状态">
        <div class="metric">
          <span>队列模式</span>
          <strong>{{ payload.queue_mode }}</strong>
          <small>queue_mode</small>
        </div>
        <div class="metric">
          <span>队列可达</span>
          <strong>
            <span class="chip" :data-status="payload.queue_reachable ? 'ok' : 'fail'">
              {{ payload.queue_reachable ? "可达" : "不可达" }}
            </span>
          </strong>
          <small>queue_reachable</small>
        </div>
        <div class="metric">
          <span>注册表</span>
          <strong>{{ payload.registry_available ? "可用" : "不可用" }}</strong>
          <small>registry_available</small>
        </div>
        <div class="metric">
          <span>认证模式</span>
          <strong>{{ payload.auth_mode || "—" }}</strong>
          <small>auth_mode</small>
        </div>
      </section>

      <p v-if="!payload.registry_available" class="banner quiet">
        当前为 file 存储模式，无跨进程 worker 注册表；下方仅显示进程内（内嵌）worker 状态。
      </p>

      <section class="panel platform-panel" aria-label="内嵌 worker">
        <div class="panel-head">
          <h3>内嵌 Worker</h3>
          <span v-if="payload.embedded_worker" class="chip" :data-status="payload.embedded_worker.running ? 'running' : 'skipped'">
            {{ payload.embedded_worker.running ? "运行中" : "未运行" }}
          </span>
        </div>
        <template v-if="payload.embedded_worker">
          <p class="hint">
            worker_id：<span class="mono">{{ payload.embedded_worker.worker_id || "—" }}</span>
            ；模式：{{ payload.embedded_worker.mode || "—" }}
          </p>
          <p class="hint">
            已执行：<strong>{{ payload.embedded_worker.executed_jobs ?? 0 }}</strong>
            ；领取失败：<strong>{{ payload.embedded_worker.claim_errors ?? 0 }}</strong>
          </p>
        </template>
        <p v-else class="empty">当前进程没有内嵌 worker。</p>
      </section>

      <section class="panel platform-panel" aria-label="worker 注册表">
        <div class="panel-head">
          <h3>Worker 注册表</h3>
          <span class="chip quiet">{{ payload.workers.length }} 个</span>
        </div>
        <table v-if="payload.workers.length" class="platform-table">
          <thead>
            <tr>
              <th>Worker ID</th>
              <th>状态</th>
              <th>PID</th>
              <th>启动时间</th>
              <th>最近心跳</th>
              <th>心跳距今（秒）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="worker in payload.workers" :key="worker.worker_id">
              <td><span class="mono">{{ worker.worker_id }}</span></td>
              <td><span class="chip" :data-status="worker.status">{{ worker.status }}</span></td>
              <td>{{ worker.pid ?? "—" }}</td>
              <td>{{ worker.started_at || "—" }}</td>
              <td>{{ worker.heartbeat_at || "—" }}</td>
              <td>{{ worker.seconds_since_heartbeat ?? "—" }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">注册表中暂无 worker。</p>
      </section>
    </template>
    <p v-else-if="loading" class="banner loading">加载 Worker 状态中…</p>
  </div>
</template>
