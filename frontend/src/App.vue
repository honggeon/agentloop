<script setup lang="ts">
import { computed, onMounted, provide, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  apiDelete,
  apiGet,
  canLaunchLocally,
  checkBackendHealth,
  startLocalServices,
  type RunItem
} from "./api";

interface RunGroup {
  host: string;
  label: string;
  runs: RunItem[];
}

const route = useRoute();
const router = useRouter();

const runs = ref<RunItem[]>([]);
const loadingRuns = ref(false);
const deletingRun = ref("");
const error = ref("");
const collapsedHosts = ref<Record<string, boolean>>({});
const backendDown = ref(false);
const launcherBusy = ref(false);
const launcherMessage = ref("");

const activeRunId = computed(() => (route.name === "run" ? String(route.params.runId || "") : ""));
const analyzedCount = computed(() => runs.value.filter((item) => item.has_judge).length);

const runGroups = computed<RunGroup[]>(() => {
  const buckets = new Map<string, RunItem[]>();
  for (const run of runs.value) {
    const host = (run.server_host || "").trim() || "未知来源";
    const list = buckets.get(host) || [];
    list.push(run);
    buckets.set(host, list);
  }
  return [...buckets.entries()]
    .sort(([a], [b]) => {
      if (a === "未知来源") return 1;
      if (b === "未知来源") return -1;
      return a.localeCompare(b, "zh-CN");
    })
    .map(([host, groupRuns]) => ({
      host,
      label: host,
      runs: groupRuns
    }));
});

async function loadRuns() {
  error.value = "";
  loadingRuns.value = true;
  try {
    const data = await apiGet<{ runs: RunItem[] }>("/api/runs");
    runs.value = data.runs;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loadingRuns.value = false;
  }
}

function goBatch() {
  void router.push({ name: "batch" });
}

function goRun(runId: string) {
  void router.push({ name: "run", params: { runId } });
}

function toggleGroup(host: string) {
  collapsedHosts.value = {
    ...collapsedHosts.value,
    [host]: !collapsedHosts.value[host]
  };
}

async function removeRun(runId: string, confirm = true) {
  if (confirm && !window.confirm(`确定删除运行？\n${runId}\n此操作不可恢复。`)) {
    return false;
  }

  deletingRun.value = runId;
  error.value = "";
  try {
    const result = await apiDelete<{ deleted: string; runs: RunItem[] }>(`/api/runs/${encodeURIComponent(runId)}`);
    runs.value = result.runs;
    if (activeRunId.value === runId) {
      await router.push({ name: "batch" });
    }
    return true;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    return false;
  } finally {
    deletingRun.value = "";
  }
}

async function deleteRun(run: RunItem, event: Event) {
  event.stopPropagation();
  event.preventDefault();
  await removeRun(run.name);
}

async function checkBackend() {
  if (!canLaunchLocally) return;
  backendDown.value = !(await checkBackendHealth());
}

async function startServices() {
  if (launcherBusy.value) return;
  launcherBusy.value = true;
  launcherMessage.value = "";
  try {
    const result = await startLocalServices();
    if (!result.ok) {
      launcherMessage.value = `启动失败：${result.error || "未知错误"}${result.output ? `\n${result.output}` : ""}`;
      return;
    }
    // start.sh 已内置健康等待，这里再轮询确认后端真正就绪后再放开页面
    for (let attempt = 0; attempt < 30; attempt += 1) {
      if (await checkBackendHealth()) {
        backendDown.value = false;
        await loadRuns();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 2000));
    }
    launcherMessage.value = "启动命令已执行，但后端健康检查超时，请查看 .logs/backend.log";
  } catch (err) {
    launcherMessage.value = err instanceof Error ? err.message : String(err);
  } finally {
    launcherBusy.value = false;
  }
}

provide("runs", runs);
provide("reloadRuns", loadRuns);
provide("deleteRunById", removeRun);

onMounted(loadRuns);
onMounted(checkBackend);
</script>

