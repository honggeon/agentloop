<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  apiErrorMessage,
  applyMigrations,
  fetchAdminMigrations,
  fetchAuthContext,
  type AdminMigrationsState,
  type AuthContext
} from "../api";

const state = ref<AdminMigrationsState | null>(null);
const authContext = ref<AuthContext | null>(null);
const loading = ref(false);
const applying = ref(false);
const error = ref("");
const actionError = ref("");
const appliedNotice = ref("");

// apply 需要 admin（require_role(admin)）；disabled 模式下 capabilities 包含全部能力。
const canApply = computed(() => {
  const caps = authContext.value?.capabilities;
  if (Array.isArray(caps) && caps.includes("index:admin")) return true;
  return authContext.value?.auth_mode === "disabled";
});

async function load() {
  loading.value = true;
  error.value = "";
  try {
    state.value = await fetchAdminMigrations();
  } catch (err) {
    state.value = null;
    error.value = apiErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

async function runApply() {
  if (!canApply.value || applying.value) return;
  const pending = state.value?.pending ?? [];
  if (!window.confirm(`确定执行数据库迁移？\n待应用版本：${pending.length ? pending.join(", ") : "（无）"}`)) {
    return;
  }
  applying.value = true;
  actionError.value = "";
  appliedNotice.value = "";
  try {
    const result = await applyMigrations();
    state.value = result;
    appliedNotice.value = result.applied_now?.length
      ? `已应用迁移版本：${result.applied_now.join(", ")}`
      : "没有需要应用的迁移，当前已是最新版本。";
  } catch (err) {
    actionError.value = apiErrorMessage(err);
  } finally {
    applying.value = false;
  }
}

onMounted(async () => {
  try {
    authContext.value = await fetchAuthContext();
  } catch {
    authContext.value = null;
  }
  void load();
});
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">平台管理</p>
        <h2>数据库迁移</h2>
        <p class="lede">Schema 迁移状态：迁移不会自动执行，必须由 admin 显式触发并写入审计日志。</p>
      </div>
      <div class="hero-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
        <button
          v-if="canApply"
          class="ghost"
          type="button"
          :disabled="applying || loading || !state || state.storage_mode === 'file'"
          title="显式应用全部待执行迁移（admin，写入审计日志）"
          @click="runApply"
        >
          {{ applying ? "执行中…" : "执行迁移" }}
        </button>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">加载迁移状态失败：{{ error }}</p>
    <p v-else-if="actionError" class="banner error" role="alert">{{ actionError }}</p>
    <p v-if="appliedNotice" class="banner success">{{ appliedNotice }}</p>

    <template v-if="state">
      <section class="metrics agent-kpis" aria-label="迁移总览">
        <div class="metric">
          <span>当前版本</span>
          <strong>{{ state.current_version }}</strong>
          <small>目标版本：{{ state.schema_version_target }}</small>
        </div>
        <div class="metric">
          <span>状态</span>
          <strong>
            <span class="chip" :data-status="state.up_to_date ? 'ok' : 'queued'">
              {{ state.up_to_date ? "已是最新" : "有待执行迁移" }}
            </span>
          </strong>
          <small>{{ state.required ? "需要数据库迁移" : "无需迁移" }}</small>
        </div>
        <div class="metric">
          <span>待执行</span>
          <strong>{{ state.pending.length }}</strong>
          <small>{{ state.pending.length ? `版本：${state.pending.join(", ")}` : "无待执行版本" }}</small>
        </div>
        <div class="metric">
          <span>存储模式</span>
          <strong>{{ state.storage_mode }}</strong>
          <small>file 模式不使用数据库</small>
        </div>
      </section>

      <p v-if="state.storage_mode === 'file'" class="banner quiet">
        当前为 file 存储模式，不使用数据库，无需 migration。
      </p>

      <section class="panel platform-panel" aria-label="已应用迁移">
        <div class="panel-head">
          <h3>已应用迁移</h3>
          <span class="chip quiet">{{ state.applied.length }} 项</span>
        </div>
        <table v-if="state.applied.length" class="platform-table">
          <thead>
            <tr>
              <th>版本</th>
              <th>名称</th>
              <th>应用时间</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in state.applied" :key="item.version">
              <td>{{ item.version }}</td>
              <td>{{ item.name }}</td>
              <td>{{ item.applied_at || "—" }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">尚未应用任何迁移。</p>
      </section>
    </template>
    <p v-else-if="loading" class="banner loading">加载迁移状态中…</p>
  </div>
</template>
