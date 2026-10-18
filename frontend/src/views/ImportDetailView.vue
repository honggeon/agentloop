<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  apiErrorMessage,
  cancelCostImport,
  cancelImportJob,
  fetchAuthContext,
  fetchCostImport,
  fetchImportJob,
  fetchImportJobStatus,
  IMPORT_JOB_PHASE_LABELS,
  IMPORT_JOB_STATUS_LABELS,
  pauseCostImport,
  pauseImport,
  resumeCostImport,
  resumeImportJob,
  retryCostImport,
  retryImportJob,
  type AuthContext,
  type ImportJob,
  type ImportJobStatusPayload
} from "../api";

const route = useRoute();
const BASE_POLL_INTERVAL_MS = 2000;
const MAX_POLL_INTERVAL_MS = 30000;
const ACTIVE_STATUSES = new Set(["queued", "validating", "running", "paused"]);
const PAUSABLE_STATUSES = new Set(["queued", "validating", "running"]);
const RESUMABLE_STATUSES = new Set(["failed", "partial", "cancelled", "stale", "paused"]);

const importId = computed(() => String(route.params.importId || ""));
const authContext = ref<AuthContext | null>(null);
const job = ref<ImportJob | null>(null);
const stale = ref(false);
// 成本导入（kind === "cost_records"）走 /api/costs/imports 控制端点（同形 job view）。
const isCostImport = ref(false);
const loading = ref(false);
const error = ref("");
const actionError = ref("");
const actionBusy = ref(false);
let pollTimer: ReturnType<typeof setTimeout> | null = null;
let pollFailures = 0;
let pollingActive = false;

const isLocalDevMode = computed(() => authContext.value?.auth_mode === "disabled");
const canImport = computed(() => Boolean(authContext.value?.permissions?.import));
const isActive = computed(() => ACTIVE_STATUSES.has(String(job.value?.status)) && !stale.value);
const isPausable = computed(() => PAUSABLE_STATUSES.has(String(job.value?.status)) && !stale.value && !job.value?.pause_requested);
const isCancellable = computed(() => isActive.value && !job.value?.cancel_requested);
const isResumable = computed(() => RESUMABLE_STATUSES.has(String(job.value?.status)) || stale.value);

const canPause = computed(() => canImport.value && isPausable.value && !actionBusy.value);
const canCancel = computed(() => canImport.value && isCancellable.value && !actionBusy.value);
const canResume = computed(() => canImport.value && isResumable.value && !actionBusy.value);

const pauseTitle = computed(() => {
  if (!canImport.value) return "当前角色无导入权限（需要 operator）";
  if (job.value?.pause_requested) return "已请求暂停，等待 worker 在批次边界停下";
  if (!isPausable.value) return "仅排队中 / 校验中 / 运行中的任务可以暂停";
  return "协作式暂停：worker 在下一个批次/分页边界停下并释放租约";
});

const cancelTitle = computed(() => {
  if (!canImport.value) return "当前角色无导入权限（需要 operator）";
  if (job.value?.cancel_requested) return "已请求取消，等待 worker 响应";
  if (!isCancellable.value) return "当前状态不可取消";
  return "请求取消该导入任务（协作式取消）";
});

