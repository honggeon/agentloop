<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  apiErrorMessage,
  fetchAuditEvents,
  fetchAuthContext,
  fetchRunIndexHealth,
  pauseRunIndex,
  resumeRunIndex,
  WATCHER_STATUS_LABELS,
  type AuditEvent,
  type AuthContext,
  type RunIndexHealthPayload
} from "../api";

const PAGE_SIZE = 50;

const authContext = ref<AuthContext | null>(null);
const events = ref<AuditEvent[]>([]);
const total = ref(0);
const offset = ref(0);
const hasMore = ref(false);
const note = ref("");
const loading = ref(false);
const error = ref("");

const resourceTypeFilter = ref("");
const actionFilter = ref("");

const health = ref<RunIndexHealthPayload | null>(null);
const healthError = ref("");
const indexBusy = ref(false);

const isLocalDevMode = computed(() => authContext.value?.auth_mode === "disabled");
const canManageIndex = computed(() => Boolean(authContext.value?.permissions?.manage_index));

const pageRange = computed(() => {
  if (!events.value.length) return "0 / 0";
  return `${offset.value + 1} - ${offset.value + events.value.length} / ${total.value}`;
});

const indexPauseTitle = computed(() => {
  if (!canManageIndex.value) return "当前角色无索引管理权限（需要 admin）";
  return "暂停自动刷新；手动 rebuild 仍可显式执行";
});

function watcherStatusLabel(status: string | null | undefined): string {
  if (!status) return "未知";
  return WATCHER_STATUS_LABELS[status] || status;
}

function resultLabel(result: string | null | undefined): string {
  if (result === "success") return "成功";
  if (result === "denied") return "已拒绝";
  if (result === "failed") return "失败";
  return result || "—";
}

