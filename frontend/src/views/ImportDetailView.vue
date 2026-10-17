<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  apiErrorMessage,
  cancelImportJob,
  fetchAuthContext,
  fetchImportJob,
  fetchImportJobStatus,
  IMPORT_JOB_PHASE_LABELS,
  IMPORT_JOB_STATUS_LABELS,
  resumeImportJob,
  retryImportJob,
  type AuthContext,
  type ImportJob,
  type ImportJobStatusPayload
} from "../api";

const route = useRoute();
const POLL_INTERVAL_MS = 2000;
const ACTIVE_STATUSES = new Set(["queued", "validating", "running", "paused"]);
const RESUMABLE_STATUSES = new Set(["failed", "partial", "cancelled", "stale"]);

const importId = computed(() => String(route.params.importId || ""));
const authContext = ref<AuthContext | null>(null);
const job = ref<ImportJob | null>(null);
const stale = ref(false);
const loading = ref(false);
const error = ref("");
const actionError = ref("");
const actionBusy = ref(false);
let pollTimer: ReturnType<typeof setInterval> | null = null;

const isLocalDevMode = computed(() => authContext.value?.auth_mode === "disabled");
const canImport = computed(() => Boolean(authContext.value?.permissions?.import));
const isActive = computed(() => ACTIVE_STATUSES.has(String(job.value?.status)) && !stale.value);
const isCancellable = computed(() => isActive.value && !job.value?.cancel_requested);
const isResumable = computed(() => RESUMABLE_STATUSES.has(String(job.value?.status)) || stale.value);

const canCancel = computed(() => canImport.value && isCancellable.value && !actionBusy.value);
const canResume = computed(() => canImport.value && isResumable.value && !actionBusy.value);

const cancelTitle = computed(() => {
  if (!canImport.value) return "当前角色无导入权限（需要 operator）";
  if (job.value?.cancel_requested) return "已请求取消，等待 worker 响应";
  if (!isCancellable.value) return "当前状态不可取消";
  return "请求取消该导入任务（协作式取消）";
});

const resumeTitle = computed(() => {
  if (!canImport.value) return "当前角色无导入权限（需要 operator）";
  if (!isResumable.value) return "仅失败 / 部分完成 / 已取消 / 已失联的任务可以恢复";
  return "从最近一次 checkpoint 断点续跑";
});

const displayStatus = computed(() => {
  if (stale.value) return "stale";
  return String(job.value?.status || "");
});

function statusLabel(status: string): string {
  return IMPORT_JOB_STATUS_LABELS[status] || status || "未知";
}

function phaseLabel(phase: string | null | undefined): string {
  if (!phase) return "—";
  return IMPORT_JOB_PHASE_LABELS[phase] || phase;
}

const progressPercent = computed(() => {
  const totalRecords = Number(job.value?.total_records ?? 0);
  if (!totalRecords) return null;
  const processed = Number(job.value?.processed_records ?? 0);
  return Math.min(100, Math.round((processed / totalRecords) * 100));
});

function applyStatus(payload: ImportJobStatusPayload) {
  stale.value = Boolean(payload.stale) || payload.status === "stale";
  if (!job.value) return;
  const { stale: _stale, ...rest } = payload;
  job.value = { ...job.value, ...rest };
}

async function loadDetail() {
  loading.value = true;
  error.value = "";
  try {
    job.value = await fetchImportJob(importId.value);
    stale.value = String(job.value.status) === "stale";
    if (isActive.value) {
      startPolling();
    } else {
      stopPolling();
    }
  } catch (err) {
    job.value = null;
    error.value = apiErrorMessage(err);
    stopPolling();
  } finally {
    loading.value = false;
  }
}

