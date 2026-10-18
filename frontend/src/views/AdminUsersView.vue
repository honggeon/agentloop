<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  apiErrorMessage,
  fetchAdminUsers,
  fetchAuthContext,
  updateUserRoles,
  type AdminUser,
  type AdminUsersPayload,
  type AuthContext
} from "../api";

const ROLE_CHOICES = ["viewer", "operator", "admin", "owner"];
const ROLE_LABELS: Record<string, string> = {
  viewer: "viewer（只读）",
  operator: "operator（导入与任务控制）",
  admin: "admin（平台管理）",
  owner: "owner（含用户管理）"
};

const payload = ref<AdminUsersPayload | null>(null);
const authContext = ref<AuthContext | null>(null);
const loading = ref(false);
const error = ref("");
const actionError = ref("");
const savedNotice = ref("");
const busyUserId = ref("");
// 编辑中的角色草稿：user_id -> 目标角色全集（整体替换，不是增量）。
const roleDrafts = ref<Record<string, string[]>>({});

// 用户管理需要 owner（users:admin）；disabled 模式下 capabilities 包含全部能力。
const canManage = computed(() => {
  const caps = authContext.value?.capabilities;
  if (Array.isArray(caps) && caps.includes("users:admin")) return true;
  return authContext.value?.auth_mode === "disabled";
});

function draftFor(user: AdminUser): string[] {
  return roleDrafts.value[user.user_id] ?? [...user.roles];
}

function toggleRole(user: AdminUser, role: string) {
  const current = new Set(draftFor(user));
  if (current.has(role)) {
    current.delete(role);
  } else {
    current.add(role);
  }
  roleDrafts.value = { ...roleDrafts.value, [user.user_id]: [...current] };
}

function isDirty(user: AdminUser): boolean {
  const draft = draftFor(user);
  return JSON.stringify([...draft].sort()) !== JSON.stringify([...user.roles].sort());
}

async function load() {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchAdminUsers();
    roleDrafts.value = {};
  } catch (err) {
    payload.value = null;
    error.value = apiErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

async function saveRoles(user: AdminUser) {
  if (!canManage.value || busyUserId.value) return;
  const roles = draftFor(user);
  if (!roles.length) {
    actionError.value = "至少保留一个角色。";
    return;
  }
  if (!window.confirm(`确定将用户 ${user.display_name || user.email || user.user_id} 的角色更新为：${roles.join(", ")}？`)) {
    return;
  }
  busyUserId.value = user.user_id;
  actionError.value = "";
  savedNotice.value = "";
  try {
    await updateUserRoles(user.user_id, roles);
    savedNotice.value = `已更新 ${user.display_name || user.user_id} 的角色。`;
    await load();
  } catch (err) {
    actionError.value = apiErrorMessage(err);
  } finally {
    busyUserId.value = "";
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
        <h2>用户管理</h2>
        <p class="lede">OIDC/JWT 用户注册表：查看用户并调整角色。角色整体替换，修改写入审计日志。</p>
      </div>
      <div class="hero-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="load">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">加载用户列表失败：{{ error }}</p>
    <p v-else-if="actionError" class="banner error" role="alert">{{ actionError }}</p>
    <p v-if="savedNotice" class="banner success">{{ savedNotice }}</p>

    <template v-if="payload">
      <p v-if="!payload.available" class="banner quiet">
        {{ payload.note || "用户注册表当前不可用。" }}
      </p>
      <p v-else-if="!canManage" class="banner quiet">当前角色可以查看用户列表，只有 owner 可以修改角色。</p>

      <section v-if="payload.available" class="panel platform-panel" aria-label="用户列表">
        <div class="panel-head">
          <h3>用户列表</h3>
          <span class="chip quiet">{{ payload.users.length }} 人</span>
        </div>
        <table v-if="payload.users.length" class="platform-table">
          <thead>
            <tr>
              <th>用户</th>
              <th>Issuer / Subject</th>
              <th>状态</th>
              <th>最近活跃</th>
              <th>角色</th>
              <th v-if="canManage">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in payload.users" :key="user.user_id">
              <td>
                <strong>{{ user.display_name || user.email || user.user_id }}</strong>
                <br />
                <small v-if="user.email && user.display_name">{{ user.email }}</small>
              </td>
              <td>
                <span class="mono">{{ user.issuer || "—" }}</span>
                <br />
                <small class="mono">{{ user.subject || "—" }}</small>
              </td>
              <td>{{ user.status || "—" }}</td>
              <td>{{ user.last_seen_at || "—" }}</td>
              <td>
                <template v-if="canManage">
                  <label v-for="role in ROLE_CHOICES" :key="role" class="role-choice">
                    <input
                      type="checkbox"
                      :checked="draftFor(user).includes(role)"
                      :disabled="busyUserId === user.user_id"
                      @change="toggleRole(user, role)"
                    />
                    {{ ROLE_LABELS[role] || role }}
                  </label>
                </template>
                <template v-else>
                  <span v-for="role in user.roles" :key="role" class="chip quiet">{{ role }}</span>
                </template>
              </td>
              <td v-if="canManage">
                <button
                  class="ghost small"
                  type="button"
                  :disabled="!isDirty(user) || busyUserId === user.user_id"
                  @click="saveRoles(user)"
                >
                  {{ busyUserId === user.user_id ? "保存中…" : "保存角色" }}
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">暂无用户记录；用户首次通过 OIDC 登录后自动注册。</p>
      </section>
    </template>
    <p v-else-if="loading" class="banner loading">加载用户列表中…</p>
  </div>
</template>

<style scoped>
.role-choice {
  display: block;
  font-size: 13px;
  line-height: 1.8;
  white-space: nowrap;
}
.role-choice input {
  margin-right: 6px;
}
</style>