async function loadEvents() {
  loading.value = true;
  error.value = "";
  try {
    const payload = await fetchAuditEvents({
      resource_type: resourceTypeFilter.value.trim() || undefined,
      action: actionFilter.value.trim() || undefined,
      limit: PAGE_SIZE,
      offset: offset.value
    });
    events.value = payload.audit_events;
    total.value = payload.total;
    hasMore.value = payload.has_more;
    note.value = payload.note || "";
  } catch (err) {
    events.value = [];
    error.value = apiErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

async function loadHealth() {
  healthError.value = "";
  try {
    health.value = await fetchRunIndexHealth();
  } catch (err) {
    health.value = null;
    healthError.value = apiErrorMessage(err);
  }
}

function applyFilters() {
  offset.value = 0;
  void loadEvents();
}

function prevPage() {
  if (offset.value <= 0) return;
  offset.value = Math.max(0, offset.value - PAGE_SIZE);
  void loadEvents();
}

function nextPage() {
  if (!hasMore.value) return;
  offset.value += PAGE_SIZE;
  void loadEvents();
}

async function toggleIndexPause() {
  if (!canManageIndex.value || indexBusy.value) return;
  indexBusy.value = true;
  healthError.value = "";
  try {
    const action = health.value?.paused ? resumeRunIndex : pauseRunIndex;
    const result = await action();
    if (health.value) {
      health.value = { ...health.value, watcher: result.watcher, paused: result.paused };
    } else {
      await loadHealth();
    }
  } catch (err) {
    healthError.value = apiErrorMessage(err);
  } finally {
    indexBusy.value = false;
  }
}

onMounted(async () => {
  try {
    authContext.value = await fetchAuthContext();
  } catch {
    authContext.value = null;
  }
  void loadEvents();
  void loadHealth();
});
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">管理</p>
        <h2>审计日志</h2>
        <p class="lede">
          本页展示 append-only 本地审计日志（不支持修改或删除），记录导入、审核、索引管理等操作的
          操作者、角色与结果。
        </p>
        <p v-if="authContext" class="lede agent-header-meta">
          <span>当前身份：{{ authContext.actor }}</span>
          <span>角色：{{ authContext.role }}</span>
          <span>认证模式：{{ authContext.auth_mode }}</span>
        </p>
        <p v-if="authContext?.note" class="hint">{{ authContext.note }}</p>
      </div>
      <div class="hero-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="loadEvents">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </div>
    </header>

    <p v-if="isLocalDevMode" class="banner quiet">本地开发模式：认证已禁用，所有操作按本地身份放行。</p>

    <section class="panel platform-panel" aria-label="运行索引健康">
      <div class="panel-head">
        <h3>运行索引健康</h3>
        <span v-if="health" class="chip" :data-status="health.degraded ? 'failed' : 'completed'">
          {{ health.degraded ? "已降级" : "正常" }}
        </span>
        <span v-if="health?.paused" class="chip" data-status="partial">已暂停自动刷新</span>
      </div>
      <p class="hint">
        {{ health?.note || "索引是缓存，不是事实来源；Run 详情始终读取正式产物。" }}
      </p>
      <template v-if="health">
        <p class="hint">
          索引状态：{{ health.index.status }}
          ；已索引 {{ health.index.indexed_count ?? 0 }} 条，错误 {{ health.index.error_count ?? 0 }} 条
          ；最近扫描：{{ health.index.last_scan_at || "—" }}
          <template v-if="health.index.refresh_reason">；刷新原因：{{ health.index.refresh_reason }}</template>
        </p>
        <p class="hint">
          watcher：{{ watcherStatusLabel(health.watcher?.status) }}
          <template v-if="health.watcher?.last_refresh_at">；最近刷新：{{ health.watcher.last_refresh_at }}</template>
          <template v-if="health.watcher?.failure_count">；失败次数：{{ health.watcher.failure_count }}</template>
          <template v-if="health.watcher?.last_refresh_reason">；原因：{{ health.watcher.last_refresh_reason }}</template>
        </p>
        <p v-if="health.watcher?.last_error" class="hint">最近错误：{{ health.watcher.last_error }}</p>
      </template>
      <p v-if="healthError" class="banner error" role="alert">加载索引健康失败：{{ healthError }}</p>
      <p class="hero-action-group">
        <button class="ghost small" type="button" :disabled="!canManageIndex || indexBusy" :title="indexPauseTitle" @click="toggleIndexPause">
          {{ indexBusy ? "处理中…" : health?.paused ? "恢复自动刷新" : "暂停自动刷新" }}
        </button>
      </p>
    </section>

    <section class="panel platform-panel" aria-label="审计事件">
      <div class="panel-head">
        <h3>审计事件</h3>
        <span class="chip quiet">{{ pageRange }}</span>
      </div>
      <p v-if="note" class="hint">{{ note }}</p>
      <p class="hint">
        resource_type：
        <input v-model="resourceTypeFilter" type="text" placeholder="如 import_job / promptfoo_draft" @keydown.enter="applyFilters" />
        action：
        <input v-model="actionFilter" type="text" placeholder="如 import_create / promptfoo_draft_approve" @keydown.enter="applyFilters" />
        <button class="ghost small" type="button" :disabled="loading" @click="applyFilters">查询</button>
      </p>

      <p v-if="error" class="banner error" role="alert">加载审计日志失败：{{ error }}</p>

      <table v-if="events.length" class="platform-table">
        <thead>
          <tr>
            <th>时间</th>
            <th>操作者</th>
            <th>角色</th>
            <th>操作</th>
            <th>资源类型</th>
            <th>资源 ID</th>
            <th>结果</th>
            <th>原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="event in events" :key="event.event_id">
            <td>{{ event.timestamp }}</td>
            <td>{{ event.actor }}</td>
            <td>{{ event.role }}</td>
            <td><span class="mono">{{ event.action }}</span></td>
            <td>{{ event.resource_type }}</td>
            <td><span class="mono">{{ event.resource_id || "—" }}</span></td>
            <td>
              <span class="chip" :data-status="event.result === 'success' ? 'success' : event.result === 'denied' ? 'partial' : 'failed'">
                {{ resultLabel(event.result) }}
              </span>
            </td>
            <td>{{ event.reason || "—" }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="loading" class="empty">加载审计日志中…</p>
      <p v-else class="empty">暂无审计事件。</p>

      <p class="hint">
        <button class="ghost small" type="button" :disabled="offset <= 0 || loading" @click="prevPage">上一页</button>
        <button class="ghost small" type="button" :disabled="!hasMore || loading" @click="nextPage">下一页</button>
        最新事件在前。
      </p>
    </section>
  </div>
</template>
