<script setup lang="ts">
import { computed, inject, onMounted, ref, watch, type Ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { apiGet, type RunItem, type TrajectoryMessage } from "../api";

const route = useRoute();
const router = useRouter();
const runs = inject<Ref<RunItem[]>>("runs")!;
const reloadRuns = inject<() => Promise<void>>("reloadRuns")!;

const messages = ref<TrajectoryMessage[]>([]);
const loading = ref(false);
const error = ref("");
const activeRole = ref("all");
const expandedReasoning = ref<string[]>([]);

const runId = computed(() => String(route.params.runId || ""));
const selectedMeta = computed(() => runs.value.find((item) => item.name === runId.value));
const serverHost = computed(() => {
  const host = selectedMeta.value?.server_host;
  return typeof host === "string" && host ? host : "未知来源";
});

const roleFilters = [
  { value: "all", label: "全部" },
  { value: "user", label: "用户" },
  { value: "assistant", label: "助手" },
  { value: "tool", label: "工具" }
];

const counts = computed(() => ({
  total: messages.value.length,
  user: messages.value.filter((item) => item.role === "user").length,
  assistant: messages.value.filter((item) => item.role === "assistant").length,
  tool: messages.value.filter((item) => item.role === "tool").length
}));

const filteredMessages = computed(() => {
  if (activeRole.value === "all") return messages.value;
  return messages.value.filter((item) => item.role === activeRole.value);
});

const statsText = computed(() => {
  const c = counts.value;
  return `共 ${c.total} 条消息 · 用户 ${c.user} · 助手 ${c.assistant} · 工具 ${c.tool}`;
});

const ROLE_LABELS: Record<string, string> = {
  user: "用户",
  assistant: "助手",
  tool: "工具",
  system: "系统"
};

function roleLabel(role: string) {
  return ROLE_LABELS[role] || role || "消息";
}

function countForRole(role: string) {
  if (role === "all") return counts.value.total;
  if (role === "user") return counts.value.user;
  if (role === "assistant") return counts.value.assistant;
  if (role === "tool") return counts.value.tool;
  return 0;
}

function kindLabel(message: TrajectoryMessage) {
  if (message.kind === "assistant_tool_request") return "请求工具调用";
  if (message.kind === "tool_result") return "工具结果";
  if (message.kind === "assistant_message") return "回复";
  if (message.kind === "user_message") return "提问";
  return "";
}

function messageKey(message: TrajectoryMessage) {
  return String(message.message_id || message.message_uuid || message.index);
}

function reasoningText(message: TrajectoryMessage) {
  const text = message.reasoning || message.reasoning_content || "";
  return typeof text === "string" ? text.trim() : "";
}

function toolCallName(call: any) {
  const fn = call?.function;
  if (fn && typeof fn.name === "string") return fn.name;
  if (typeof call?.name === "string") return call.name;
  return "tool_call";
}

function toolCallArguments(call: any) {
  const fn = call?.function;
  const raw = fn && typeof fn.arguments === "string" ? fn.arguments : call?.arguments;
  return prettyJson(raw) || (typeof raw === "string" ? raw : "");
}

function prettyJson(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  try {
    return JSON.stringify(JSON.parse(trimmed), null, 2);
  } catch {
    return null;
  }
}

function contentText(message: TrajectoryMessage) {
  const raw = message.content || "";
  if (message.role === "tool" || message.kind === "tool_result") {
    return prettyJson(raw) || raw;
  }
  return raw;
}

function formatTimestamp(value: string | number | undefined) {
  if (value === undefined || value === null || value === "") return "-";
  const num = Number(value);
  if (Number.isFinite(num) && num > 0) {
    const ms = num < 1e11 ? num * 1000 : num;
    return new Date(ms).toLocaleString([], {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit"
    });
  }
  return String(value);
}

function isReasoningExpanded(message: TrajectoryMessage) {
  return expandedReasoning.value.includes(messageKey(message));
}

function toggleReasoning(message: TrajectoryMessage) {
  const key = messageKey(message);
  const current = new Set(expandedReasoning.value);
  if (current.has(key)) current.delete(key);
  else current.add(key);
  expandedReasoning.value = Array.from(current);
}

async function loadTrajectory(id: string) {
  if (!id) return;
  error.value = "";
  loading.value = true;
  try {
    const data = await apiGet<{ run_id: string; messages: TrajectoryMessage[] }>(`/api/runs/${id}/trajectory`);
    messages.value = data.messages || [];
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    messages.value = [];
  } finally {
    loading.value = false;
  }
}

watch(
  runId,
  (id) => {
    void loadTrajectory(id);
  },
  { immediate: true }
);

onMounted(() => {
  if (!runs.value.length) void reloadRuns();
});
</script>

<template>
  <div class="page run-page">
    <header class="page-hero">
      <div class="hero-copy">
        <button class="back-link" type="button" @click="router.push({ name: 'run', params: { runId } })">
          ← 返回单次评测
        </button>
        <p class="eyebrow">Trajectory · {{ serverHost }}</p>
        <h2 class="run-title">轨迹 · {{ runId || "暂无运行" }}</h2>
        <p class="lede">{{ statsText }}</p>
      </div>
      <div class="hero-actions">
        <button class="ghost" type="button" :disabled="!runId || loading" @click="loadTrajectory(runId)">
          {{ loading ? "加载中" : "重新加载" }}
        </button>
        <button class="primary" type="button" :disabled="!runId" @click="router.push({ name: 'run', params: { runId } })">
          查看评测
        </button>
      </div>
    </header>

    <p v-if="error" class="banner error">{{ error }}</p>
    <p v-if="loading" class="banner loading">正在读取轨迹数据</p>

    <section v-if="!loading || messages.length" class="ops-bar reveal" aria-label="Trajectory filters">
      <div class="ops-left">
        <button
          v-for="filter in roleFilters"
          :key="filter.value"
          class="filter-pill"
          :class="{ active: activeRole === filter.value }"
          type="button"
          @click="activeRole = filter.value"
        >
          {{ filter.label }}
          <span class="filter-count">{{ countForRole(filter.value) }}</span>
        </button>
      </div>
      <div class="ops-right">
        <span class="context-token">run: {{ runId }}</span>
        <span class="context-token">messages: {{ counts.total }}</span>
      </div>
    </section>

    <section class="trajectory-stream reveal" aria-label="Trajectory messages">
      <article
        v-for="message in filteredMessages"
        :key="messageKey(message)"
        class="traj-row"
        :data-role="message.role"
      >
        <header class="traj-row-head">
          <span class="traj-role" :data-role="message.role">{{ roleLabel(message.role) }}</span>
          <span v-if="kindLabel(message)" class="traj-kind">{{ kindLabel(message) }}</span>
          <time class="traj-time">{{ formatTimestamp(message.timestamp) }}</time>
          <span class="traj-index">#{{ message.index }}</span>
        </header>

        <div class="traj-body">
          <button
            v-if="reasoningText(message)"
            class="traj-reasoning-toggle"
            type="button"
            @click="toggleReasoning(message)"
          >
            <span class="chevron" :class="{ closed: !isReasoningExpanded(message) }">▾</span>
            <span>思考过程</span>
            <small v-if="!isReasoningExpanded(message)">{{ reasoningText(message).slice(0, 120) }}…</small>
          </button>
          <pre v-if="reasoningText(message) && isReasoningExpanded(message)" class="traj-reasoning">{{ reasoningText(message) }}</pre>

          <div v-if="message.role === 'tool'" class="traj-tool-result">
            <span class="traj-tool-name">{{ message.tool_name || "tool" }}</span>
            <span v-if="message.tool_call_id" class="traj-tool-id">{{ message.tool_call_id }}</span>
          </div>

          <pre v-if="contentText(message)" class="traj-content">{{ contentText(message) }}</pre>

          <div v-if="message.tool_calls && message.tool_calls.length" class="traj-tool-calls">
            <div v-for="(call, callIndex) in message.tool_calls" :key="`${messageKey(message)}-call-${callIndex}`" class="traj-tool-call">
              <div class="traj-tool-call-head">
                <span class="traj-tool-name">{{ toolCallName(call) }}</span>
                <span v-if="call.id || call.call_id" class="traj-tool-id">{{ call.id || call.call_id }}</span>
              </div>
              <pre v-if="toolCallArguments(call)" class="traj-tool-args">{{ toolCallArguments(call) }}</pre>
            </div>
          </div>
        </div>
      </article>

      <p v-if="!loading && !messages.length" class="empty traj-empty">暂无轨迹数据。</p>
      <p v-else-if="!loading && !filteredMessages.length" class="empty traj-empty">当前筛选下没有消息。</p>
    </section>
  </div>
</template>

<style scoped>
.filter-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  cursor: pointer;
}

