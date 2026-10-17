<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  cancelSuiteJob,
  createAgent,
  createPlatformRun,
  createTestCase,
  createTestSuite,
  deleteAgent,
  deleteTestSuite,
  fetchAgents,
  fetchRunStatus,
  fetchSuiteJob,
  fetchSuiteJobs,
  fetchTestCases,
  fetchTestSuites,
  launchSuiteJob,
  recoverStaleSuiteJobs,
  updateAgent,
  updateTestSuite,
  type AgentConfig,
  type RunStatusPayload,
  type SuiteJob,
  type TestCase,
  type TestSuite
} from "../api";

const router = useRouter();
const route = useRoute();

const agents = ref<AgentConfig[]>([]);
const testCases = ref<TestCase[]>([]);
const testCaseErrors = ref<Array<{ case_id: string; error: string }>>([]);
const suites = ref<TestSuite[]>([]);
const suiteJobs = ref<SuiteJob[]>([]);
const loading = ref(false);
const error = ref("");

// 新建 / 编辑 Agent 表单（Phase 2：environment/description/tags 正式元数据）
const agentForm = ref({
  agent_id: "",
  name: "",
  version: "",
  endpoint: "",
  capabilities: "session,chat",
  environment: "",
  description: "",
  tags: ""
});
const editingAgentId = ref("");
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

// 新建 / 编辑 TestSuite 表单（case_ids 的顺序即批量执行顺序）
const suiteForm = ref({
  suite_id: "",
  title: "",
  description: "",
  case_ids: [] as string[],
  enabled: true
});
const editingSuiteId = ref("");
const suiteBusy = ref(false);

// 发起运行
const selectedAgentId = ref("");
const selectedCaseId = ref("");
const judgeEnabled = ref(false);
const idempotencyKey = ref("");
const launchBusy = ref(false);
const lastRun = ref<RunStatusPayload | null>(null);
let pollTimer: ReturnType<typeof setInterval> | null = null;

// 发起批量运行（SuiteJob -> CaseExecution -> Attempt N -> Run）
const batchAgentId = ref("");
const batchSuiteId = ref("");
const batchJudge = ref(false);
// Phase 1.1 可靠性控制：重试、套件总超时、执行/成本预算、幂等提交
const batchMaxRetries = ref(0);
const batchTimeoutSeconds = ref<number | null>(null);
const batchBudgetAttempts = ref<number | null>(null);
const batchBudgetTokens = ref<number | null>(null);
const batchIdempotencyKey = ref("");
const batchBusy = ref(false);
const activeJob = ref<SuiteJob | null>(null);
let jobPollTimer: ReturnType<typeof setInterval> | null = null;

const runnableCases = computed(() => testCases.value.filter((item) => item.adapter === "http_session" && !item.legacy));
const legacyCases = computed(() => testCases.value.filter((item) => item.legacy));
const selectedCase = computed(() => testCases.value.find((item) => item.case_id === selectedCaseId.value) || null);
const enabledSuites = computed(() => suites.value.filter((item) => item.enabled && item.case_ids.length));
const activeJobAttempts = computed(() => activeJob.value?.attempts || []);
const hasStaleJobs = computed(() => suiteJobs.value.some((job) => job.stale));

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  timed_out: "超时",
  cancelled: "已取消",
  skipped: "已跳过"
};

const SUITE_JOB_STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消"
};

const FINAL_QUALITY_LABELS: Record<string, string> = {
  success: "通过",
  partial: "部分通过",
  failed: "未通过",
  unknown: "未评估"
};

function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] || status || "未知";
}

function jobStatusLabel(status: string): string {
  return SUITE_JOB_STATUS_LABELS[status] || status || "未知";
}

function finalQualityLabel(status: string): string {
  return FINAL_QUALITY_LABELS[status] || "未评估";
}

