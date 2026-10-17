<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  createAgent,
  createPlatformRun,
  createTestCase,
  fetchAgents,
  fetchRunStatus,
  fetchTestCases,
  type AgentConfig,
  type RunStatusPayload,
  type TestCase
} from "../api";

const router = useRouter();

const agents = ref<AgentConfig[]>([]);
const testCases = ref<TestCase[]>([]);
const testCaseErrors = ref<Array<{ case_id: string; error: string }>>([]);
const loading = ref(false);
const error = ref("");

// 新建 Agent 表单
const agentForm = ref({
  agent_id: "",
  name: "",
  version: "",
  endpoint: "",
  capabilities: "session,chat"
});
const agentBusy = ref(false);

// 新建 TestCase 表单（http_session 适配器）
const caseForm = ref({
  case_id: "",
  title: "",
  user_id: "",
  message: "",
  session_id: "",
  timeout_seconds: 120
});
const caseBusy = ref(false);

// 发起运行
const selectedAgentId = ref("");
const selectedCaseId = ref("");
const judgeEnabled = ref(false);
const idempotencyKey = ref("");
const launchBusy = ref(false);
const lastRun = ref<RunStatusPayload | null>(null);
let pollTimer: ReturnType<typeof setInterval> | null = null;

const runnableCases = computed(() => testCases.value.filter((item) => item.adapter === "http_session" && !item.legacy));
const legacyCases = computed(() => testCases.value.filter((item) => item.legacy));
const selectedCase = computed(() => testCases.value.find((item) => item.case_id === selectedCaseId.value) || null);

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  timed_out: "超时",
  cancelled: "已取消",
  skipped: "已跳过"
};

function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] || status || "未知";
}

