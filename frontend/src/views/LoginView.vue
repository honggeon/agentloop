<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  apiErrorMessage,
  fetchAuthContext,
  setAuthToken,
  type AuthContext
} from "../api";

const route = useRoute();
const router = useRouter();

// 离开页面前把 next 暂存在 sessionStorage：OIDC 回调只回 /login/callback#token=...，
// 不带原 query，回调完成后从这里取回目标页。
const LOGIN_NEXT_STORAGE_KEY = "auditrail.login_next";

const authContext = ref<AuthContext | null>(null);
const loading = ref(true);
const error = ref("");
const tokenInput = ref("");
const tokenBusy = ref(false);
const callbackFailed = ref(false);

const isCallback = computed(() => route.name === "login-callback");
const isLocalLike = computed(() => ["disabled", "local"].includes(String(authContext.value?.auth_mode || "")));
const isOidc = computed(() => authContext.value?.auth_mode === "oidc");
const isSsoMode = computed(() => isOidc.value || authContext.value?.auth_mode === "jwt");
const authenticated = computed(() => Boolean(authContext.value?.authenticated));

const displayName = computed(() => {
  const ctx = authContext.value;
  return ctx?.user?.display_name || ctx?.user?.email || ctx?.actor || "本地用户";
});

const authModeLabel = computed(() => {
  const mode = String(authContext.value?.auth_mode || "");
  if (mode === "disabled") return "认证已禁用";
  if (mode === "local") return "本地身份";
  if (mode === "oidc") return "OIDC 单点登录";
  if (mode === "jwt") return "JWT Bearer token";
  return mode || "未知";
});

function queryNext(): string | null {
  const next = route.query.next;
  if (typeof next === "string" && next.startsWith("/") && !next.startsWith("//")) return next;
  return null;
}

function nextTarget(): string {
  const fromQuery = queryNext();
  if (fromQuery) return fromQuery;
  try {
    const stored = window.sessionStorage.getItem(LOGIN_NEXT_STORAGE_KEY);
    if (stored && stored.startsWith("/") && !stored.startsWith("//")) return stored;
  } catch {
    // sessionStorage 不可用时退回首页。
  }
  return "/";
}

function clearStoredNext() {
  try {
    window.sessionStorage.removeItem(LOGIN_NEXT_STORAGE_KEY);
  } catch {
    // 忽略存储不可用。
  }
}

// OIDC 回调把 token 放在 URL fragment：fragment 不进 access log 与服务器历史。
// 解析后立刻从地址栏清除，避免 token 残留在历史记录或被复制分享。
function parseCallbackToken(): string | null {
  const hash = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : window.location.hash;
  const token = new URLSearchParams(hash).get("token");
  return token && token.trim() ? token.trim() : null;
}

