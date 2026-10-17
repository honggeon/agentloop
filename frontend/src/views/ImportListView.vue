<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  apiErrorMessage,
  fetchAuthContext,
  fetchImportJobs,
  IMPORT_JOB_STATUS_LABELS,
  type AuthContext,
  type ImportJobSummary
} from "../api";

const router = useRouter();
const PAGE_SIZE = 50;

// 「进行中」聚合排队 / 校验 / 运行 / 暂停四种活跃状态。
const ACTIVE_STATUSES = new Set(["queued", "validating", "running", "paused"]);

const STATUS_FILTERS: Array<{ value: string; label: string }> = [
  { value: "all", label: "全部" },
  { value: "active", label: "进行中" },
  { value: "completed", label: "已完成" },
  { value: "partial", label: "部分完成" },
  { value: "failed", label: "失败" },
  { value: "cancelled", label: "已取消" },
  { value: "stale", label: "已失联" }
];

const authContext = ref<AuthContext | null>(null);
const imports = ref<ImportJobSummary[]>([]);
const total = ref(0);
const offset = ref(0);
const hasMore = ref(false);
const loading = ref(false);
const error = ref("");
const statusFilter = ref("all");

const isLocalDevMode = computed(() => authContext.value?.auth_mode === "disabled");

const filteredImports = computed(() => {
  if (statusFilter.value === "all") return imports.value;
  if (statusFilter.value === "active") {
    return imports.value.filter((job) => ACTIVE_STATUSES.has(String(job.status)));
  }
  return imports.value.filter((job) => String(job.status) === statusFilter.value);
});

const pageRange = computed(() => {
  if (!imports.value.length) return "0 / 0";
  return `${offset.value + 1} - ${offset.value + imports.value.length} / ${total.value}`;
});

function statusLabel(status: string | null | undefined): string {
  if (!status) return "未知";
  return IMPORT_JOB_STATUS_LABELS[status] || status;
}

function progressText(job: ImportJobSummary): string {
  const processed = job.processed_records ?? 0;
  if (job.total_records === null || job.total_records === undefined) {
    return `${processed} / —`;
  }
  return `${processed} / ${job.total_records}`;
}

async function loadAuth() {
  try {
    authContext.value = await fetchAuthContext();
  } catch {
    authContext.value = null;
  }
}

async function loadImports() {
  loading.value = true;
  error.value = "";
  try {
    const payload = await fetchImportJobs(PAGE_SIZE, offset.value);
    imports.value = payload.imports;
    total.value = payload.total;
    hasMore.value = payload.has_more;
  } catch (err) {
    imports.value = [];
    error.value = apiErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

function prevPage() {
  if (offset.value <= 0) return;
  offset.value = Math.max(0, offset.value - PAGE_SIZE);
  void loadImports();
}

function nextPage() {
  if (!hasMore.value) return;
  offset.value += PAGE_SIZE;
  void loadImports();
}

function openImport(job: ImportJobSummary) {
  void router.push({ name: "integration-import-detail", params: { importId: job.import_id } });
}

onMounted(loadAuth);
onMounted(loadImports);
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">集成</p>
        <h2>导入任务</h2>
        <p class="lede">
          Langfuse / promptfoo 外部证据导入任务列表。导入是只读数据落盘，不执行任何外部内容，
          也不修改正式 run 产物。
        </p>
      </div>
      <div class="hero-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="loadImports">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </div>
    </header>

    <p v-if="isLocalDevMode" class="banner quiet">本地开发模式：认证已禁用，所有操作按本地身份放行。</p>
    <p v-if="error" class="banner error" role="alert">加载导入任务失败：{{ error }}</p>

    <section class="panel platform-panel" aria-label="导入任务列表">
      <div class="panel-head">
        <h3>导入任务</h3>
        <span class="chip quiet">{{ pageRange }}</span>
      </div>
      <p class="hint">
        状态过滤：
        <select v-model="statusFilter" aria-label="状态过滤">
          <option v-for="item in STATUS_FILTERS" :key="item.value" :value="item.value">{{ item.label }}</option>
        </select>
        （进行中 = 排队中 + 校验中 + 运行中 + 已暂停）
      </p>

      <table v-if="filteredImports.length" class="platform-table">
        <thead>
          <tr>
            <th>导入 ID</th>
            <th>来源</th>
            <th>类型</th>
            <th>状态</th>
            <th>进度</th>
            <th>导入 / 重复</th>
            <th>未关联 / 无效</th>
            <th>失败</th>
            <th>重试</th>
            <th>创建时间</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="job in filteredImports"
            :key="job.import_id"
            tabindex="0"
            @click="openImport(job)"
            @keydown.enter="openImport(job)"
          >
            <td><span class="mono">{{ job.import_id }}</span></td>
            <td>
              {{ job.source_system }}
              <span v-if="job.dry_run" class="chip quiet">dry-run</span>
              <span v-if="job.legacy_report" class="chip quiet">旧版报告</span>
            </td>
            <td>{{ job.kind || job.source_type || "—" }}</td>
            <td>
              <span class="chip" :data-status="job.status">{{ statusLabel(String(job.status)) }}</span>
              <span v-if="job.cancel_requested" class="chip quiet">已请求取消</span>
            </td>
            <td>{{ progressText(job) }}</td>
            <td>{{ job.imported_records ?? 0 }} / {{ job.duplicate_records ?? 0 }}</td>
            <td>{{ job.unlinked_records ?? 0 }} / {{ job.invalid_records ?? 0 }}</td>
            <td>{{ job.failed_records ?? 0 }}</td>
            <td>{{ job.retry_count ?? 0 }}</td>
            <td>{{ job.created_at || "—" }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="loading" class="empty">加载导入任务中…</p>
      <p v-else class="empty">暂无导入任务。</p>

      <p class="hint">
        <button class="ghost small" type="button" :disabled="offset <= 0 || loading" @click="prevPage">上一页</button>
        <button class="ghost small" type="button" :disabled="!hasMore || loading" @click="nextPage">下一页</button>
        点击行查看导入详情。
      </p>
    </section>
  </div>
</template>
