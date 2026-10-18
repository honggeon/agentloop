<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { fetchAuthContext, type AuthContext } from "../api";

const authContext = ref<AuthContext | null>(null);

const roleLabel = computed(() => {
  const role = authContext.value?.role;
  if (!role) return "未登录或无角色";
  return String(role);
});

const displayName = computed(() => {
  const ctx = authContext.value;
  return ctx?.user?.display_name || ctx?.user?.email || ctx?.actor || "";
});

onMounted(async () => {
  try {
    authContext.value = await fetchAuthContext();
  } catch {
    authContext.value = null;
  }
});
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">403</p>
        <h2>无权限</h2>
        <p class="lede">当前账号已通过认证，但没有访问该页面或执行该操作所需的权限。</p>
      </div>
    </header>

    <section class="panel platform-panel" aria-label="权限说明">
      <div class="panel-head">
        <h3>权限说明</h3>
      </div>
      <p class="hint">
        当前角色：<span class="chip" data-status="failed">{{ roleLabel }}</span>
        <template v-if="displayName">（{{ displayName }}）</template>
      </p>
      <p class="hint">
        角色权限由服务端按 RBAC 判定：viewer 只读，operator 可执行导入与任务控制，admin 可管理平台配置，
        owner 额外拥有用户管理权限。如需更高权限，请联系平台 owner 调整角色。
      </p>
      <p class="hint">
        <router-link class="back-link" :to="{ name: 'batch' }">返回首页</router-link>
        <router-link class="back-link" :to="{ name: 'login' }">重新登录</router-link>
      </p>
    </section>
  </div>
</template>