.filter-count {
  opacity: 0.75;
  font-size: 0.72rem;
}

.trajectory-stream {
  display: grid;
  gap: 12px;
}

.traj-row {
  position: relative;
  display: grid;
  gap: 10px;
  padding: 14px 16px;
  border-radius: var(--radius);
  background: var(--surface);
  box-shadow: var(--shadow), inset 0 0 0 1px rgba(197, 208, 219, 0.7);
  backdrop-filter: blur(10px);
}

.traj-row::before {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 4px;
  border-radius: var(--radius) 0 0 var(--radius);
  background: var(--muted);
}

.traj-row[data-role="user"]::before {
  background: var(--spark);
}

.traj-row[data-role="assistant"]::before {
  background: var(--signal);
}

.traj-row[data-role="tool"]::before {
  background: #e39b2f;
}

.traj-row-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.traj-role {
  padding: 3px 10px;
  border-radius: 999px;
  color: #fff;
  font-size: 0.74rem;
  font-weight: 750;
  background: var(--muted);
}

.traj-role[data-role="user"] {
  background: var(--spark);
}

.traj-role[data-role="assistant"] {
  background: var(--signal);
}

.traj-role[data-role="tool"] {
  background: #c98a1f;
}

.traj-kind {
  color: var(--muted);
  font-size: 0.74rem;
  font-weight: 650;
}