async function loadAll() {
  loading.value = true;
  error.value = "";
  try {
    const [agentData, caseData] = await Promise.all([fetchAgents(), fetchTestCases()]);
    agents.value = agentData.agents;
    testCases.value = caseData.test_cases;
    testCaseErrors.value = caseData.errors;
    if (!selectedAgentId.value && agents.value.length) selectedAgentId.value = agents.value[0].agent_id;
    if (!selectedCaseId.value && runnableCases.value.length) selectedCaseId.value = runnableCases.value[0].case_id;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

async function submitAgent() {
  agentBusy.value = true;
  error.value = "";
  try {
    await createAgent({
      agent_id: agentForm.value.agent_id,
      name: agentForm.value.name || agentForm.value.agent_id,
      version: agentForm.value.version,
      adapter: "http_session",
      endpoint: agentForm.value.endpoint,
      capabilities: agentForm.value.capabilities.split(",").map((item) => item.trim()).filter(Boolean),
      metadata: {}
    });
    agentForm.value = { agent_id: "", name: "", version: "", endpoint: "", capabilities: "session,chat" };
    await loadAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    agentBusy.value = false;
  }
}

async function submitCase() {
  caseBusy.value = true;
  error.value = "";
  try {
    const input: Record<string, unknown> = { user_id: caseForm.value.user_id };
    if (caseForm.value.message.trim()) input.message = caseForm.value.message;
    if (caseForm.value.session_id.trim()) input.session_id = caseForm.value.session_id.trim();
    await createTestCase({
      case_id: caseForm.value.case_id,
      title: caseForm.value.title,
      adapter: "http_session",
      input,
      expectations: [{ id: "e1", type: "final_answer_present", required: true }],
      timeout_seconds: caseForm.value.timeout_seconds
    });
    caseForm.value = { case_id: "", title: "", user_id: "", message: "", session_id: "", timeout_seconds: 120 };
    await loadAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    caseBusy.value = false;
  }
}

async function launchRun() {
  if (!selectedAgentId.value || !selectedCaseId.value) return;
  launchBusy.value = true;
  error.value = "";
  stopPolling();
  try {
    const result = await createPlatformRun(
      { agent_id: selectedAgentId.value, test_case_id: selectedCaseId.value, judge: judgeEnabled.value },
      idempotencyKey.value.trim() || undefined
    );
    lastRun.value = await fetchRunStatus(result.run_id);
    startPolling(result.run_id);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    launchBusy.value = false;
  }
}

function startPolling(runId: string) {
  pollTimer = setInterval(async () => {
    try {
      const status = await fetchRunStatus(runId);
      lastRun.value = status;
      if (!["queued", "running"].includes(String(status.status))) {
        stopPolling();
      }
    } catch {
      stopPolling();
    }
  }, 1000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function openRun(runId: string) {
  void router.push({ name: "run", params: { runId } });
}

onMounted(loadAll);
</script>

<template>
  <div class="page platform-page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">Agent 测试平台</p>
        <h2 class="run-title">单次运行</h2>
        <p class="lede">配置一个 HTTP/SSE Agent，选择或创建统一 TestCase，发起一次异步单 Case 运行。</p>
      </div>
    </header>

    <p v-if="error" class="banner error">{{ error }}</p>

    <section class="panel platform-panel" aria-label="Agents">
      <div class="panel-head">
        <h3>Agents</h3>
        <span class="chip quiet">{{ agents.length }}</span>
      </div>
      <table v-if="agents.length" class="platform-table">
        <thead>
          <tr><th>ID</th><th>名称</th><th>版本</th><th>Endpoint</th><th>能力</th></tr>
        </thead>
        <tbody>
          <tr v-for="agent in agents" :key="agent.agent_id">
            <td><code>{{ agent.agent_id }}</code></td>
            <td>{{ agent.name }}</td>
            <td>{{ agent.version || "—" }}</td>
            <td><code>{{ agent.endpoint }}</code></td>
            <td>{{ (agent.capabilities || []).join(", ") }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty">尚未配置 Agent。</p>
      <form class="platform-form" @submit.prevent="submitAgent">
        <input v-model="agentForm.agent_id" placeholder="agent_id（如 xuanjian-local）" required />
        <input v-model="agentForm.name" placeholder="名称（可选）" />
        <input v-model="agentForm.version" placeholder="版本（如 git:abc123，可选）" />
        <input v-model="agentForm.endpoint" placeholder="endpoint（http(s)://host:port）" required />
        <input v-model="agentForm.capabilities" placeholder="能力（逗号分隔：session,chat）" />
        <button class="primary" type="submit" :disabled="agentBusy">{{ agentBusy ? "保存中…" : "保存 Agent" }}</button>
      </form>
      <p class="hint">endpoint 会经过 SSRF 校验：仅 http/https，默认拒绝 loopback、私网、link-local 与云 metadata 地址。</p>
    </section>

    <section class="panel platform-panel" aria-label="TestCases">
      <div class="panel-head">
        <h3>Test Cases</h3>
        <span class="chip quiet">{{ runnableCases.length }} 个可运行</span>
      </div>
      <table v-if="runnableCases.length" class="platform-table">
        <thead>
          <tr><th>ID</th><th>标题</th><th>适配器</th><th>超时(s)</th><th>验收标准</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in runnableCases" :key="item.case_id">
            <td><code>{{ item.case_id }}</code></td>
            <td>{{ item.title }}</td>
            <td>{{ item.adapter }}</td>
            <td>{{ item.timeout_seconds }}</td>
            <td>{{ (item.expectations || []).map((e) => e.type).join(", ") || "—" }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty">尚无平台 TestCase。</p>
      <form class="platform-form" @submit.prevent="submitCase">
        <input v-model="caseForm.case_id" placeholder="case_id" required />
        <input v-model="caseForm.title" placeholder="标题" required />
        <input v-model="caseForm.user_id" placeholder="user_id" required />
        <input v-model="caseForm.message" placeholder="消息（chat 模式；留空则用 session_id 抓取）" />
        <input v-model="caseForm.session_id" placeholder="session_id（fetch 模式，可选）" />
        <input v-model.number="caseForm.timeout_seconds" type="number" min="1" placeholder="超时秒数" />
        <button class="primary" type="submit" :disabled="caseBusy">{{ caseBusy ? "保存中…" : "保存 TestCase" }}</button>
      </form>
      <details v-if="legacyCases.length" class="legacy-cases">
        <summary>旧格式命令用例（adapter=command，仅 CLI 执行，共 {{ legacyCases.length }} 个）</summary>
        <ul>
          <li v-for="item in legacyCases" :key="item.case_id"><code>{{ item.case_id }}</code> {{ item.title }}</li>
        </ul>
      </details>
      <p v-for="item in testCaseErrors" :key="item.case_id" class="banner error">用例 {{ item.case_id }} 无效：{{ item.error }}</p>
    </section>

    <section class="panel platform-panel" aria-label="发起运行">
      <div class="panel-head">
        <h3>发起运行</h3>
      </div>
      <div class="platform-form">
        <select v-model="selectedAgentId" aria-label="选择 Agent">
          <option v-for="agent in agents" :key="agent.agent_id" :value="agent.agent_id">
            {{ agent.name }}（{{ agent.endpoint }}）
          </option>
        </select>
        <select v-model="selectedCaseId" aria-label="选择 TestCase">
          <option v-for="item in runnableCases" :key="item.case_id" :value="item.case_id">
            {{ item.case_id }} — {{ item.title }}
          </option>
        </select>
        <label class="judge-toggle">
          <input v-model="judgeEnabled" type="checkbox" />
          运行后执行 LLM Judge（需要模型凭据；失败不影响运行产物）
        </label>
        <input v-model="idempotencyKey" placeholder="Idempotency-Key（可选，防止重复提交）" />
        <button
          class="primary"
          type="button"
          :disabled="launchBusy || !selectedAgentId || !selectedCaseId"
          @click="launchRun"
        >
          {{ launchBusy ? "提交中…" : "发起单次运行" }}
        </button>
      </div>

      <div v-if="lastRun" class="run-status-card" :data-status="lastRun.status">
        <p>
          <strong>{{ lastRun.run_id }}</strong>
          <span class="chip" :data-status="lastRun.status">{{ runStatusLabel(String(lastRun.status)) }}</span>
          <span v-if="lastRun.stale" class="chip quiet">执行器已中断（stale）</span>
        </p>
        <p v-if="lastRun.status_reason" class="hint">原因：{{ lastRun.status_reason }}</p>
        <p v-if="lastRun.error" class="hint">
          错误：{{ lastRun.error.error_type }}（阶段 {{ lastRun.error.phase }}）— {{ lastRun.error.safe_message }}
        </p>
        <p class="hint">产物：{{ (lastRun.artifact_manifest || []).join(", ") || "暂无" }}</p>
        <button
          v-if="['completed', 'failed', 'timed_out'].includes(String(lastRun.status))"
          class="ghost"
          type="button"
          @click="openRun(lastRun.run_id)"
        >
          打开运行详情
        </button>
      </div>
    </section>
  </div>
</template>