<template>
  <main class="shell">
    <aside class="sidebar">
      <div class="brand" @click="goBatch">
        <span class="mark" aria-hidden="true">
          <svg viewBox="0 0 32 32" width="22" height="22">
            <circle cx="16" cy="16" r="11" fill="none" stroke="currentColor" stroke-width="1.6" stroke-dasharray="4 3" />
            <circle cx="16" cy="16" r="3.2" fill="currentColor" />
            <path d="M16 5v4M27 16h-4M16 27v-4M5 16h4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" />
          </svg>
        </span>
        <div>
          <h1>Auditrail</h1>
          <p>轨迹评测控制台</p>
        </div>
      </div>

      <nav class="nav-block" aria-label="Primary">
        <RouterLink class="nav-link" :class="{ active: route.name === 'batch' }" to="/">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">批量评测总览</span>
          <small>首页</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'live' }" to="/live">
          <span class="nav-dot live" aria-hidden="true"></span>
          <span class="nav-label">运行时评测</span>
          <small>Prompt → Agent Judge</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'platform' }" to="/platform">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">测试平台</span>
          <small>Agent · TestCase · Run</small>
        </RouterLink>
      </nav>

      <div class="sidebar-meta">
        <div>
          <span>运行</span>
          <strong>{{ runs.length }}</strong>
        </div>
        <div>
          <span>已分析</span>
          <strong>{{ analyzedCount }}</strong>
        </div>
      </div>

      <button class="refresh" type="button" :disabled="loadingRuns" @click="loadRuns">
        {{ loadingRuns ? "刷新中…" : "刷新运行列表" }}
      </button>

      <p v-if="error" class="sidebar-error">{{ error }}</p>

      <div class="run-section">
        <div class="run-section-head">
          <h2>Runs</h2>
          <span>{{ runGroups.length }} 组</span>
        </div>
        <nav class="run-list" aria-label="Runs">
          <section v-for="group in runGroups" :key="group.host" class="run-group">
            <button type="button" class="group-head" @click="toggleGroup(group.host)">
              <span class="group-title">
                <span class="chevron" :class="{ closed: collapsedHosts[group.host] }">▾</span>
                <span class="host-label">{{ group.label }}</span>
              </span>
              <span class="group-count">{{ group.runs.length }}</span>
            </button>
            <div v-show="!collapsedHosts[group.host]" class="group-body">
              <div
                v-for="run in group.runs"
                :key="run.name"
                class="run-row"
                :class="{ active: run.name === activeRunId }"
              >
                <button type="button" class="run-item" @click="goRun(run.name)">
                  <span class="run-copy">
                    <span>{{ run.name }}</span>
                    <small :class="{ ready: run.has_judge }">{{ run.has_judge ? "已分析" : "未分析" }}</small>
                  </span>
                </button>
                <button
                  type="button"
                  class="run-delete"
                  :disabled="deletingRun === run.name"
                  :aria-label="`删除 ${run.name}`"
                  :title="deletingRun === run.name ? '删除中' : '删除'"
                  @click="deleteRun(run, $event)"
                >
                  <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
                    <path
                      d="M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6"
                      fill="none"
                      stroke="currentColor"
                      stroke-width="1.6"
                      stroke-linecap="round"
                    />
                  </svg>
                </button>
              </div>
            </div>
          </section>
          <p v-if="!runs.length && !loadingRuns" class="empty-side">暂无运行，请先在总览页导入。</p>
        </nav>
      </div>
    </aside>

    <section class="workspace">
      <RouterView />
    </section>

    <div v-if="backendDown" class="launcher-overlay" role="alertdialog" aria-label="后端服务未启动">
      <div class="launcher-card">
        <h2>后端服务未启动</h2>
        <p>看板后端（/api/health）当前不可达，页面数据无法加载。点击下方按钮将执行项目根目录的 ./start.sh 拉起本地服务。</p>
        <button type="button" class="launcher-start" :disabled="launcherBusy" @click="startServices">
          {{ launcherBusy ? "启动中…" : "一键启动服务" }}
        </button>
        <p v-if="launcherMessage" class="launcher-message">{{ launcherMessage }}</p>
      </div>
    </div>
  </main>
</template>