async function loadAll() {
  loading.value = true;
  error.value = "";
  try {
    const [agentData, caseData, suiteData, jobData] = await Promise.all([
      fetchAgents(),
      fetchTestCases(),
      fetchTestSuites(),
      fetchSuiteJobs()
    ]);
    agents.value = agentData.agents;
    testCases.value = caseData.test_cases;
    testCaseErrors.value = caseData.errors;
    suites.value = suiteData.test_suites;
    suiteJobs.value = jobData.suite_jobs;
    if (!selectedAgentId.value && agents.value.length) selectedAgentId.value = agents.value[0].agent_id;
    if (!selectedCaseId.value && runnableCases.value.length) selectedCaseId.value = runnableCases.value[0].case_id;
    if (!batchAgentId.value && agents.value.length) batchAgentId.value = agents.value[0].agent_id;
    if (!batchSuiteId.value && enabledSuites.value.length) batchSuiteId.value = enabledSuites.value[0].suite_id;
    if (!activeJob.value && suiteJobs.value.length) activeJob.value = suiteJobs.value[0];
    // 从 Agent 工作区跳转过来时（?job=...）直接定位到对应 SuiteJob。
    const queryJob = String(route.query.job || "");
    if (queryJob) await selectJob(queryJob);
    const current = activeJob.value;
    if (current && ["queued", "running"].includes(String(current.status))) startJobPolling(current.job_id);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    loading.value = false;
  }
}

function resetAgentForm() {
  agentForm.value = {
    agent_id: "",
    name: "",
    version: "",
    endpoint: "",
    capabilities: "session,chat",
    environment: "",
    description: "",
    tags: ""
  };
  editingAgentId.value = "";
}

function editAgent(agent: AgentConfig) {
  editingAgentId.value = agent.agent_id;
  agentForm.value = {
    agent_id: agent.agent_id,
    name: agent.name,
    version: agent.version || "",
    endpoint: agent.endpoint,
    capabilities: (agent.capabilities || []).join(","),
    environment: agent.environment || "",
    description: agent.description || "",
    tags: (agent.tags || []).join(",")
  };
}

async function submitAgent() {
  agentBusy.value = true;
  error.value = "";
  try {
    const payload: Partial<AgentConfig> = {
      agent_id: agentForm.value.agent_id,
      name: agentForm.value.name || agentForm.value.agent_id,
      version: agentForm.value.version,
      adapter: "http_session",
      endpoint: agentForm.value.endpoint,
      capabilities: agentForm.value.capabilities.split(",").map((item) => item.trim()).filter(Boolean),
      // 空字符串提交为 null，不默认成 production；自定义值原样保留。
      environment: agentForm.value.environment.trim() || null,
      description: agentForm.value.description,
      tags: agentForm.value.tags.split(",").map((item) => item.trim()).filter(Boolean),
      metadata: {}
    };
    if (editingAgentId.value) {
      // 更新不影响历史 Run 的 agent_ref 快照，created_at 保持不变。
      await updateAgent(editingAgentId.value, payload);
    } else {
      await createAgent(payload);
    }
    resetAgentForm();
    await loadAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    agentBusy.value = false;
  }
}

