<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  apiErrorMessage,
  createAdminBackup,
  fetchAdminBackups,
  fetchAuthContext,
  type AdminBackupsPayload,
  type AuthContext
} from "../api";

const payload = ref<AdminBackupsPayload | null>(null);
const authContext = ref<AuthContext | null>(null);
const loading = ref(false);
const creating = ref(false);
const error = ref("");
const actionError = ref("");
const createdNotice = ref("");

// 创建备份需要 admin；disabled 模式下 capabilities 包含全部能力。
const canCreate = computed(() => {
  const caps = authContext.value?.capabilities;
  if (Array.isArray(caps) && caps.includes("index:admin")) return true;
  return authContext.value?.auth_mode === "disabled";
});

async function load() {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchAdminBackups();
  } catch (err) {
    payload.value = null;
    error.value = apiErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

async function runBackup() {
  if (!canCreate.value || creating.value) return;
  if (!window.confirm("确定立即创建平台备份？\n备份包含平台配置与数据库，不包含 runs/ 证据目录。")) {
    return;
  }
  creating.value = true;
  actionError.value = "";
  createdNotice.value = "";
  try {
    const result = await createAdminBackup();
    createdNotice.value = `备份已创建：${result.backup_id}`;
    await load();
  } catch (err) {
    actionError.value = apiErrorMessage(err);
  } finally {
    creating.value = false;
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
        <h2>备份</h2>
        <p class="lede">平台备份列表与手动备份入口。备份用于平台配置与数据库恢复。</p>
      </div>
      <div class="hero-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
        <button v-if="canCreate" class="ghost" type="button" :disabled="creating" @click="runBackup">
          {{ creating ? "备份中…" : "立即备份" }}
        </button>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">加载备份列表失败：{{ error }}</p>
    <p v-else-if="actionError" class="banner error" role="alert">{{ actionError }}</p>
    <p v-if="createdNotice" class="banner success">{{ createdNotice }}</p>

    <template v-if="payload">
      <p class="banner quiet">
        {{ payload.note || "runs/ 证据目录不包含在平台备份内，请另行备份 runs/。" }}
        <template v-if="!payload.backup_root_configured">未显式配置备份根目录，使用平台目录下的 backups/。</template>
      </p>

      <section class="panel platform-panel" aria-label="备份列表">
        <div class="panel-head">
          <h3>备份列表</h3>
          <span class="chip quiet">{{ payload.backups.length }} 份</span>
        </div>
        <table v-if="payload.backups.length" class="platform-table">
          <thead>
            <tr>
              <th>备份 ID</th>
              <th>创建时间</th>
              <th>文件数</th>
              <th>表数</th>
              <th>包含敏感信息</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="backup in payload.backups" :key="backup.backup_id">
              <td><span class="mono">{{ backup.backup_id }}</span></td>
              <td>{{ backup.created_at || "—" }}</td>
              <td>{{ backup.file_count }}</td>
              <td>{{ backup.table_count }}</td>
              <td>{{ backup.contains_secrets ? "是（需加密保管）" : "否" }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">暂无备份。</p>
      </section>
    </template>
    <p v-else-if="loading" class="banner loading">加载备份列表中…</p>
  </div>
</template>