async function loadContext() {
  loading.value = true;
  error.value = "";
  try {
    authContext.value = await fetchAuthContext();
  } catch (err) {
    error.value = apiErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

async function handleCallback() {
  const token = parseCallbackToken();
  if (!token) {
    callbackFailed.value = true;
    error.value = "登录回调缺少 token，请重新发起登录。";
    loading.value = false;
    return;
  }
  setAuthToken(token);
  window.history.replaceState(null, "", `${window.location.pathname}${window.location.search}`);
  try {
    authContext.value = await fetchAuthContext();
  } catch (err) {
    callbackFailed.value = true;
    error.value = apiErrorMessage(err);
    loading.value = false;
    return;
  }
  const target = nextTarget();
  clearStoredNext();
  await router.replace(target);
}

function startSsoLogin() {
  const loginUrl = authContext.value?.login_url;
  if (!loginUrl) return;
  try {
    const next = queryNext();
    if (next) window.sessionStorage.setItem(LOGIN_NEXT_STORAGE_KEY, next);
  } catch {
    // 忽略存储不可用，回调后退回首页。
  }
  window.location.href = loginUrl;
}

// 仅开发调试：jwt 模式没有浏览器登录流时，手动粘贴 Bearer token。
async function applyPastedToken() {
  const token = tokenInput.value.trim();
  if (!token || tokenBusy.value) return;
  tokenBusy.value = true;
  error.value = "";
  try {
    setAuthToken(token);
    tokenInput.value = "";
    await loadContext();
  } finally {
    tokenBusy.value = false;
  }
}

async function enterDashboard() {
  const target = nextTarget();
  clearStoredNext();
  await router.push(target);
}

onMounted(async () => {
  if (isCallback.value) {
    await handleCallback();
    return;
  }
  await loadContext();
});
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">账号</p>
        <h2>登录</h2>
        <p class="lede">Auditrail 评测看板登录入口。token 仅保存在当前标签页的 sessionStorage 中，关闭标签页即失效。</p>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">{{ error }}</p>
    <p v-if="loading" class="banner loading">正在获取登录状态…</p>

    <template v-if="!loading && authContext">
      <section class="panel platform-panel" aria-label="当前身份">
        <div class="panel-head">
          <h3>当前身份</h3>
          <span class="chip quiet">{{ authModeLabel }}</span>
        </div>
        <p class="hint">{{ authContext.note }}</p>
        <p v-if="authenticated" class="hint">
          当前身份：<strong>{{ displayName }}</strong>
          <span v-if="authContext.role" class="chip" data-status="running">{{ authContext.role }}</span>
          <span v-else class="chip quiet">未分配角色</span>
        </p>
      </section>

      <section v-if="isLocalLike" class="panel platform-panel" aria-label="本地模式">
        <div class="panel-head">
          <h3>本地模式</h3>
        </div>
        <p class="hint">本地模式无需登录，所有操作按本地身份放行；不要将该部署直接暴露到公网。</p>
        <p class="hint">
          <button class="ghost" type="button" @click="enterDashboard">进入看板</button>
        </p>
      </section>

      <section v-else-if="isSsoMode && !authenticated" class="panel platform-panel" aria-label="登录方式">
        <div class="panel-head">
          <h3>登录方式</h3>
        </div>
        <template v-if="isOidc && authContext.login_url">
          <p class="hint">使用组织单点登录（SSO）。点击后跳转到身份提供方，登录成功自动返回看板。</p>
          <p class="hint">
            <button class="ghost" type="button" @click="startSsoLogin">SSO 登录</button>
          </p>
        </template>
        <details class="token-fallback">
          <summary>粘贴 Bearer token（仅开发调试）</summary>
          <p class="hint">仅用于开发调试：token 会以 Bearer 方式随请求发送，不要在此粘贴生产高权限 token。</p>
          <p class="hint token-row">
            <input
              v-model="tokenInput"
              type="password"
              placeholder="Bearer token"
              aria-label="Bearer token"
              @keydown.enter="applyPastedToken"
            />
            <button class="ghost small" type="button" :disabled="!tokenInput.trim() || tokenBusy" @click="applyPastedToken">
              {{ tokenBusy ? "校验中…" : "使用 token" }}
            </button>
          </p>
        </details>
      </section>

      <section v-else-if="authenticated" class="panel platform-panel" aria-label="已登录">
        <div class="panel-head">
          <h3>已登录</h3>
        </div>
        <p class="hint">
          已认证为 <strong>{{ displayName }}</strong
          >，可以继续使用看板。
        </p>
        <p class="hint">
          <button class="ghost" type="button" @click="enterDashboard">进入看板</button>
        </p>
      </section>

      <p v-if="callbackFailed" class="hint">
        <router-link class="back-link" :to="{ name: 'login' }">返回登录页</router-link>
      </p>
    </template>
  </div>
</template>

<style scoped>
.token-fallback {
  margin-top: 8px;
}
.token-fallback summary {
  cursor: pointer;
  color: var(--muted, #64748b);
  font-size: 13px;
}
.token-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.token-row input {
  flex: 1;
  max-width: 360px;
  padding: 6px 10px;
  border: 1px solid rgba(148, 163, 184, 0.4);
  border-radius: 8px;
  background: transparent;
  color: inherit;
}
</style>
