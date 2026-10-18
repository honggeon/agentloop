<script setup lang="ts">
import { computed, onMounted, provide, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  apiDelete,
  apiGet,
  canLaunchLocally,
  checkBackendHealth,
  clearAuthToken,
  fetchAuthContext,
  fetchReadiness,
  logout,
  startLocalServices,
  type AuthContext,
  type ReadinessReport,
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
// Phase 5：全局身份与平台就绪状态（启动时各拉一次）。
const authContext = ref<AuthContext | null>(null);
const readiness = ref<ReadinessReport | null>(null);
const logoutBusy = ref(false);

const activeRunId = computed(() => (route.name === "run" ? String(route.params.runId || "") : ""));
const analyzedCount = computed(() => runs.value.filter((item) => item.has_judge).length);

// 当前用户展示：display_name > email > actor；无认证模式退回本地身份。
const currentUserLabel = computed(() => {
  const ctx = authContext.value;
  if (!ctx) return "";
  return ctx.user?.display_name || ctx.user?.email || ctx.actor || "本地用户";
});
const currentRole = computed(() => authContext.value?.role || "");
const isAuthDisabled = computed(() => authContext.value?.auth_mode === "disabled");
const isOidcMode = computed(() => authContext.value?.auth_mode === "oidc");
// 仅 oidc 模式提供服务端登出入口；jwt/local/disabled 无需登出按钮。
const canLogout = computed(() => isOidcMode.value && Boolean(authContext.value?.logout_url));
const readinessDegradedReasons = computed(() => readiness.value?.degraded_reasons ?? []);

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

async function loadAuthContext() {
  try {
    authContext.value = await fetchAuthContext();
  } catch {
    authContext.value = null;
  }
}

async function loadReadiness() {
  try {
    readiness.value = await fetchReadiness();
  } catch {
    readiness.value = null;
  }
}

// oidc 模式登出：先通知服务端（审计），再丢弃本地 token 并回登录页。
async function logoutCurrentUser() {
  if (logoutBusy.value) return;
  logoutBusy.value = true;
  try {
    await logout();
  } catch {
    // 服务端登出失败也继续清理本地 token。
  } finally {
    clearAuthToken();
    logoutBusy.value = false;
    await router.push({ name: "login" });
  }
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
onMounted(loadAuthContext);
onMounted(loadReadiness);
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

      <div v-if="authContext" class="sidebar-user" aria-label="当前用户">
        <div class="user-row">
          <span class="user-name">{{ currentUserLabel }}</span>
          <span v-if="currentRole" class="chip" data-status="running">{{ currentRole }}</span>
        </div>
        <div class="user-actions">
          <RouterLink class="user-link" :to="{ name: 'login' }">登录页</RouterLink>
          <button v-if="canLogout" class="user-link logout" type="button" :disabled="logoutBusy" @click="logoutCurrentUser">
            {{ logoutBusy ? "登出中…" : "登出" }}
          </button>
        </div>
      </div>
      <p v-if="isAuthDisabled" class="banner quiet sidebar-auth-banner">本地开发：认证已禁用，所有请求按本地身份放行。</p>

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
        <RouterLink
          class="nav-link"
          :class="{ active: route.name === 'agents' || route.name === 'agent-detail' || route.name === 'agent-runs' }"
          to="/agents"
        >
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">Agent 工作区</span>
          <small>Agent 列表 · 详情 · 运行</small>
        </RouterLink>
        <RouterLink
          class="nav-link"
          :class="{ active: route.name === 'integration-imports' || route.name === 'integration-import-detail' }"
          to="/integrations/imports"
        >
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">导入任务</span>
          <small>Langfuse / promptfoo 导入</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'promptfoo-drafts' }" to="/integrations/promptfoo/drafts">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">草稿审核</span>
          <small>promptfoo 草稿 → TestCase</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'audit-events' }" to="/admin/audit-events">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">审计日志</span>
          <small>本地审计 · 运行索引</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'admin-health' }" to="/admin/health">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">平台健康</span>
          <small>readiness · 依赖检查</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'admin-workers' }" to="/admin/workers">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">Worker</span>
          <small>队列 · 注册表</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'admin-migrations' }" to="/admin/migrations">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">迁移</span>
          <small>数据库 schema 版本</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'admin-backups' }" to="/admin/backups">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">备份</span>
          <small>平台备份 · 恢复</small>
        </RouterLink>
        <RouterLink class="nav-link" :class="{ active: route.name === 'admin-users' }" to="/admin/users">
          <span class="nav-dot" aria-hidden="true"></span>
          <span class="nav-label">用户</span>
          <small>用户注册表 · 角色</small>
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
      <p v-if="readiness?.status === 'not_ready'" class="banner error global-readiness" role="alert">
        平台不可用：关键依赖未就绪，部分功能可能无法使用。详见
        <RouterLink to="/admin/health">平台健康</RouterLink>。
      </p>
      <p v-else-if="readiness?.status === 'degraded'" class="banner warning global-readiness" role="alert">
        平台处于降级状态：{{ readinessDegradedReasons.join("、") || "存在非关键依赖异常" }}。详见
        <RouterLink to="/admin/health">平台健康</RouterLink>。
      </p>
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
