<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  fetchAgentDirectory,
  type AgentDirectoryPayload,
  type AgentListItem,
  type DerivedAgentSummary
} from "../api";

const router = useRouter();

const payload = ref<AgentDirectoryPayload | null>(null);
const loading = ref(false);
const error = ref("");

// 统一行模型：注册 Agent 与派生 Agent 合并展示；派生 Agent 必须标注「来源于运行数据」。
interface AgentRow {
  agent_id: string;
  name: string;
  type: string;
  environment: string;
  derived: boolean;
  run_count: number;
  last_run_at: string | null;
  last_run_status: string | null;
  created_at: string | null;
}

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  timed_out: "超时",
  cancelled: "已取消",
  skipped: "已跳过",
  stale: "已中断"
};

function runStatusLabel(status: string | null): string {
  if (!status) return "—";
  return RUN_STATUS_LABELS[status] || status;
}

function fromConfig(agent: AgentListItem): AgentRow {
  // Phase 2：environment 优先读顶层正式字段，兼容旧配置的 metadata.environment。
  const legacyEnv =
    agent.metadata && typeof agent.metadata.environment === "string" ? agent.metadata.environment : "";
  return {
    agent_id: agent.agent_id,
    name: agent.name || agent.agent_id,
    type: agent.adapter || "—",
    environment: agent.environment || legacyEnv || "—",
    derived: false,
    run_count: agent.stats?.run_count ?? 0,
    last_run_at: agent.stats?.last_run_at ?? null,
    last_run_status: agent.stats?.last_run_status ?? null,
    created_at: agent.created_at ?? null
  };
}

function fromDerived(agent: DerivedAgentSummary): AgentRow {
  return {
    agent_id: agent.agent_id,
    name: agent.name || agent.agent_id,
    type: "派生",
    environment: agent.source_host || "—",
    derived: true,
    run_count: agent.stats?.run_count ?? 0,
    last_run_at: agent.stats?.last_run_at ?? null,
    last_run_status: agent.stats?.last_run_status ?? null,
    created_at: null
  };
}

const rows = computed<AgentRow[]>(() => {
  const data = payload.value;
  if (!data) return [];
  return [...(data.agents || []).map(fromConfig), ...(data.derived_agents || []).map(fromDerived)];
});

const partialWarning = computed(() => {
  const data = payload.value;
  if (!data) return "";
  if (data.partial || (data.errors && data.errors.length)) {
    const first = data.errors?.[0];
    const suffix = first ? `：${first.stage} 阶段 — ${first.message}` : "";
    return `部分运行数据扫描失败，当前展示为局部数据${suffix}`;
  }
  return "";
});

// Phase 2：索引不可用时明确提示当前为扫描结果，不伪装成索引加速。
const indexHint = computed(() => {
  const data = payload.value;
  if (!data || !data.index) return "";
  if (data.index.used) {
    const count = data.index.indexed_count ?? 0;
    return `运行索引就绪（已索引 ${count} 个 Run）`;
  }
  return "索引不可用，当前使用扫描结果";
});

function openAgent(agentId: string) {
  void router.push({ name: "agent-detail", params: { agentId } });
}

function formatTime(value: string | null): string {
  return value || "—";
}

async function loadAgents() {
  loading.value = true;
  error.value = "";
  try {
    payload.value = await fetchAgentDirectory();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

onMounted(loadAgents);
</script>

<template>
  <div class="page agent-page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">Agent 工作区</p>
        <h2 class="run-title">Agent 列表</h2>
        <p class="lede">
          注册的 Agent 与仅从运行数据派生的 Agent 汇总视图。点击任意行进入 Agent 详情，查看 Models、Tools、趋势与 Auditrail Health。
        </p>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">{{ error }}</p>
    <p v-else-if="partialWarning" class="banner error" role="status">{{ partialWarning }}</p>
    <p v-if="indexHint" class="banner quiet" role="status">{{ indexHint }}</p>
    <p v-if="loading" class="banner loading">加载 Agent 列表中…</p>

    <section class="panel agent-panel" aria-label="Agent 列表">
      <div class="panel-head">
        <h3>Agents</h3>
        <span class="chip quiet">{{ rows.length }}</span>
      </div>
      <table v-if="rows.length" class="run-table agent-table">
        <thead>
          <tr>
            <th>名称</th>
            <th>类型</th>
            <th>环境</th>
            <th>创建时间</th>
            <th>最近运行时间</th>
            <th>最近状态</th>
            <th>Run 数量</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in rows"
            :key="row.agent_id"
            tabindex="0"
            :aria-label="`打开 Agent ${row.name} 详情`"
            @click="openAgent(row.agent_id)"
            @keydown.enter.prevent="openAgent(row.agent_id)"
            @keydown.space.prevent="openAgent(row.agent_id)"
          >
            <td>
              <strong>{{ row.name }}</strong>
              <span v-if="row.derived" class="chip quiet derived-badge">来源于运行数据</span>
              <br />
              <span class="mono">{{ row.agent_id }}</span>
            </td>
            <td>{{ row.type }}</td>
            <td>{{ row.environment }}</td>
            <td>{{ formatTime(row.created_at) }}</td>
            <td>{{ formatTime(row.last_run_at) }}</td>
            <td>
              <span v-if="row.last_run_status" class="chip" :data-status="row.last_run_status">
                {{ runStatusLabel(row.last_run_status) }}
              </span>
              <span v-else>—</span>
            </td>
            <td>{{ row.run_count }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else-if="!loading && !error" class="empty">
        暂无 Agent。可在测试平台注册 Agent，或先导入运行数据生成派生 Agent。
      </p>
    </section>
  </div>
</template>