// 删除 Agent 配置：历史 Run 保留，之后以 missing_configuration/derived 展示。
async function removeAgent(agent: AgentConfig) {
  error.value = "";
  try {
    await deleteAgent(agent.agent_id);
    if (editingAgentId.value === agent.agent_id) resetAgentForm();
    await loadAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
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

// --- TestSuite CRUD -----------------------------------------------------------

function toggleSuiteCase(caseId: string) {
  const ids = suiteForm.value.case_ids;
  const index = ids.indexOf(caseId);
  if (index >= 0) {
    ids.splice(index, 1);
  } else {
    ids.push(caseId);
  }
}

function resetSuiteForm() {
  suiteForm.value = { suite_id: "", title: "", description: "", case_ids: [], enabled: true };
  editingSuiteId.value = "";
}

function editSuite(suite: TestSuite) {
  editingSuiteId.value = suite.suite_id;
  suiteForm.value = {
    suite_id: suite.suite_id,
    title: suite.title,
    description: suite.description,
    case_ids: [...suite.case_ids],
    enabled: suite.enabled
  };
}

async function submitSuite() {
  suiteBusy.value = true;
  error.value = "";
  try {
    const payload = {
      suite_id: suiteForm.value.suite_id,
      title: suiteForm.value.title,
      description: suiteForm.value.description,
      case_ids: [...suiteForm.value.case_ids],
      enabled: suiteForm.value.enabled
    };
    if (editingSuiteId.value) {
      await updateTestSuite(editingSuiteId.value, payload);
    } else {
      await createTestSuite(payload);
    }
    resetSuiteForm();
    await loadAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    suiteBusy.value = false;
  }
}

async function removeSuite(suite: TestSuite) {
  error.value = "";
  try {
    await deleteTestSuite(suite.suite_id);
    if (editingSuiteId.value === suite.suite_id) resetSuiteForm();
    await loadAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

// --- SuiteJob 批量运行 ----------------------------------------------------------

async function launchSuite() {
  if (!batchAgentId.value || !batchSuiteId.value) return;
  batchBusy.value = true;
  error.value = "";
  stopJobPolling();
  try {
    const budget: { max_attempts?: number | null; max_total_tokens?: number | null } = {};
    if (batchBudgetAttempts.value) budget.max_attempts = batchBudgetAttempts.value;
    if (batchBudgetTokens.value) budget.max_total_tokens = batchBudgetTokens.value;
    const result = await launchSuiteJob(
      batchSuiteId.value,
      {
        agent_id: batchAgentId.value,
        judge: batchJudge.value,
        max_retries: batchMaxRetries.value || 0,
        timeout_seconds: batchTimeoutSeconds.value || null,
        ...(Object.keys(budget).length ? { budget } : {})
      },
      batchIdempotencyKey.value.trim() || undefined
    );
    const payload = await fetchSuiteJob(result.job_id);
    activeJob.value = payload.suite_job;
    startJobPolling(result.job_id);
    await loadAll();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    batchBusy.value = false;
  }
}

// 恢复进程重启后遗留的 stale SuiteJob：孤儿 attempt 标记为失败（可重试），
// 未完成的 case 用新 Run 继续执行，已完成证据保留。
async function recoverJobs() {
  error.value = "";
  try {
    const result = await recoverStaleSuiteJobs();
    await loadAll();
    if (result.resumed.length && activeJob.value && result.resumed.includes(activeJob.value.job_id)) {
      startJobPolling(activeJob.value.job_id);
    }
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function selectJob(jobId: string) {
  error.value = "";
  stopJobPolling();
  try {
    const payload = await fetchSuiteJob(jobId);
    activeJob.value = payload.suite_job;
    if (["queued", "running"].includes(String(payload.suite_job.status))) startJobPolling(jobId);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function cancelActiveJob() {
  if (!activeJob.value) return;
  error.value = "";
  try {
    const payload = await cancelSuiteJob(activeJob.value.job_id);
    activeJob.value = payload.suite_job;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

function startJobPolling(jobId: string) {
  stopJobPolling();
  jobPollTimer = setInterval(async () => {
    try {
      const payload = await fetchSuiteJob(jobId);
      activeJob.value = payload.suite_job;
      if (!["queued", "running"].includes(String(payload.suite_job.status))) {
        stopJobPolling();
        await loadAll();
      }
    } catch {
      stopJobPolling();
    }
  }, 1000);
}

function stopJobPolling() {
  if (jobPollTimer) {
    clearInterval(jobPollTimer);
    jobPollTimer = null;
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
onUnmounted(() => {
  stopPolling();
  stopJobPolling();
});
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
          <tr><th>ID</th><th>名称</th><th>环境</th><th>版本</th><th>Endpoint</th><th>能力</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="agent in agents" :key="agent.agent_id">
            <td><code>{{ agent.agent_id }}</code></td>
            <td>{{ agent.name }}</td>
            <td>{{ agent.environment || "—" }}</td>
            <td>{{ agent.version || "—" }}</td>
            <td><code>{{ agent.endpoint }}</code></td>
            <td>{{ (agent.capabilities || []).join(", ") }}</td>
            <td>
              <button class="ghost" type="button" @click="editAgent(agent)">编辑</button>
              <button class="ghost" type="button" @click="removeAgent(agent)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty">尚未配置 Agent。</p>
      <form class="platform-form" @submit.prevent="submitAgent">
        <input v-model="agentForm.agent_id" placeholder="agent_id（如 xuanjian-local）" :disabled="!!editingAgentId" required />
        <input v-model="agentForm.name" placeholder="名称（可选）" />
        <input v-model="agentForm.version" placeholder="版本（如 git:abc123，可选）" />
        <input v-model="agentForm.endpoint" placeholder="endpoint（http(s)://host:port）" required />
        <input v-model="agentForm.capabilities" placeholder="能力（逗号分隔：session,chat）" />
        <input v-model="agentForm.environment" placeholder="环境（local/development/staging/production，可选）" />
        <input v-model="agentForm.description" placeholder="描述（可选）" />
        <input v-model="agentForm.tags" placeholder="标签（逗号分隔，可选）" />
        <button class="primary" type="submit" :disabled="agentBusy">
          {{ agentBusy ? "保存中…" : editingAgentId ? "更新 Agent" : "保存 Agent" }}
        </button>
        <button v-if="editingAgentId" class="ghost" type="button" @click="resetAgentForm">取消编辑</button>
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

    <section class="panel platform-panel" aria-label="Test Suites">
      <div class="panel-head">
        <h3>Test Suites</h3>
        <span class="chip quiet">{{ suites.length }}</span>
      </div>
      <table v-if="suites.length" class="platform-table">
        <thead>
          <tr><th>ID</th><th>标题</th><th>用例数</th><th>版本</th><th>启用</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="suite in suites" :key="suite.suite_id">
            <td><code>{{ suite.suite_id }}</code></td>
            <td>{{ suite.title }}</td>
            <td>{{ suite.case_ids.length }}</td>
            <td>v{{ suite.version }}</td>
            <td>{{ suite.enabled ? "是" : "否" }}</td>
            <td>
              <button class="ghost" type="button" @click="editSuite(suite)">编辑</button>
              <button class="ghost" type="button" @click="removeSuite(suite)">删除</button>
            </td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty">尚未创建测试集。</p>
      <form class="platform-form" @submit.prevent="submitSuite">
        <input v-model="suiteForm.suite_id" placeholder="suite_id" :disabled="!!editingSuiteId" required />
        <input v-model="suiteForm.title" placeholder="标题" required />
        <input v-model="suiteForm.description" placeholder="描述（可选）" />
        <label class="judge-toggle">
          <input v-model="suiteForm.enabled" type="checkbox" />
          启用（停用的测试集不能发起批量运行）
        </label>
        <div class="suite-case-picker">
          <p class="hint">按勾选顺序加入测试集，执行时按此顺序逐个运行：</p>
          <label v-for="item in runnableCases" :key="item.case_id" class="judge-toggle">
            <input
              type="checkbox"
              :checked="suiteForm.case_ids.includes(item.case_id)"
              @change="toggleSuiteCase(item.case_id)"
            />
            <code>{{ item.case_id }}</code> — {{ item.title }}
          </label>
          <p v-if="!runnableCases.length" class="hint">暂无可用 TestCase，请先在上方创建。</p>
        </div>
        <button class="primary" type="submit" :disabled="suiteBusy">
          {{ suiteBusy ? "保存中…" : editingSuiteId ? "更新测试集" : "保存测试集" }}
        </button>
        <button v-if="editingSuiteId" class="ghost" type="button" @click="resetSuiteForm">取消编辑</button>
      </form>
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

    <section class="panel platform-panel" aria-label="批量运行">
      <div class="panel-head">
        <h3>批量运行</h3>
        <span class="chip quiet">{{ suiteJobs.length }} 个 SuiteJob</span>
      </div>
      <div class="platform-form">
        <select v-model="batchAgentId" aria-label="选择 Agent">
          <option v-for="agent in agents" :key="agent.agent_id" :value="agent.agent_id">
            {{ agent.name }}（{{ agent.endpoint }}）
          </option>
        </select>
        <select v-model="batchSuiteId" aria-label="选择 TestSuite">
          <option v-for="suite in enabledSuites" :key="suite.suite_id" :value="suite.suite_id">
            {{ suite.suite_id }} — {{ suite.title }}（{{ suite.case_ids.length }} 个用例）
          </option>
        </select>
        <label class="judge-toggle">
          <input v-model="batchJudge" type="checkbox" />
          每个用例运行后执行 LLM Judge（需要模型凭据；失败不影响运行产物）
        </label>
        <div class="batch-controls">
          <label class="batch-field">
            失败重试次数
            <input v-model.number="batchMaxRetries" type="number" min="0" max="5" placeholder="0" />
          </label>
          <label class="batch-field">
            套件总超时（秒）
            <input v-model.number="batchTimeoutSeconds" type="number" min="1" placeholder="不限" />
          </label>
          <label class="batch-field">
            执行预算（最大运行次数）
            <input v-model.number="batchBudgetAttempts" type="number" min="1" placeholder="不限" />
          </label>
          <label class="batch-field">
            成本预算（最大 token）
            <input v-model.number="batchBudgetTokens" type="number" min="1" placeholder="不限" />
          </label>
          <label class="batch-field">
            Idempotency-Key（可选）
            <input v-model="batchIdempotencyKey" placeholder="防止重复提交同一批量任务" />
          </label>
        </div>
        <button
          class="primary"
          type="button"
          :disabled="batchBusy || !batchAgentId || !batchSuiteId"
          @click="launchSuite"
        >
          {{ batchBusy ? "提交中…" : "发起批量运行" }}
        </button>
        <button v-if="hasStaleJobs" class="ghost" type="button" @click="recoverJobs">
          恢复中断的批量任务
        </button>
        <p class="hint">
          按测试集定义顺序逐个执行；单个 case 失败不会阻断后续 case。可重试的失败（超时、连接失败、5xx）按重试次数自动重试，每次重试创建新
          Run，不覆盖旧证据；并发受全局与单 Agent 上限约束。
        </p>
      </div>

      <table v-if="suiteJobs.length" class="platform-table jobs-table">
        <thead>
          <tr><th>Job</th><th>测试集</th><th>状态</th><th>统计</th><th>创建时间</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="job in suiteJobs"
            :key="job.job_id"
            :class="{ selected: activeJob && activeJob.job_id === job.job_id }"
            @click="selectJob(job.job_id)"
          >
            <td><code>{{ job.job_id }}</code></td>
            <td>{{ job.suite_ref?.title || job.suite_ref?.id || "—" }}</td>
            <td>
              <span class="chip" :data-status="job.status">{{ jobStatusLabel(String(job.status)) }}</span>
              <span v-if="job.stale" class="chip quiet">执行器已中断（stale）</span>
              <span v-else-if="job.recovered" class="chip quiet">已恢复</span>
            </td>
            <td>
              {{ job.stats.completed }}/{{ job.stats.total }} 完成
              <span v-if="job.stats.retries" class="hint">（重试 {{ job.stats.retries }} 次）</span>
            </td>
            <td>{{ job.created_at || "—" }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty">尚未发起批量运行。</p>

      <div v-if="activeJob" class="run-status-card" :data-status="activeJob.status">
        <p>
          <strong>{{ activeJob.job_id }}</strong>
          <span class="chip" :data-status="activeJob.status">{{ jobStatusLabel(String(activeJob.status)) }}</span>
          <span v-if="activeJob.cancel_requested" class="chip quiet">已请求取消</span>
          <span v-if="activeJob.stale" class="chip quiet">执行器已中断（stale）</span>
          <span v-if="activeJob.recovered" class="chip quiet">已恢复（进程重启后续跑）</span>
        </p>
        <p v-if="activeJob.status_reason" class="hint">原因：{{ activeJob.status_reason }}</p>
        <p class="hint">
          Agent：{{ activeJob.agent_name || activeJob.agent_id || "unknown" }}
          <template v-if="activeJob.agent_environment">（环境：{{ activeJob.agent_environment }}）</template>
          <template v-else-if="!activeJob.agent_id">（旧任务未记录 Agent，显示为 unknown）</template>
        </p>
        <p class="hint">
          统计：总数 {{ activeJob.stats.total }}，完成 {{ activeJob.stats.completed }}，失败
          {{ activeJob.stats.failed }}，超时 {{ activeJob.stats.timed_out }}，取消
          {{ activeJob.stats.cancelled }}，跳过 {{ activeJob.stats.skipped }}；运行
          {{ activeJob.stats.runs }} 次，重试 {{ activeJob.stats.retries }} 次。通过率分母只包含已完成的 case，跳过不计入。
        </p>
        <p v-if="activeJob.limits" class="hint">
          并发上限：全局 {{ activeJob.limits.global }} / 单 Agent {{ activeJob.limits.agent }}；
          单 case 重试上限 {{ activeJob.max_retries ?? 0 }} 次<template v-if="activeJob.timeout_seconds">
            ；套件总超时 {{ activeJob.timeout_seconds }} 秒</template>。
        </p>
        <p v-if="activeJob.budget && (activeJob.budget.max_attempts || activeJob.budget.max_total_tokens)" class="hint">
          预算：已运行 {{ activeJob.budget_used?.attempts ?? 0 }} 次<template v-if="activeJob.budget.max_attempts">
            / 上限 {{ activeJob.budget.max_attempts }}</template>；已消耗
          {{ activeJob.budget_used?.total_tokens ?? 0 }} token<template v-if="activeJob.budget.max_total_tokens">
            / 上限 {{ activeJob.budget.max_total_tokens }}</template>。预算耗尽后未开始的 case 标记为跳过。
        </p>
        <table v-if="activeJobAttempts.length" class="platform-table">
          <thead>
            <tr><th>#</th><th>TestCase</th><th>状态</th><th>质量</th><th>Run</th></tr>
          </thead>
          <tbody>
            <tr v-for="attempt in activeJobAttempts" :key="attempt.attempt_id">
              <td>{{ attempt.order + 1 }}</td>
              <td>
                <code>{{ attempt.case_id }}</code> {{ attempt.case_title }}
                <span v-if="attempt.retry_index" class="chip quiet">重试 #{{ attempt.retry_index }}</span>
                <span v-if="attempt.attempt_count && attempt.attempt_count > 1" class="chip quiet">
                  共 {{ attempt.attempt_count }} 次尝试
                </span>
                <span v-if="attempt.is_final_attempt" class="chip quiet">最终尝试</span>
              </td>
              <td>
                <span class="chip" :data-status="attempt.status">{{ runStatusLabel(String(attempt.status)) }}</span>
                <span v-if="attempt.status_reason" class="hint">— {{ attempt.status_reason }}</span>
              </td>
              <td>{{ attempt.final_quality_status ? finalQualityLabel(String(attempt.final_quality_status)) : "—" }}</td>
              <td>
                <button v-if="attempt.run_id" class="ghost" type="button" @click="openRun(attempt.run_id)">
                  打开运行详情
                </button>
                <span v-else>—</span>
              </td>
            </tr>
          </tbody>
        </table>
        <button
          v-if="['queued', 'running'].includes(String(activeJob.status))"
          class="ghost"
          type="button"
          @click="cancelActiveJob"
        >
          取消批量运行
        </button>
      </div>
    </section>
  </div>
</template>