.traj-time {
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.72rem;
}

.traj-index {
  margin-left: auto;
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.72rem;
}

.traj-body {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.traj-reasoning-toggle {
  display: grid;
  grid-template-columns: auto auto minmax(0, 1fr);
  gap: 8px;
  align-items: baseline;
  min-width: 0;
  padding: 8px 10px;
  border: 0;
  border-radius: 8px;
  color: var(--ink);
  background: rgba(20, 32, 51, 0.05);
  text-align: left;
  cursor: pointer;
}

.traj-reasoning-toggle:hover {
  background: rgba(20, 32, 51, 0.08);
}

.traj-reasoning-toggle small {
  overflow: hidden;
  color: var(--muted);
  font-size: 0.74rem;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.traj-reasoning,
.traj-content,
.traj-tool-args {
  margin: 0;
  padding: 12px;
  overflow: auto;
  border-radius: 8px;
  color: #243449;
  background: #eef4f7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-family: var(--font-mono);
  font-size: 0.78rem;
  line-height: 1.55;
}

.traj-reasoning {
  max-height: 300px;
  color: #4a3d1f;
  background: #f7f1e3;
}

.traj-content {
  background: #fff;
  box-shadow: inset 0 0 0 1px rgba(197, 208, 219, 0.65);
  font-family: var(--font-body);
  font-size: 0.88rem;
}

.traj-tool-calls {
  display: grid;
  gap: 8px;
}

.traj-tool-call,
.traj-tool-result {
  display: grid;
  gap: 8px;
  padding: 10px 12px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.72);
  box-shadow: inset 0 0 0 1px rgba(197, 208, 219, 0.65);
}

.traj-tool-call-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.traj-tool-name {
  width: fit-content;
  padding: 3px 9px;
  border-radius: 999px;
  color: #174ea6;
  background: #e9f1ff;
  font-family: var(--font-mono);
  font-size: 0.74rem;
  font-weight: 750;
}

.traj-tool-id {
  color: var(--muted);
  font-family: var(--font-mono);
  font-size: 0.72rem;
}

.traj-tool-args {
  max-height: 260px;
  background: #eef4f7;
}

.traj-tool-result {
  display: flex;
  align-items: center;
  gap: 8px;
}

.traj-empty {
  padding: 40px 0;
  text-align: center;
}
</style>