// 活跃状态每 2 秒轮询状态子集；终态或失联时停止。
function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      const payload = await fetchImportJobStatus(importId.value);
      applyStatus(payload);
      if (!ACTIVE_STATUSES.has(String(payload.status)) || payload.stale) stopPolling();
    } catch {
      stopPolling();
    }
  }, POLL_INTERVAL_MS);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function cancelJob() {
  if (!canCancel.value) return;
  actionBusy.value = true;
  actionError.value = "";
  try {
    job.value = await cancelImportJob(importId.value);
  } catch (err) {
    actionError.value = apiErrorMessage(err);
  } finally {
    actionBusy.value = false;
  }
}

async function resumeJob(useRetry: boolean) {
  if (!canResume.value) return;
  actionBusy.value = true;
  actionError.value = "";
  try {
    const action = useRetry ? retryImportJob : resumeImportJob;
    const result = await action(importId.value);
    if (result.resumed_from_checkpoint === false && job.value) {
      actionError.value = "该任务将从头重新执行（无可用 checkpoint）。";
    }
    await loadDetail();
  } catch (err) {
    // 409 import_invalid_state 等：message 是后端安全文案，直接展示。
    actionError.value = apiErrorMessage(err);
  } finally {
    actionBusy.value = false;
  }
}

watch(importId, () => {
  stopPolling();
  void loadDetail();
});