const resumeTitle = computed(() => {
  if (!canImport.value) return "当前角色无导入权限（需要 operator）";
  if (!isResumable.value) return "仅失败 / 部分完成 / 已取消 / 已失联 / 已暂停的任务可以恢复";
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

function isTerminalStatus(status: string): boolean {
  return !ACTIVE_STATUSES.has(status);
}

async function loadDetail() {
  loading.value = true;
  error.value = "";
  try {
    try {
      job.value = await fetchImportJob(importId.value);
      isCostImport.value = String(job.value.kind || "") === "cost_records";
    } catch (integrationErr) {
      // 成本导入不在 integrations 详情端点下：回退到 costs 端点（同形 job view）。
      try {
        job.value = await fetchCostImport(importId.value);
        isCostImport.value = true;
      } catch {
        throw integrationErr;
      }
    }
    stale.value = String(job.value.status) === "stale";
    pollFailures = 0;
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

// 轮询降频策略：任务进入终态（completed/partial/failed/cancelled）即停止；
// 页面不可见（document.hidden）时暂停，重新可见时恢复；
// 连续失败 3 次后指数退避（2s → 4s → 8s …），最长 30s。
function currentPollDelay(): number {
  if (pollFailures < 3) return BASE_POLL_INTERVAL_MS;
  return Math.min(MAX_POLL_INTERVAL_MS, BASE_POLL_INTERVAL_MS * 2 ** (pollFailures - 2));
}

function scheduleNextPoll() {
  if (!pollingActive || pollTimer) return;
  if (typeof document !== "undefined" && document.hidden) return;
  pollTimer = setTimeout(runPollOnce, currentPollDelay());
}

async function runPollOnce() {
  pollTimer = null;
  if (!pollingActive) return;
  try {
    if (isCostImport.value) {
      // 成本导入没有 /status 子端点，直接拉完整 job view。
      const latest = await fetchCostImport(importId.value);
      if (job.value) {
        job.value = { ...job.value, ...latest };
      }
      stale.value = String(latest.status) === "stale";
      if (isTerminalStatus(String(latest.status)) || stale.value) {
        stopPolling();
        return;
      }
    } else {
      const payload = await fetchImportJobStatus(importId.value);
      applyStatus(payload);
      if (isTerminalStatus(String(payload.status)) || payload.stale) {
        stopPolling();
        return;
      }
    }
    pollFailures = 0;
  } catch {
    pollFailures += 1;
  }
  scheduleNextPoll();
}

function startPolling() {
  stopPolling();
  pollingActive = true;
  scheduleNextPoll();
}

function stopPolling() {
  pollingActive = false;
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function handleVisibilityChange() {
  if (document.hidden) {
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
  } else {
    scheduleNextPoll();
  }
}

async function pauseJob() {
  if (!canPause.value) return;
  actionBusy.value = true;
  actionError.value = "";
  try {
    job.value = isCostImport.value ? await pauseCostImport(importId.value) : await pauseImport(importId.value);
  } catch (err) {
    actionError.value = apiErrorMessage(err);
  } finally {
    actionBusy.value = false;
  }
}

async function cancelJob() {
  if (!canCancel.value) return;
  actionBusy.value = true;
  actionError.value = "";
  try {
    job.value = isCostImport.value ? await cancelCostImport(importId.value) : await cancelImportJob(importId.value);
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
    const action = isCostImport.value
      ? useRetry
        ? retryCostImport
        : resumeCostImport
      : useRetry
        ? retryImportJob
        : resumeImportJob;
    const result = await action(importId.value);
    if (result.resumed_from_checkpoint === false && job.value) {
      actionError.value = "该任务将从头重新执行（无可用 checkpoint）。";
    }
    if (result.queue_degraded) {
      actionError.value = "队列当前不可达，任务已标记为队列降级，恢复后自动续跑。";
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
  isCostImport.value = false;
  void loadDetail();
});

onMounted(async () => {
  document.addEventListener("visibilitychange", handleVisibilityChange);
  try {
    authContext.value = await fetchAuthContext();
  } catch {
    authContext.value = null;
  }
  void loadDetail();
});
onUnmounted(() => {
  stopPolling();
  document.removeEventListener("visibilitychange", handleVisibilityChange);
});
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">导入详情</p>
        <h2 class="run-title">
          <span class="mono">{{ importId }}</span>
          <span v-if="job" class="chip" :data-status="displayStatus">{{ statusLabel(displayStatus) }}</span>
          <span v-if="isCostImport" class="chip quiet">成本导入</span>
          <span v-if="stale" class="chip quiet">worker 已失联（stale）</span>
          <span v-if="job?.cancel_requested" class="chip quiet">已请求取消</span>
          <span v-if="job?.pause_requested" class="chip quiet">已请求暂停</span>
          <span v-if="job?.queue_degraded" class="chip warning">队列降级</span>
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
          <span>尝试：{{ job.attempt_count ?? 0 }} / {{ job.max_attempts ?? "—" }}</span>
          <span>租约版本：{{ job.lease_version ?? "—" }}</span>
          <span v-if="job.next_retry_at">下次重试：{{ job.next_retry_at }}</span>
        </p>
      </div>
      <div class="hero-actions">
        <router-link class="back-link" :to="{ name: 'integration-imports' }">返回导入列表</router-link>
        <button class="ghost" type="button" :disabled="!canPause" :title="pauseTitle" @click="pauseJob">
          暂停导入
        </button>
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