onMounted(async () => {
  try {
    authContext.value = await fetchAuthContext();
  } catch {
    authContext.value = null;
  }
  void loadDetail();
});
onUnmounted(stopPolling);
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">导入详情</p>
        <h2 class="run-title">
          <span class="mono">{{ importId }}</span>
          <span v-if="job" class="chip" :data-status="displayStatus">{{ statusLabel(displayStatus) }}</span>
          <span v-if="stale" class="chip quiet">worker 已失联（stale）</span>
          <span v-if="job?.cancel_requested" class="chip quiet">已请求取消</span>
          <span v-if="job?.legacy_report" class="chip quiet">旧版导入报告</span>
        </h2>
        <p v-if="job" class="lede agent-header-meta">
          <span>来源系统：{{ job.source_system }}</span>
          <span>类型：{{ job.kind || job.source_type || "—" }}</span>
          <span>模式：{{ job.mode || "—" }}{{ job.dry_run ? "（dry-run）" : "" }}</span>
          <span>当前阶段：{{ phaseLabel(job.phase) }}</span>
          <span>创建：{{ job.created_at || "—" }}</span>
          <span>开始：{{ job.started_at || "—" }}</span>
          <span>结束：{{ job.finished_at || "—" }}</span>
          <span v-if="job.worker_id">worker：{{ job.worker_id }}</span>
        </p>
      </div>
      <div class="hero-actions">
        <router-link class="back-link" :to="{ name: 'integration-imports' }">返回导入列表</router-link>
        <button class="ghost danger" type="button" :disabled="!canCancel" :title="cancelTitle" @click="cancelJob">
          取消导入
        </button>
        <button class="ghost" type="button" :disabled="!canResume" :title="resumeTitle" @click="resumeJob(false)">
          从断点恢复
        </button>
        <button class="ghost" type="button" :disabled="!canResume" :title="resumeTitle" @click="resumeJob(true)">
          重试导入
        </button>
      </div>
    </header>

    <p v-if="isLocalDevMode" class="banner quiet">本地开发模式：认证已禁用，所有操作按本地身份放行。</p>
    <p v-if="error" class="banner error" role="alert">加载导入详情失败：{{ error }}</p>
    <p v-else-if="actionError" class="banner error" role="alert">{{ actionError }}</p>
    <p v-else-if="loading && !job" class="banner loading">加载导入详情中…</p>
    <p v-else-if="!job && !loading" class="empty">未找到该导入任务，或详情加载失败。</p>

    <template v-if="job">
      <section class="metrics agent-kpis" aria-label="导入计数">
        <div class="metric">
          <span>进度</span>
          <strong>{{ job.processed_records ?? 0 }} / {{ job.total_records ?? "—" }}</strong>
          <small>{{ progressPercent === null ? "总量未知" : `已完成 ${progressPercent}%` }}</small>
        </div>
        <div class="metric">
          <span>已导入</span>
          <strong>{{ job.imported_records ?? 0 }}</strong>
          <small>真正落盘的外部证据记录</small>
        </div>
        <div class="metric">
          <span>重复</span>
          <strong>{{ job.duplicate_records ?? 0 }}</strong>
          <small>幂等去重，不重复落盘</small>
        </div>
        <div class="metric">
          <span>未关联</span>
          <strong>{{ job.unlinked_records ?? 0 }}</strong>
          <small>保留为未关联证据，不丢弃</small>
        </div>
        <div class="metric">
          <span>无效 / 失败</span>
          <strong>{{ job.invalid_records ?? 0 }} / {{ job.failed_records ?? 0 }}</strong>
          <small>校验失败与处理失败计数</small>
        </div>
        <div class="metric">
          <span>重试次数</span>
          <strong>{{ job.retry_count ?? 0 }}</strong>
          <small>每次重试从 checkpoint 断点续跑</small>
        </div>
      </section>

      <section v-if="progressPercent !== null" class="panel platform-panel" aria-label="进度">
        <div class="panel-head">
          <h3>进度</h3>
          <span class="chip quiet">{{ progressPercent }}%</span>
        </div>
        <div class="progress-track" role="progressbar" :aria-valuenow="progressPercent" aria-valuemin="0" aria-valuemax="100">
          <div class="progress-fill" :style="{ width: `${progressPercent}%` }"></div>
        </div>
      </section>

      <section class="panel platform-panel" aria-label="断点信息">
        <div class="panel-head">
          <h3>断点（checkpoint）</h3>
          <span class="chip quiet">{{ job.checkpoint_at || "无 checkpoint" }}</span>
        </div>
        <template v-if="job.checkpoint">
          <p class="hint">
            cursor：<span class="mono">{{ job.checkpoint.cursor || "—" }}</span>
            ；page：{{ job.checkpoint.page ?? "—" }}
            ；byte_offset：{{ job.checkpoint.byte_offset ?? "—" }}
            ；index：{{ job.checkpoint.index ?? "—" }}
          </p>
          <p class="hint">
            last_source_id：<span class="mono">{{ job.checkpoint.last_source_id || "—" }}</span>
            ；断点时已处理：{{ job.checkpoint.processed_records ?? "—" }}
            ；断点时间：{{ job.checkpoint.checkpoint_at || job.checkpoint_at || "—" }}
          </p>
          <p v-if="job.restart_from_beginning" class="hint">下次恢复将从头开始（restart_from_beginning）。</p>
        </template>
        <p v-else class="empty">暂无 checkpoint；恢复时将从头开始。</p>
      </section>

      <section v-if="job.error" class="panel platform-panel" aria-label="失败原因">
        <div class="panel-head">
          <h3>失败原因</h3>
          <span class="chip" :data-status="job.error.retryable ? 'partial' : 'failed'">
            {{ job.error.retryable ? "可重试" : "不可重试" }}
          </span>
        </div>
        <p class="hint">错误码：<span class="mono">{{ job.error.code }}</span></p>
        <p class="banner error">{{ job.error.message }}</p>
      </section>

      <section v-if="job.warnings && job.warnings.length" class="panel platform-panel" aria-label="警告">
        <div class="panel-head">
          <h3>警告</h3>
          <span class="chip quiet">{{ job.warnings.length }}</span>
        </div>
        <ul>
          <li v-for="(warning, index) in job.warnings" :key="index" class="hint">{{ warning }}</li>
        </ul>
      </section>
    </template>
  </div>
</template>

<style scoped>
.progress-track {
  height: 8px;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.18);
  overflow: hidden;
}
.progress-fill {
  height: 100%;
  background: #4f8cff;
  transition: width 0.3s ease;
}
</style>
