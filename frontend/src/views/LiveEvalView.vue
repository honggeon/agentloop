<script setup lang="ts">
import { computed, inject, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import {
  apiPostJson,
  apiPost,
  cancelDshAcpOperation,
  cancelDshChat,
  closeDshAcpSession,
  createDshAcpSession,
  fetchDshAcpOptions,
  fetchDshAcpSessions,
  fetchDshOptions,
  respondDshAcpPermission,
  resumeDshAcpSession,
  streamDshAcpPrompt,
  streamDshChat,
  streamPostJson,
  type DshAcpCompletedPayload,
  type DshAcpOptions,
  type DshAcpPermissionRequest,
  type DshAcpSession,
  type DshChatErrorPayload,
  type DshOptions,
  type DshRuntimeEvent,
  type Finding,
  type JudgeReport,
  type LiveEvaluatePayload,
  type LiveEvent,
  type RuntimeGraph,
  type RuntimeGraphNode,
  type RuntimeProviderId,
  type RuntimeSpan,
  type RuntimeTrace,
  type RunItem
} from "../api";

interface LiveLogItem {
  index: number;
  event: string;
  text: string;
}

type DshPhase = "idle" | "running" | "stopping" | "cancelled" | "timeout" | "failed" | "imported" | "judged";

type DshStreamKind = "status" | "thinking" | "text" | "tool_call" | "tool_result" | "final" | "error";

interface DshStreamItem {
  index: number;
  kind: DshStreamKind;
  title: string;
  detail: string;
  status?: string;
  callId?: string;
  expanded: boolean;
}

const DSH_PHASE_LABELS: Record<DshPhase, string> = {
  idle: "未开始",
  running: "运行中",
  stopping: "正在停止",
  cancelled: "已取消",
  timeout: "已超时",
  failed: "失败",
  imported: "已导入运行",
  judged: "已评测"
};

const DSH_STATUS_PHASE_LABELS: Record<string, string> = {
  turn_start: "回合开始",
  step_start: "步骤开始",
  step_end: "步骤结束",
  turn_end: "回合结束"
};

const DSH_KIND_LABELS: Record<DshStreamKind, string> = {
  status: "status",
  thinking: "thinking",
  text: "assistant",
  tool_call: "tool_call",
  tool_result: "tool_result",
  final: "final",
  error: "error"
};

// ACP 回合的结束状态：completed 只说明 session/prompt 已结束（stopReason 把
// 成功与若干失败都折叠成 end_turn），不代表任务成功。
type AcpPhase = "idle" | "running" | "stopping" | "settled" | "cancelled" | "failed" | "imported";

const ACP_PHASE_LABELS: Record<AcpPhase, string> = {
  idle: "未开始",
  running: "回合进行中",
  stopping: "正在取消",
  settled: "回合已结束（未导入）",
  cancelled: "已取消",
  failed: "失败",
  imported: "已导入运行"
};

const router = useRouter();
const reloadRuns = inject<() => Promise<void>>("reloadRuns")!;

const form = ref<LiveEvaluatePayload>({
  base_url: "",
  user_id: "",
  token: "",
  model: "",
  title: "ppt-master runtime evaluation",
  skill: "ppt-master",
  message: "",
  acceptance_criteria: "必须调用 ppt-master；如进入确认环节，不允许伪造用户确认，必须通过 clarify/approval 等真实交互继续；最终回答必须说明产物路径。",
  max_turns: 20,
  timeout: 60,
  auto_continue: true,
  auto_continue_limit: 8
});

const running = ref(false);
const judging = ref(false);
const error = ref("");
const selectedFiles = ref<File[]>([]);
const logs = ref<LiveLogItem[]>([]);
const graph = ref<RuntimeGraph>({ nodes: [], edges: [], current_node: null });
const trace = ref<RuntimeTrace | null>(null);
const traceSource = ref<"initial" | "session">("initial");
const selectedSpanId = ref("");
const collapsedSpanIds = ref<Set<string>>(new Set());
const audit = ref<Record<string, any> | null>(null);
const finalRun = ref<RunItem | null>(null);
const judge = ref<JudgeReport | null>(null);
const currentSessionId = ref("");
const pendingClarify = ref<any | null>(null);
const clarifyResponse = ref("继续使用当前推荐方案。");
const pendingApproval = ref<any | null>(null);
const pendingChatInteraction = ref<any | null>(null);
const chatResponse = ref("确认");

// 运行时提供方与本地 DSH 工作台状态。
const provider = ref<RuntimeProviderId>("remote");
const dshOptions = ref<DshOptions | null>(null);
const dshOptionsLoaded = ref(false);
const dshOptionsError = ref("");
const dshForm = ref({
  profile: "",
  workspace: "",
  message: "",
  acceptance_criteria: "",
  timeout: 300
});
const dshOperationId = ref("");
const dshSessionId = ref("");
const dshPhase = ref<DshPhase>("idle");
const dshStream = ref<DshStreamItem[]>([]);

// 本机 DSH ACP 交互运行时：长驻进程 + 会话 + 一次性权限应答。
const dshAcpOptions = ref<DshAcpOptions | null>(null);
const dshAcpOptionsLoaded = ref(false);
const dshAcpOptionsError = ref("");
const dshAcpSessions = ref<DshAcpSession[]>([]);
const dshAcpForm = ref({
  workspace: "",
  message: "",
  acceptance_criteria: ""
});
const dshAcpSessionId = ref("");
const dshAcpOperationId = ref("");
const dshAcpPhase = ref<AcpPhase>("idle");
const dshAcpStopReason = ref("");
const dshAcpStatus = ref("");
const dshAcpCompleted = ref<DshAcpCompletedPayload | null>(null);
// 同一时刻只保留一个待回答的权限请求：DSH 在 prompt 阻塞期间等待它。
const dshAcpPermission = ref<DshAcpPermissionRequest | null>(null);
const dshAcpPermissionBusy = ref(false);

const dshEnabled = computed(() => Boolean(dshOptions.value?.dsh.enabled));
const dshDisabledReason = computed(() => dshOptions.value?.dsh.disabled_reason || "");
const dshProfiles = computed(() => dshOptions.value?.dsh.profiles || []);
const dshWorkspaces = computed(() => dshOptions.value?.dsh.workspaces || []);
const dshWorkspaceRoots = computed(() => dshOptions.value?.dsh.workspace_roots || []);
const dshMaxTimeout = computed(() => Number(dshOptions.value?.dsh.max_timeout_seconds || 900));
const dshPhaseLabel = computed(() => DSH_PHASE_LABELS[dshPhase.value]);
const acpManifest = computed(() => dshOptions.value?.dsh.acp || null);
const acpEnabled = computed(() => Boolean(dshEnabled.value && acpManifest.value?.enabled));
const acpDisabledReason = computed(
  () =>
    acpManifest.value?.enabled === false
      ? dshDisabledReason.value || "本地 DSH ACP 运行时未启用"
      : dshDisabledReason.value
);
const acpCapabilities = computed(() => dshAcpOptions.value?.capabilities || {});
const providerLabel = computed(() => {
  if (provider.value === "acp") return "Local DSH ACP";
  return provider.value === "dsh" ? "Local DSH" : "远程 Agent";
});
const dshAcpPhaseLabel = computed(() => ACP_PHASE_LABELS[dshAcpPhase.value]);
const acpStatusLabel = computed(() => {
  if (dshAcpPhase.value === "imported") return "已发布 Auditrail run";
  if (dshAcpPhase.value === "running") return "回合进行中，尚未发布 run";
  if (dshAcpPhase.value === "settled") return "回合已结束，未发布 run";
  return dshAcpPhase.value === "idle" ? "尚未开始" : ACP_PHASE_LABELS[dshAcpPhase.value];
});
const canSubmitAcp = computed(() => {
  return Boolean(
    acpEnabled.value && dshAcpForm.value.workspace.trim() && dshAcpForm.value.message.trim() && !running.value
  );
});
const canCreateAcpSession = computed(
  () => acpEnabled.value && Boolean(dshAcpForm.value.workspace.trim()) && !running.value
);
const canSubmitRemote = computed(() => {
  return Boolean(form.value.base_url.trim() && form.value.user_id.trim() && form.value.message.trim() && !running.value);
});
const canSubmitDsh = computed(() => {
  return Boolean(
    dshEnabled.value &&
      dshForm.value.profile &&
      dshForm.value.workspace.trim() &&
      dshForm.value.message.trim() &&
      !running.value
  );
});
const canSubmit = computed(() => {
  if (provider.value === "acp") return canSubmitAcp.value;
  return provider.value === "dsh" ? canSubmitDsh.value : canSubmitRemote.value;
});
const submitLabel = computed(() => {
  if (provider.value === "acp") {
    if (running.value) return "ACP 回合进行中";
    return dshAcpSessionId.value ? "继续 ACP 会话" : "开始 ACP 会话";
  }
  if (provider.value === "dsh") {
    if (running.value) return "DSH 运行中";
    return dshSessionId.value ? "继续 DSH 会话" : "开始 DSH 会话";
  }
  return running.value ? "运行中" : "开始评测";
});
const screenStatus = computed(() => {
  if (provider.value === "acp") return ACP_PHASE_LABELS[dshAcpPhase.value];
  return provider.value === "dsh" ? dshPhaseLabel.value : traceSummary.value.status;
});
const traceAgentName = computed(() => {
  if (provider.value === "acp") return "DSH ACP Runtime";
  return provider.value === "dsh" ? "DSH Runtime" : form.value.title || "";
});
const traceAgentVersion = computed(() => {
  if (provider.value === "acp") return acpManifest.value?.profile || "acp";
  return provider.value === "dsh" ? dshForm.value.profile : form.value.model || "";
});

const findings = computed<Finding[]>(() => judge.value?.findings || []);
const liveGraphTrace = computed<RuntimeTrace>(() =>
  traceFromGraph(graph.value, currentSessionId.value, traceAgentName.value, traceAgentVersion.value)
);
const activeTrace = computed<RuntimeTrace>(() => {
  if (!trace.value) return liveGraphTrace.value;
  if (traceSource.value === "initial" && liveGraphTrace.value.spans.length > trace.value.spans.length) {
    return liveGraphTrace.value;
  }
  return trace.value;
});
const selectedSpan = computed<RuntimeSpan | null>(() => {
  return activeTrace.value.spans.find((span) => span.spanId === selectedSpanId.value) || activeTrace.value.spans[0] || null;
});
const traceRows = computed(() => flattenTrace(activeTrace.value.spans, collapsedSpanIds.value));
const traceSummary = computed(() => {
  const currentTrace = activeTrace.value;
  const metrics = currentTrace.metrics || {};
  const diagnosis = runtimeDiagnosis.value;
  return {
    status: diagnosis.kind === "needs_input" ? "needs_input" : normalizeTraceStatus(currentTrace.status),
    duration: formatDuration(currentTrace.totalLatency),
    steps: metrics.steps ?? currentTrace.spans.length,
    llmCalls: metrics.llmCalls ?? currentTrace.spans.filter((span) => span.type === "LLM").length,
    toolCalls:
      (metrics.toolCalls ?? 0) +
      (metrics.skillCalls ?? currentTrace.spans.filter((span) => span.type === "Skill").length),
    tokens: currentTrace.totalTokens || sumTokens(currentTrace.spans),
    cost: formatCost(currentTrace.cost)
  };
});
const timelineEvents = computed(() => {
  const start = parseTime(activeTrace.value.startTime);
  return activeTrace.value.spans.map((span) => ({
    id: span.spanId,
    offset: formatOffset(start, parseTime(span.startTime)),
    label: timelineLabel(span),
    status: normalizeTraceStatus(span.status)
  }));
});
const runtimeDiagnosis = computed(() => buildRuntimeDiagnosis());

async function startEvaluation() {
  if (!canSubmit.value) return;
  running.value = true;
  error.value = "";
  logs.value = [];
  graph.value = { nodes: [], edges: [], current_node: null };
  trace.value = null;
  traceSource.value = "initial";
  selectedSpanId.value = "";
  collapsedSpanIds.value = new Set();
  audit.value = null;
  finalRun.value = null;
  judge.value = null;
  currentSessionId.value = "";
  pendingClarify.value = null;
  pendingApproval.value = null;
  pendingChatInteraction.value = null;
  chatResponse.value = "确认";

  try {
    await streamPostJson("/api/live/evaluate-with-files", await normalizedPayload(), handleLiveEvent);
    await reloadRuns();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    running.value = false;
  }
}

async function continueSession() {
  if (!currentSessionId.value || !chatResponse.value.trim() || running.value) return;
  running.value = true;
  error.value = "";
  judge.value = null;
  pendingChatInteraction.value = null;
  try {
    await streamPostJson(
      "/api/live/message/respond",
      {
        ...connectionPayload(),
        session_id: currentSessionId.value,
        message: chatResponse.value.trim(),
        acceptance_criteria: form.value.acceptance_criteria.trim(),
        model: form.value.model?.trim() || undefined,
        max_turns: Number(form.value.max_turns || 20),
        auto_continue: Boolean(form.value.auto_continue),
        auto_continue_limit: Number(form.value.auto_continue_limit || 8)
      },
      handleLiveEvent
    );
    await reloadRuns();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    running.value = false;
  }
}

function handleLiveEvent(item: { event: string; payload: any }) {
  appendLog(item.event, item.payload);
  appendLiveGraphEvent(item.event, item.payload);
  if (item.event === "run_started") {
    currentSessionId.value = item.payload.session_id || currentSessionId.value;
  }
  if (item.event === "graph_update") {
    graph.value = item.payload.graph || graph.value;
  } else if (item.event === "trace_update") {
    trace.value = item.payload.trace || trace.value;
    traceSource.value = item.payload.run_id && item.payload.run_id !== currentSessionId.value ? "session" : "initial";
    if (!selectedSpanId.value && trace.value?.spans.length) {
      selectedSpanId.value = attentionSpanId() || trace.value.spans[0].spanId;
    }
  } else if (item.event === "audit_update") {
    audit.value = item.payload.audit || null;
  } else if (item.event === "needs_interaction") {
    pendingChatInteraction.value = item.payload || {};
    chatResponse.value = item.payload.recommended_response || "确认";
    selectedSpanId.value = attentionSpanId() || selectedSpanId.value;
  } else if (item.event === "auto_interaction") {
    pendingClarify.value = null;
    pendingApproval.value = null;
    pendingChatInteraction.value = null;
    if (item.payload?.kind === "continue" && item.payload?.response) {
      chatResponse.value = item.payload.response;
    }
  } else if (item.event === "run_finished") {
    finalRun.value = item.payload.run || null;
    pendingClarify.value = null;
    pendingApproval.value = null;
  } else if (item.event === "error") {
    error.value = item.payload.stage
      ? `${item.payload.stage}: ${item.payload.detail || JSON.stringify(item.payload)}`
      : item.payload.detail || JSON.stringify(item.payload);
  } else if (item.event === "agent_event") {
    if (item.payload.name === "clarify") {
      pendingClarify.value = item.payload.payload || {};
      clarifyResponse.value = recommendedClarifyResponse(pendingClarify.value);
    } else if (item.payload.name === "approval") {
      pendingApproval.value = item.payload.payload || {};
    }
  }
}

async function loadDshOptions() {
  try {
    const options = await fetchDshOptions();
    dshOptions.value = options;
    dshOptionsError.value = "";
    dshForm.value.profile = options.dsh.profiles[0] || "";
    dshForm.value.workspace = options.dsh.workspaces[0] || "";
    dshForm.value.timeout = Number(options.dsh.default_timeout_seconds || 300);
    dshAcpForm.value.workspace = options.dsh.workspaces[0] || "";
  } catch (err) {
    dshOptionsError.value = err instanceof Error ? err.message : String(err);
  } finally {
    dshOptionsLoaded.value = true;
  }
}

// --- 本机 DSH ACP 交互运行时 ------------------------------------------------

async function loadDshAcpOptions() {
  try {
    dshAcpOptions.value = await fetchDshAcpOptions();
    dshAcpOptionsError.value = "";
  } catch (err) {
    dshAcpOptionsError.value = err instanceof Error ? err.message : String(err);
  } finally {
    dshAcpOptionsLoaded.value = true;
  }
}

async function refreshDshAcpSessions() {
  try {
    const payload = await fetchDshAcpSessions();
    dshAcpSessions.value = payload.sessions || [];
  } catch {
    dshAcpSessions.value = [];
  }
}

async function newAcpSession() {
  if (!canCreateAcpSession.value) return;
  try {
    const session = await createDshAcpSession({ workspace: dshAcpForm.value.workspace.trim() });
    applyAcpSession(session.session_id, session.workspace);
    dshAcpPhase.value = "idle";
    dshAcpCompleted.value = null;
    dshAcpStopReason.value = "";
    dshAcpPermission.value = null;
    await refreshDshAcpSessions();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function resumeAcpSession(session: DshAcpSession) {
  if (running.value) return;
  try {
    const resumed = await resumeDshAcpSession({
      workspace: session.workspace || dshAcpForm.value.workspace.trim(),
      session_id: session.session_id
    });
    applyAcpSession(resumed.session_id, resumed.workspace);
    dshAcpPhase.value = "idle";
    dshAcpCompleted.value = null;
    dshAcpStopReason.value = "";
    dshAcpPermission.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function closeAcpSession(sessionId: string) {
  if (running.value) return;
  try {
    await closeDshAcpSession(sessionId);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
  if (dshAcpSessionId.value === sessionId) {
    dshAcpSessionId.value = "";
    currentSessionId.value = "";
    dshAcpPhase.value = "idle";
    dshAcpCompleted.value = null;
    dshAcpStopReason.value = "";
  }
  await refreshDshAcpSessions();
}

function applyAcpSession(sessionId: string, workspace?: string) {
  const next = String(sessionId || "").trim();
  if (!next) return;
  dshAcpSessionId.value = next;
  currentSessionId.value = next;
  if (workspace) dshAcpForm.value.workspace = String(workspace);
}

function resetAcpTurnPanels() {
  dshStream.value = [];
  resetRuntimePanels();
  dshAcpCompleted.value = null;
  dshAcpStopReason.value = "";
  dshAcpStatus.value = "";
  dshAcpPermission.value = null;
}

async function startAcpTurn() {
  if (!canSubmitAcp.value) return;
  if (!dshAcpSessionId.value) {
    await newAcpSession();
    if (!dshAcpSessionId.value) return;
  }
  resetAcpTurnPanels();
  running.value = true;
  error.value = "";
  judge.value = null;
  dshAcpPhase.value = "running";
  dshAcpStatus.value = "started";
  try {
    await streamDshAcpPrompt(
      {
        session_id: dshAcpSessionId.value,
        message: dshAcpForm.value.message.trim(),
        acceptance_criteria: dshAcpForm.value.acceptance_criteria.trim()
      },
      handleAcpEvent
    );
    await reloadRuns();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    if (dshAcpPhase.value === "running") dshAcpPhase.value = "failed";
  } finally {
    running.value = false;
    dshAcpOperationId.value = "";
    dshAcpPermission.value = null;
    await refreshDshAcpSessions();
  }
}

async function stopAcpTurn() {
  if (!dshAcpOperationId.value || !running.value) return;
  dshAcpPhase.value = "stopping";
  try {
    await cancelDshAcpOperation(dshAcpOperationId.value);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function answerAcpPermission(optionId: string) {
  const permission = dshAcpPermission.value;
  if (!permission || !dshAcpOperationId.value || dshAcpPermissionBusy.value) return;
  dshAcpPermissionBusy.value = true;
  try {
    await respondDshAcpPermission(dshAcpOperationId.value, permission.request_id, optionId);
    dshAcpPermission.value = null;
    pushDshStream({
      kind: "status",
      title: optionId === "allow-once" ? "已允许一次" : "已拒绝一次",
      detail: `request: ${permission.request_id}\ntool_call: ${permission.tool_call_id}`
    });
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    dshAcpPermissionBusy.value = false;
  }
}

function handleAcpEvent(item: LiveEvent) {
  appendLog(item.event, item.payload);
  if (item.event === "started") {
    dshAcpOperationId.value = String(item.payload.operation_id || "");
    applyAcpSession(item.payload.session_id);
    dshAcpStatus.value = "started";
    dshAcpPhase.value = "running";
    return;
  }
  if (item.event === "runtime_event") {
    dshAcpStatus.value = "streaming";
    const runtimeEvent = item.payload as DshRuntimeEvent | undefined;
    if (runtimeEvent) appendDshRuntimeEvent(runtimeEvent);
    return;
  }
  if (item.event === "permission_requested") {
    const permission = item.payload as DshAcpPermissionRequest;
    dshAcpPermission.value = permission;
    dshAcpStatus.value = "waiting_permission";
    pushDshStream({
      kind: "status",
      title: "等待一次性权限决定",
      detail: `tool_call: ${permission.tool_call_id}\n选项: ${(permission.options || [])
        .map((option) => option.optionId)
        .join(", ")}`
    });
    return;
  }
  if (item.event === "completed") {
    const completed = item.payload as DshAcpCompletedPayload;
    dshAcpCompleted.value = completed;
    dshAcpStopReason.value = completed.stop_reason || "";
    dshAcpStatus.value = "completed";
    // completed 只表示 ACP 回合已结束；standard run 是否发布由 run_imported 决定。
    dshAcpPhase.value = completed.cancelled ? "cancelled" : "settled";
    pushDshStream({
      kind: "status",
      title: "ACP 回合已结束",
      detail: `stop_reason: ${completed.stop_reason}\ncancelled: ${completed.cancelled}\n（回合结束不等于任务成功，等待 run_imported 或 import 阶段的 error）`
    });
    return;
  }
  if (item.event === "run_imported") {
    applyAcpSession(item.payload.session_id);
    dshAcpPhase.value = "imported";
    dshAcpStatus.value = "run_imported";
    return;
  }
  if (item.event === "error") {
    const payload = item.payload as DshChatErrorPayload;
    dshAcpStatus.value = payload.stage === "import" ? "import_failed" : payload.code || "error";
    dshAcpPhase.value = payload.cancelled ? "cancelled" : "failed";
    error.value = payload.stage
      ? `${payload.stage}: ${payload.detail || JSON.stringify(payload)}`
      : payload.detail || JSON.stringify(payload);
    pushDshStream({
      kind: "error",
      title: payload.stage === "import" ? "导入阶段失败" : "ACP 回合失败",
      detail: payload.detail || JSON.stringify(payload)
    });
  }
}

function selectProvider(next: RuntimeProviderId) {
  if (running.value || provider.value === next) return;
  if (next === "dsh" && !dshEnabled.value) return;
  if (next === "acp" && !acpEnabled.value) return;
  provider.value = next;
  resetRuntimePanels();
  if (next === "acp") {
    loadDshAcpOptions();
    refreshDshAcpSessions();
  }
}

function resetRuntimePanels() {
  logs.value = [];
  graph.value = { nodes: [], edges: [], current_node: null };
  trace.value = null;
  traceSource.value = "initial";
  selectedSpanId.value = "";
  collapsedSpanIds.value = new Set();
  audit.value = null;
  error.value = "";
}

async function submitActive() {
  if (provider.value === "acp") {
    await startAcpTurn();
    return;
  }
  if (provider.value === "dsh") {
    await startDshTurn();
    return;
  }
  await startEvaluation();
}

async function startDshTurn() {
  if (!canSubmitDsh.value) return;
  if (!dshSessionId.value) resetRuntimePanels();
  running.value = true;
  error.value = "";
  judge.value = null;
  pendingClarify.value = null;
  pendingApproval.value = null;
  pendingChatInteraction.value = null;
  dshPhase.value = "running";
  const payload = {
    message: dshForm.value.message.trim(),
    workspace: dshForm.value.workspace.trim(),
    profile: dshForm.value.profile,
    session_id: dshSessionId.value || null,
    acceptance_criteria: dshForm.value.acceptance_criteria.trim(),
    timeout: Number(dshForm.value.timeout || dshOptions.value?.dsh.default_timeout_seconds || 300)
  };
  try {
    await streamDshChat(payload, handleDshEvent);
    await reloadRuns();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
    if (dshPhase.value === "running") dshPhase.value = "failed";
  } finally {
    running.value = false;
    dshOperationId.value = "";
  }
}

async function stopDshTurn() {
  if (!dshOperationId.value || !running.value) return;
  dshPhase.value = "stopping";
  try {
    await cancelDshChat(dshOperationId.value);
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

function newDshSession() {
  if (running.value) return;
  dshSessionId.value = "";
  dshOperationId.value = "";
  dshStream.value = [];
  dshPhase.value = "idle";
  currentSessionId.value = "";
  resetRuntimePanels();
}

function handleDshEvent(item: LiveEvent) {
  appendLog(item.event, item.payload);
  if (item.event === "run_started") {
    dshOperationId.value = String(item.payload.operation_id || "");
    applyDshSessionId(item.payload.session_id);
    dshPhase.value = "running";
    return;
  }
  if (item.event === "agent_event") {
    applyDshSessionId(item.payload.session_id);
    const runtimeEvent = item.payload.event as DshRuntimeEvent | undefined;
    if (runtimeEvent) appendDshRuntimeEvent(runtimeEvent);
    return;
  }
  if (item.event === "graph_update") {
    graph.value = item.payload.graph || graph.value;
    return;
  }
  if (item.event === "trace_update") {
    trace.value = item.payload.trace || trace.value;
    traceSource.value = "session";
    if (!selectedSpanId.value && trace.value?.spans.length) {
      selectedSpanId.value = attentionSpanId() || trace.value.spans[0].spanId;
    }
    return;
  }
  if (item.event === "audit_update") {
    audit.value = item.payload.audit || null;
    return;
  }
  if (item.event === "run_finished") {
    finalRun.value = item.payload.run || null;
    dshPhase.value = "imported";
    return;
  }
  if (item.event === "error") {
    const payload = item.payload as DshChatErrorPayload;
    if (payload.cancelled) {
      dshPhase.value = "cancelled";
    } else if (payload.timed_out) {
      dshPhase.value = "timeout";
    } else {
      dshPhase.value = "failed";
    }
    error.value = payload.stage
      ? `${payload.stage}: ${payload.detail || JSON.stringify(payload)}`
      : payload.detail || JSON.stringify(payload);
    pushDshStream({
      kind: "error",
      title: DSH_PHASE_LABELS[dshPhase.value],
      detail: payload.detail || JSON.stringify(payload)
    });
  }
}

function applyDshSessionId(value: unknown) {
  const sessionId = typeof value === "string" ? value.trim() : "";
  if (!sessionId) return;
  dshSessionId.value = sessionId;
  currentSessionId.value = sessionId;
}

function appendDshRuntimeEvent(event: DshRuntimeEvent) {
  const data = event.data || {};
  if (event.type === "session") {
    const sessionId = String(data.session_id || "");
    const cwd = String(data.cwd || "");
    pushDshStream({
      kind: "status",
      title: "会话已建立",
      detail: `session: ${sessionId}${cwd ? `\ncwd: ${cwd}` : ""}`
    });
    return;
  }
  if (event.type === "status") {
    const phase = String(event.phase || "");
    pushDshStream({
      kind: "status",
      title: DSH_STATUS_PHASE_LABELS[phase] || "状态更新",
      detail: compact(data)
    });
    return;
  }
  if (event.type === "thinking") {
    pushDshStream({ kind: "thinking", title: "思考", detail: String(data.text || ""), expanded: false });
    return;
  }
  if (event.type === "text") {
    pushDshStream({ kind: "text", title: "助手输出", detail: String(data.text || "") });
    return;
  }
  if (event.type === "tool_call") {
    pushDshStream({
      kind: "tool_call",
      title: String(data.tool || "tool"),
      detail: formatJson(data.input),
      callId: String(data.call_id || "")
    });
    return;
  }
  if (event.type === "tool_result") {
    pushDshStream({
      kind: "tool_result",
      title: String(data.call_id || "tool result"),
      detail: String(data.result ?? ""),
      status: String(data.status || ""),
      callId: String(data.call_id || "")
    });
    return;
  }
  if (event.type === "final") {
    pushDshStream({ kind: "final", title: "最终回答", detail: String(data.text || "") });
    return;
  }
  if (event.type === "error") {
    pushDshStream({
      kind: "error",
      title: String(data.code || "error"),
      detail: String(data.message || "DSH 报告了执行错误")
    });
    return;
  }
  pushDshStream({
    kind: "status",
    title: event.source_type || String(event.type),
    detail: compact(data)
  });
}

function pushDshStream(item: { kind: DshStreamKind; title: string; detail: string; status?: string; callId?: string; expanded?: boolean }) {
  dshStream.value.push({
    index: dshStream.value.length + 1,
    kind: item.kind,
    title: item.title,
    detail: item.detail,
    status: item.status,
    callId: item.callId,
    expanded: item.expanded ?? true
  });
}

function toggleDshItem(item: DshStreamItem) {
  item.expanded = !item.expanded;
}

function dshKindLabel(kind: DshStreamKind) {
  return DSH_KIND_LABELS[kind] || kind;
}

function buildDshDiagnosis() {
  if (provider.value === "acp") return buildAcpDiagnosis();
  if (dshPhase.value === "cancelled") {
    return {
      kind: "failed",
      title: "运行已取消",
      message: "本地 DSH 回合已被停止，没有导入运行结果。可以修改 prompt 后重新开始。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  if (dshPhase.value === "failed" || dshPhase.value === "timeout") {
    return {
      kind: "failed",
      title: dshPhaseLabel.value,
      message: error.value || "本地 DSH 回合未完成，请查看对话流中的错误事件。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  if (running.value || dshPhase.value === "running" || dshPhase.value === "stopping") {
    return {
      kind: "running",
      title: dshPhaseLabel.value,
      message: "正在消费本地 DSH 事件流，思考、工具调用和结果会实时出现在对话流中。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  if (dshPhase.value === "imported" || dshPhase.value === "judged") {
    return {
      kind: "success",
      title: dshPhaseLabel.value,
      message: finalRun.value
        ? `已从 DSH 会话导入运行 ${finalRun.value.name}，可打开运行查看确定性审计，或触发 Agent Judge。`
        : "DSH 会话已导入运行。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  return {
    kind: "idle",
    title: "等待开始本地 DSH 会话",
    message: "选择后端授权的工作目录与 profile 后发送 prompt；本地 DSH 会话结束后会导入为可审计的 run。",
    recommendedResponse: "",
    actionLabel: ""
  };
}

// ACP 诊断必须区分「回合结束」和「run 已发布」：只有 run_imported 才算成功。
function buildAcpDiagnosis() {
  if (dshAcpPhase.value === "cancelled") {
    return {
      kind: "failed",
      title: "ACP 回合已取消",
      message: "回合被取消，没有导入运行结果。未回答的权限请求已按 cancelled 回复 DSH。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  if (dshAcpPhase.value === "failed") {
    return {
      kind: "failed",
      title: "ACP 回合未发布运行",
      message: error.value || "ACP 回合失败或导入阶段失败，请查看对话流中的 error 事件。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  if (dshAcpPhase.value === "settled") {
    return {
      kind: "needs_input",
      title: "回合已结束，但还没有发布运行",
      message: `stop_reason=${dshAcpStopReason.value || "unknown"} 只说明 session/prompt 结束，不代表任务成功；请等待 run_imported 或 import 阶段的 error。`,
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  if (running.value || dshAcpPhase.value === "running" || dshAcpPhase.value === "stopping") {
    return {
      kind: "running",
      title: "ACP 回合进行中",
      message: "长驻 ACP 进程正在流式返回更新；权限请求会暂停回合，直到你选择 allow-once 或 reject-once。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  if (dshAcpPhase.value === "imported") {
    return {
      kind: "success",
      title: "已发布 Auditrail run",
      message: finalRun.value
        ? `已从 ACP 会话导入运行 ${finalRun.value.name}，可打开运行查看确定性审计，或触发 Agent Judge。`
        : "ACP 会话已导入运行。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  return {
    kind: "idle",
    title: "等待开始 ACP 交互会话",
    message: "创建或恢复一个后端授权的工作目录下的 ACP 会话，然后发送 prompt；只有 run_imported 才表示标准 Auditrail run 已发布。",
    recommendedResponse: "",
    actionLabel: ""
  };
}

onMounted(async () => {
  await loadDshOptions();
  await loadDshAcpOptions();
});

function selectSpan(span: RuntimeSpan) {
  selectedSpanId.value = span.spanId;
}

function toggleCollapsed(span: RuntimeSpan) {
  const next = new Set(collapsedSpanIds.value);
  if (next.has(span.spanId)) {
    next.delete(span.spanId);
  } else {
    next.add(span.spanId);
  }
  collapsedSpanIds.value = next;
}

function replayFromHere(span: RuntimeSpan | null) {
  if (!span) return;
  appendLog("replay_requested", {
    spanId: span.spanId,
    name: span.name,
    input: span.input,
    context: {
      traceId: activeTrace.value.traceId,
      sessionId: activeTrace.value.sessionId
    }
  });
}

function useRecommendedResponse() {
  if (!runtimeDiagnosis.value.recommendedResponse) return;
  chatResponse.value = runtimeDiagnosis.value.recommendedResponse;
}

function appendLiveGraphEvent(event: string, payload: any) {
  if (event === "graph_update") return;
  const node = graphNodeFromEvent(event, payload);
  if (!node) return;
  const lastNode = graph.value.nodes[graph.value.nodes.length - 1];
  const previous = graph.value.current_node || lastNode?.id || null;
  const exists = graph.value.nodes.some((item) => item.id === node.id);
  const nodes = exists
    ? graph.value.nodes.map((item) => (item.id === node.id ? { ...item, ...node } : item))
    : [...graph.value.nodes, node];
  const edges = exists || !previous ? graph.value.edges : [...graph.value.edges, { from: previous, to: node.id }];
  graph.value = {
    nodes,
    edges,
    current_node: node.id
  };
}

function graphNodeFromEvent(event: string, payload: any) {
  if (event === "remote_step") {
    const stage = String(payload?.stage || "remote step");
    return {
      id: `remote-${stage}`,
      type: "tool",
      label: stage,
      status: payload?.status === "ok" ? "ok" : "running",
      detail: compact(payload)
    };
  }
  if (event === "files_uploaded") {
    return {
      id: `files-${payload?.session_id || logs.value.length}`,
      type: "tool_result",
      label: "Files uploaded",
      status: "ok",
      detail: compact(payload?.files || payload)
    };
  }
  if (event === "needs_interaction") {
    return {
      id: `interaction-${payload?.session_id || logs.value.length}`,
      type: "assistant",
      label: "Waiting for confirmation",
      status: "running",
      detail: compact(payload?.message || payload)
    };
  }
  if (event === "auto_interaction") {
    return {
      id: `auto-${payload?.kind || "continue"}-${logs.value.length}`,
      type: "assistant",
      label: `auto ${payload?.kind || "continue"}`,
      status: "ok",
      detail: compact(payload)
    };
  }
  if (event === "error") {
    const stage = String(payload?.stage || "runtime");
    return {
      id: `error-${stage}-${logs.value.length}`,
      type: "remote_error",
      label: `${stage} failed`,
      status: "error",
      detail: compact(payload)
    };
  }
  if (event !== "agent_event") return null;

  const eventName = String(payload?.name || "agent_event");
  if (isNoisyAgentEvent(eventName)) return null;
  const inner = payload?.payload;
  const toolName = extractToolName(inner) || extractToolName(payload);
  if (toolName) {
    return {
      id: `live-tool-${toolName}-${logs.value.length}`,
      type: toolName === "skill_view" ? "skill" : "tool",
      label: toolName === "skill_view" ? skillLabel(inner) : toolName,
      status: eventName.includes("result") || eventName.includes("end") ? "ok" : "running",
      detail: compact(inner || payload)
    };
  }
  if (["clarify", "approval", "stream_end", "cancelled", "error"].includes(eventName)) {
    return {
      id: `agent-${eventName}-${logs.value.length}`,
      type: eventName === "stream_end" ? "final" : "assistant",
      label: eventName,
      status: eventName === "error" || eventName === "cancelled" ? "error" : "ok",
      detail: compact(inner || payload)
    };
  }
  return {
    id: `agent-${eventName}-${logs.value.length}`,
    type: "assistant",
    label: eventName,
    status: "running",
    detail: compact(inner || payload)
  };
}

function extractToolName(payload: any): string {
  if (!payload || typeof payload !== "object") return "";
  const direct = payload.tool_name || (typeof payload.tool === "string" ? payload.tool : payload.tool?.name) || payload.name;
  if (typeof direct === "string" && isExplicitToolName(direct, payload)) {
    return direct.trim();
  }
  const fnName = payload.function?.name || payload.tool_call?.function?.name || payload.tool_call?.name;
  if (typeof fnName === "string" && fnName.trim() && isConcreteToolName(fnName)) return fnName.trim();
  const calls = payload.tool_calls || payload.toolCalls;
  if (Array.isArray(calls) && calls.length) {
    return extractToolName(calls[0]);
  }
  return "";
}

function isNoisyAgentEvent(eventName: string) {
  return [
    "delta",
    "message",
    "ping",
    "heartbeat",
    "reasoning",
    "reasoning_delta",
    "reasoning_summary",
    "thinking",
    "token"
  ].includes(eventName);
}

function isExplicitToolName(name: string, payload: any) {
  const value = name.trim();
  if (!value || isNoisyAgentEvent(value) || value === "agent_event" || !isConcreteToolName(value)) return false;
  return Boolean(
    payload.tool_name ||
      payload.tool ||
      payload.function ||
      payload.tool_call ||
      payload.tool_calls ||
      payload.toolCalls ||
      payload.arguments !== undefined ||
      payload.call_id ||
      payload.tool_call_id
  );
}

function isConcreteToolName(name: string) {
  const value = String(name || "").trim().toLowerCase();
  if (!value) return false;
  return ![
    "tool",
    "unknown",
    "tool_call",
    "tool_result",
    "tool_use",
    "function_call",
    "assistant tool request",
    "files uploaded"
  ].includes(value);
}

function skillLabel(payload: any) {
  const args = payload?.arguments || payload?.function?.arguments || payload?.tool_call?.function?.arguments;
  const parsed = typeof args === "string" ? safeJson(args) : args;
  const name = parsed && typeof parsed === "object" ? parsed.name : "";
  return name ? `skill_view: ${name}` : "skill_view";
}

function safeJson(value: string) {
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function onFilesSelected(event: Event) {
  const input = event.target as HTMLInputElement;
  selectedFiles.value = Array.from(input.files || []);
}

async function respondClarify() {
  if (!pendingClarify.value || !clarifyResponse.value.trim()) return;
  try {
    await apiPostJson("/api/live/clarify/respond", {
      ...connectionPayload(),
      session_id: pendingClarify.value.session_id || pendingClarify.value._session_id || currentSessionId.value,
      response: clarifyResponse.value.trim()
    });
    appendLog("clarify_response", { response: clarifyResponse.value.trim() });
    pendingClarify.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function respondApproval(choice: "once" | "session" | "always" | "deny") {
  if (!pendingApproval.value) return;
  try {
    await apiPostJson("/api/live/approval/respond", {
      ...connectionPayload(),
      session_id: pendingApproval.value.session_id || pendingApproval.value._session_id || currentSessionId.value,
      choice,
      approval_id: pendingApproval.value.approval_id || undefined
    });
    appendLog("approval_response", { choice });
    pendingApproval.value = null;
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  }
}

async function runAgentJudge() {
  if (!finalRun.value?.name) return;
  judging.value = true;
  error.value = "";
  try {
    const result = await apiPost<{ report: JudgeReport }>(`/api/runs/${encodeURIComponent(finalRun.value.name)}/judge`);
    judge.value = result.report;
    if (provider.value === "dsh" && dshPhase.value === "imported") {
      dshPhase.value = "judged";
    }
    await reloadRuns();
  } catch (err) {
    error.value = err instanceof Error ? err.message : String(err);
  } finally {
    judging.value = false;
  }
}

async function openFinalRun() {
  if (finalRun.value?.name) {
    await router.push({ name: "run", params: { runId: finalRun.value.name } });
  }
}

async function openTrajectory() {
  if (finalRun.value?.name) {
    await router.push({ name: "trajectory", params: { runId: finalRun.value.name } });
  }
}

async function normalizedPayload(): Promise<LiveEvaluatePayload> {
  return {
    ...form.value,
    base_url: form.value.base_url.trim(),
    user_id: form.value.user_id.trim(),
    token: form.value.token?.trim() || undefined,
    model: form.value.model?.trim() || undefined,
    title: form.value.title?.trim() || "auditrail runtime evaluation",
    skill: form.value.skill?.trim() || undefined,
    message: form.value.message.trim(),
    acceptance_criteria: form.value.acceptance_criteria.trim(),
    files: await encodeFiles(selectedFiles.value),
    max_turns: Number(form.value.max_turns || 20),
    timeout: Number(form.value.timeout || 60),
    auto_continue: Boolean(form.value.auto_continue),
    auto_continue_limit: Number(form.value.auto_continue_limit || 8)
  };
}

function connectionPayload() {
  return {
    base_url: form.value.base_url.trim(),
    user_id: form.value.user_id.trim(),
    token: form.value.token?.trim() || undefined,
    timeout: Number(form.value.timeout || 60)
  };
}

async function encodeFiles(files: File[]) {
  return Promise.all(
    files.map(async (file) => ({
      filename: file.name,
      content_base64: await fileToBase64(file)
    }))
  );
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error || new Error("Failed to read file."));
    reader.onload = () => {
      const value = String(reader.result || "");
      resolve(value.includes(",") ? value.split(",", 2)[1] : value);
    };
    reader.readAsDataURL(file);
  });
}

function recommendedClarifyResponse(payload: any) {
  const text = compact(payload || {});
  if (/confirm|确认|recommend|推荐/i.test(text)) {
    return "继续使用当前推荐方案。";
  }
  return "";
}

function appendLog(event: string, payload: any) {
  logs.value.push({
    index: logs.value.length + 1,
    event,
    text: compact(payload)
  });
}

function compact(payload: any): string {
  const text = typeof payload === "string" ? payload : JSON.stringify(payload);
  return text.length > 220 ? `${text.slice(0, 217)}...` : text;
}

function buildRuntimeDiagnosis() {
  // 本地 DSH 是通用运行时：不使用远程 agent 的 PPT 续跑启发式。
  if (provider.value === "dsh" || provider.value === "acp") return buildDshDiagnosis();
  if (pendingChatInteraction.value) {
    const recommended = pendingChatInteraction.value.recommended_response || "继续";
    return {
      kind: "needs_input",
      title: "运行已暂停，等待继续指令",
      message: diagnosisMessage(String(pendingChatInteraction.value.message || "")),
      recommendedResponse: recommended,
      actionLabel: recommended.includes("PPT") || recommended.includes("继续") ? "填入继续指令" : "填入确认回复"
    };
  }
  const continuation = continuationPromptFromTrace();
  if (continuation) {
    return {
      kind: "needs_input",
      title: "运行已暂停，等待继续指令",
      message: diagnosisMessage(continuation),
      recommendedResponse: "继续，补齐缺失文件并生成最终 PPT。",
      actionLabel: "填入继续指令"
    };
  }
  const overallStatus = normalizeTraceStatus(activeTrace.value.status);
  if (running.value || overallStatus === "running") {
    return {
      kind: "running",
      title: "运行中",
      message: "当前节点会持续高亮；如果 agent 需要确认，这里会显示可执行的下一步。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  if (overallStatus === "success") {
    return {
      kind: activeTrace.value.spans.length ? "success" : "idle",
      title: activeTrace.value.spans.length ? "运行已完成" : "等待开始评测",
      message: activeTrace.value.spans.length ? "Trace 已保存，可查看节点详情和时间线。" : "发送评测后会在这里显示运行诊断和下一步建议。",
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  const failed = activeTrace.value.spans.find((span) => normalizeTraceStatus(span.status) === "failed");
  if (failed) {
    const output = typeof failed.output === "string" ? failed.output : JSON.stringify(failed.output || "");
    if (/missing spec_lock\.md/i.test(output)) {
      return {
        kind: "needs_input",
        title: "项目校验未通过，可继续修复",
        message: "缺少 spec_lock.md，说明 ppt-master 还停在生成流程中间。继续当前会话，让 agent 补齐 spec_lock.md 后重新校验并导出 PPT。",
        recommendedResponse: "继续，补齐 spec_lock.md，重新校验项目并生成最终 PPT。",
        actionLabel: "填入修复指令"
      };
    }
    if (isRemoteStageFailure(failed)) {
      return {
        kind: "failed",
        title: remoteFailureTitle(failed),
        message: failureMessage(failed),
        recommendedResponse: "",
        actionLabel: ""
      };
    }
    return {
      kind: "failed",
      title: "运行失败在一个工具节点",
      message: failureMessage(failed),
      recommendedResponse: "",
      actionLabel: ""
    };
  }
  return {
    kind: activeTrace.value.spans.length ? "success" : "idle",
    title: activeTrace.value.spans.length ? "运行已完成" : "等待开始评测",
    message: activeTrace.value.spans.length ? "Trace 已保存，可查看节点详情和时间线。" : "发送评测后会在这里显示运行诊断和下一步建议。",
    recommendedResponse: "",
    actionLabel: ""
  };
}

function continuationPromptFromTrace() {
  for (const span of [...activeTrace.value.spans].reverse()) {
    const text = `${formatJson(span.input)}\n${formatJson(span.output)}`;
    if (/调用次数限制|调用次数上限|工具调用次数|尚未执行最终|请回复|接着做|回复[“"”']?\*{0,2}继续|生成\s*PPT/i.test(text)) {
      return text;
    }
  }
  return "";
}

function diagnosisMessage(message: string) {
  if (/调用次数限制|调用次数上限|工具调用次数|尚未执行最终|请回复|接着做|回复.*继续|生成\s*PPT/i.test(message)) {
    return "Agent 已完成规划和部分项目文件，但还没有导出最终 PPT。继续当前 session 即可让它补齐缺失文件并生成产物。";
  }
  if (/请确认|确认后|waiting for confirmation|please confirm/i.test(message)) {
    return "Agent 正在等待你的确认。确认后会继续执行后续工具调用。";
  }
  return message || "Agent 正在等待你的输入。";
}

function failureMessage(span: RuntimeSpan) {
  const output = typeof span.output === "string" ? span.output : JSON.stringify(span.output || "");
  const text = `${span.name}\n${formatJson(span.input)}\n${output}\n${span.error?.message || ""}`;
  if (/create_session/i.test(text) && /HTTP 502/i.test(text)) {
    return "远端 Agent 服务在创建会话时返回 HTTP 502。此时还没有进入 ppt-master，请检查服务器地址是否正确、远端服务是否正常、网关/后端日志是否有 create_session 报错。";
  }
  if (/start_chat/i.test(text) && /HTTP 502/i.test(text)) {
    return "远端 Agent 服务在启动对话时返回 HTTP 502。会话可能已创建，但对话流没有启动，请检查远端 chat 接口和服务日志。";
  }
  if (/HTTP 502/i.test(text)) {
    return "远端 Agent 服务返回 HTTP 502，属于服务端或网关错误，不是 ppt-master 工具执行失败。请先检查远端服务状态和日志。";
  }
  if (/missing spec_lock\.md/i.test(output)) {
    return "项目校验未通过：缺少 spec_lock.md。继续当前会话，让 agent 补齐 spec_lock.md 后重新校验并生成 PPT。";
  }
  if (span.error?.message) return span.error.message;
  return "查看右侧节点详情中的 Output 和 Error，确认失败原因。";
}

function isRemoteStageFailure(span: RuntimeSpan) {
  const sourceType = String(span.attributes?.sourceType || "");
  return sourceType === "remote_error" || /create_session|start_chat|fetch_session|stream_events/i.test(span.name);
}

function remoteFailureTitle(span: RuntimeSpan) {
  if (/create_session/i.test(span.name)) return "远端服务创建会话失败";
  if (/start_chat/i.test(span.name)) return "远端服务启动对话失败";
  if (/fetch_session/i.test(span.name)) return "远端服务拉取会话失败";
  return "远端 Agent 服务调用失败";
}

function attentionSpanId() {
  const failed = activeTrace.value.spans.find((span) => normalizeTraceStatus(span.status) === "failed");
  if (failed) return failed.spanId;
  const spans = activeTrace.value.spans;
  return spans[spans.length - 1]?.spanId || "";
}

function flattenTrace(spans: RuntimeSpan[], collapsed: Set<string>) {
  const children = new Map<string, RuntimeSpan[]>();
  const roots: RuntimeSpan[] = [];
  for (const span of spans) {
    const parent = span.parentSpanId || "";
    if (parent && spans.some((item) => item.spanId === parent)) {
      const list = children.get(parent) || [];
      list.push(span);
      children.set(parent, list);
    } else {
      roots.push(span);
    }
  }
  const rows: Array<{ span: RuntimeSpan; depth: number; hasChildren: boolean; collapsed: boolean }> = [];
  const visit = (span: RuntimeSpan, depth: number) => {
    const list = children.get(span.spanId) || [];
    const isCollapsed = collapsed.has(span.spanId);
    rows.push({ span, depth, hasChildren: list.length > 0, collapsed: isCollapsed });
    if (!isCollapsed) {
      list.forEach((child) => visit(child, depth + 1));
    }
  };
  roots.forEach((span) => visit(span, 0));
  return rows;
}

function traceFromGraph(runtimeGraph: RuntimeGraph, sessionId: string, agentName: string, agentVersion: string): RuntimeTrace {
  const spans = runtimeGraph.nodes.map((node, index) => spanFromGraphNode(node, index, runtimeGraph));
  const failed = spans.some((span) => normalizeTraceStatus(span.status) === "failed");
  const hasRunning = spans.some((span) => normalizeTraceStatus(span.status) === "running");
  return {
    traceId: sessionId || "live-preview",
    sessionId,
    agentName: agentName || "Agent Runtime",
    agentVersion: agentVersion || "",
    startTime: spans[0]?.startTime || "",
    endTime: spans[spans.length - 1]?.endTime || "",
    status: running.value || hasRunning ? "running" : failed ? "failed" : spans.length ? "success" : "pending",
    totalLatency: Math.max(spans.length - 1, 0),
    totalTokens: 0,
    cost: 0,
    metrics: {
      steps: spans.length,
      llmCalls: spans.filter((span) => span.type === "LLM").length,
      toolCalls: spans.filter((span) => span.type === "Tool").length,
      skillCalls: spans.filter((span) => span.type === "Skill").length
    },
    spans
  };
}

function spanFromGraphNode(node: RuntimeGraphNode, index: number, runtimeGraph: RuntimeGraph): RuntimeSpan {
  const parent = runtimeGraph.edges.find((edge) => edge.to === node.id)?.from || null;
  const startTime = new Date(Date.now() + index * 1000).toISOString();
  const toolName = toolNameFromGraphNode(node);
  return {
    spanId: node.id,
    parentSpanId: parent,
    name: node.label,
    type: spanTypeFromGraph(node.type),
    startTime,
    endTime: startTime,
    latency: 0,
    input: node.detail || "",
    output: "",
    status: statusFromGraph(node.status),
    tokenUsage: { input: 0, output: 0, total: 0 },
    error: node.status === "error" ? { type: "RuntimeError", message: node.detail || node.label } : null,
    attributes: { sourceType: node.type, ...(toolName ? { toolName } : {}) }
  };
}

function toolNameFromGraphNode(node: RuntimeGraphNode) {
  const label = String(node.label || "").trim();
  const skillMatch = label.match(/^skill_view:\s*(.+)$/);
  if (skillMatch) return skillMatch[1].trim();
  if (node.type === "skill") return isConcreteToolName(label) ? label : "skill_view";
  if (node.type === "tool" || node.type === "tool_result") {
    const resultMatch = label.match(/^(.*?)\s+result$/);
    const name = (resultMatch ? resultMatch[1] : label).trim();
    return isConcreteToolName(name) ? name : "";
  }
  return "";
}

function spanTypeFromGraph(type: string) {
  if (type === "skill") return "Skill";
  if (type === "tool" || type === "tool_result") return "Tool";
  if (type === "remote_error") return "Agent";
  if (type === "assistant" || type === "final" || type === "user") return "Agent";
  return "Agent";
}

function statusFromGraph(status: string) {
  if (status === "ok") return "success";
  if (status === "error") return "failed";
  return status || "running";
}

function normalizeTraceStatus(status: unknown) {
  const value = String(status || "").toLowerCase();
  if (value === "ok") return "success";
  if (value === "error") return "failed";
  return value || "pending";
}

function sumTokens(spans: RuntimeSpan[]) {
  return spans.reduce((total, span) => total + Number(span.tokenUsage?.total || 0), 0);
}

function formatCost(value: unknown) {
  const numeric = Number(value || 0);
  return `$${numeric.toFixed(2)}`;
}

function formatDuration(value: unknown) {
  const seconds = Number(value || 0);
  if (seconds >= 1) return `${seconds.toFixed(1)}s`;
  return `${Math.round(seconds * 1000)}ms`;
}

function formatJson(value: unknown) {
  if (value === null || value === undefined || value === "") return "None";
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2);
}

function parseTime(value: string) {
  const time = Date.parse(value || "");
  return Number.isFinite(time) ? time : Date.now();
}

function formatOffset(start: number, current: number) {
  const seconds = Math.max(Math.round((current - start) / 1000), 0);
  const minute = String(Math.floor(seconds / 60)).padStart(2, "0");
  const second = String(seconds % 60).padStart(2, "0");
  return `${minute}:${second}`;
}

function timelineLabel(span: RuntimeSpan) {
  if (span.name === "User Request") return "Agent Start";
  if (span.type === "LLM") return `${span.name} Completed`;
  if (span.type === "Tool" || span.type === "Skill") {
    const kind = spanKindLabel(span);
    return span.status === "running" ? `Calling ${kind}` : `${kind} Completed`;
  }
  return span.name;
}

function spanKindLabel(span: RuntimeSpan) {
  const type = String(span.type || "span");
  const toolName = toolNameFromSpan(span);
  if ((type === "Tool" || type === "Skill") && toolName) {
    return `${type === "Skill" ? "skill" : "tool"}(${toolName})`;
  }
  return type;
}

function toolNameFromSpan(span: RuntimeSpan) {
  const fromAttr = String(span.attributes?.toolName || "").trim();
  if (isConcreteToolName(fromAttr)) return fromAttr;
  const name = String(span.name || "").trim();
  const skillMatch = name.match(/^skill_view:\s*(.+)$/);
  if (skillMatch) return skillMatch[1].trim();
  const resultMatch = name.match(/^(.*?)\s+result$/);
  if (resultMatch && isConcreteToolName(resultMatch[1])) return resultMatch[1].trim();
  return isConcreteToolName(name) ? name : "";
}

function spanMetric(span: RuntimeSpan) {
  if (span.type === "LLM") {
    return `${span.attributes?.model || "model"} · ${span.tokenUsage?.total || 0} tokens · ${formatDuration(span.latency)}`;
  }
  if (span.type === "Tool" || span.type === "Skill") {
    return `${span.attributes?.toolName || span.name} · ${formatDuration(span.latency)}`;
  }
  if (span.type === "Retriever") {
    return `TopK ${span.attributes?.topK || "-"} · Recall ${span.attributes?.recall || "-"}`;
  }
  return formatDuration(span.latency);
}
</script>

<template>
  <div class="page live-page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">Runtime evaluation</p>
        <h2>运行时评测</h2>
        <p class="lede">选择远程 Agent 或本地 DSH 运行时，实时观察思考、工具调用与状态事件，并把会话保存为可审计的 run。</p>
      </div>
      <div class="hero-actions">
        <button class="primary" type="button" :disabled="!canSubmit" @click="submitActive">
          {{ submitLabel }}
        </button>
        <button class="ghost" type="button" :disabled="!finalRun" @click="openFinalRun">
          打开运行
        </button>
        <button class="ghost" type="button" :disabled="!finalRun" @click="openTrajectory">
          查看轨迹
        </button>
      </div>
    </header>

    <p v-if="error" class="banner error">{{ error }}</p>

    <section class="ops-bar reveal" aria-label="Live evaluation context">
      <div class="ops-left">
        <span class="filter-pill" :class="{ active: provider === 'remote' }">远程 Agent</span>
        <span class="filter-pill" :class="{ active: provider === 'dsh' }">Local DSH</span>
        <span class="filter-pill" :class="{ active: provider === 'acp' }">Local DSH ACP</span>
        <span class="filter-pill active">{{ provider === "remote" ? "Live trace" : "本地事件流" }}</span>
      </div>
      <div class="ops-right">
        <span class="context-token">session: {{ currentSessionId || "not started" }}</span>
        <span class="context-token">status: {{ screenStatus }}</span>
      </div>
    </section>

    <section class="live-grid reveal">
      <form class="panel live-form" @submit.prevent="submitActive">
        <div class="panel-head">
          <h3>评测输入</h3>
          <span class="chip quiet">{{ running ? "streaming" : "ready" }}</span>
        </div>
        <div class="provider-switch" role="radiogroup" aria-label="运行时提供方">
          <button
            type="button"
            class="provider-option"
            :class="{ active: provider === 'remote' }"
            :aria-pressed="provider === 'remote'"
            :disabled="running"
            @click="selectProvider('remote')"
          >
            远程 Agent
          </button>
          <button
            type="button"
            class="provider-option"
            :class="{ active: provider === 'dsh' }"
            :aria-pressed="provider === 'dsh'"
            :disabled="running || !dshEnabled"
            @click="selectProvider('dsh')"
          >
            Local DSH
          </button>
          <button
            type="button"
            class="provider-option"
            :class="{ active: provider === 'acp' }"
            :aria-pressed="provider === 'acp'"
            :disabled="running || !acpEnabled"
            @click="selectProvider('acp')"
          >
            Local DSH ACP
          </button>
        </div>

        <template v-if="provider === 'remote'">
        <label>
          <span>服务器地址</span>
          <input v-model="form.base_url" type="url" placeholder="http://192.168.1.238:8788" required />
        </label>
        <label>
          <span>用户 ID</span>
          <input v-model="form.user_id" type="text" placeholder="auditrail-test" required />
        </label>
        <div class="form-row">
          <label>
            <span>Skill</span>
            <input v-model="form.skill" type="text" placeholder="ppt-master" />
          </label>
          <label>
            <span>会话标题</span>
            <input v-model="form.title" type="text" placeholder="ppt-master runtime evaluation" />
          </label>
        </div>
        <div class="form-row">
          <label>
            <span>模型</span>
            <input v-model="form.model" type="text" placeholder="默认" />
          </label>
          <label>
            <span>最大轮数</span>
            <input v-model.number="form.max_turns" type="number" min="1" step="1" />
          </label>
          <label>
            <span>超时秒数</span>
            <input v-model.number="form.timeout" type="number" min="1" step="1" />
          </label>
        </div>
        <label class="checkbox-row">
          <span>自动确认 / 继续</span>
          <input v-model="form.auto_continue" type="checkbox" />
          <small>通过真实 clarify / approval / 同会话消息自动回复，不伪造用户确认。默认最多自动继续 8 次。</small>
        </label>
        <label v-if="form.auto_continue">
          <span>自动继续次数</span>
          <input v-model.number="form.auto_continue_limit" type="number" min="1" step="1" />
        </label>
        <label>
          <span>Token</span>
          <input v-model="form.token" type="password" autocomplete="off" placeholder="可选" />
        </label>
        <label>
          <span>上传材料</span>
          <input type="file" multiple @change="onFilesSelected" />
        </label>
        <div v-if="selectedFiles.length" class="file-list">
          <span v-for="file in selectedFiles" :key="`${file.name}-${file.size}`">{{ file.name }}</span>
        </div>
        <label>
          <span>Prompt</span>
          <textarea
            v-model="form.message"
            rows="5"
            placeholder="请调用 ppt-master，根据上传材料生成一份 10 页中文学术汇报 PPT..."
            required
          />
        </label>
        <label>
          <span>Agent Judge 验收标准</span>
          <textarea
            v-model="form.acceptance_criteria"
            rows="5"
            placeholder="- 必须调用指定 skill&#10;- 不允许伪造用户确认&#10;- 最终回答必须说明产物路径"
          />
        </label>
        <button class="primary wide" type="submit" :disabled="!canSubmit">
          {{ running ? "正在消费事件" : "发送给 agent" }}
        </button>
        </template>

        <template v-else-if="provider === 'dsh'">
        <p v-if="!dshOptionsLoaded" class="dsh-hint">正在读取后端授权清单…</p>
        <p v-else-if="!dshEnabled" class="banner error">
          {{ dshDisabledReason || "本地 DSH 运行时未启用" }}
        </p>
        <p v-if="dshOptionsError" class="banner error">{{ dshOptionsError }}</p>
        <label>
          <span>Profile（后端授权）</span>
          <select v-model="dshForm.profile" :disabled="running">
            <option v-for="item in dshProfiles" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label v-if="dshWorkspaces.length">
          <span>工作目录（后端授权）</span>
          <select v-model="dshForm.workspace" :disabled="running">
            <option v-for="item in dshWorkspaces" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label v-else>
          <span>工作目录（必须位于授权根目录内）</span>
          <input v-model="dshForm.workspace" type="text" list="dsh-workspace-roots" placeholder="/path/to/project" />
          <datalist id="dsh-workspace-roots">
            <option v-for="item in dshWorkspaceRoots" :key="item" :value="item" />
          </datalist>
        </label>
        <label>
          <span>超时秒数（上限 {{ dshMaxTimeout }}）</span>
          <input v-model.number="dshForm.timeout" type="number" min="1" :max="dshMaxTimeout" step="1" />
        </label>
        <label>
          <span>Prompt</span>
          <textarea
            v-model="dshForm.message"
            rows="5"
            placeholder="描述要本地 DSH 执行的任务，例如：运行相关测试并解释失败原因"
            required
          />
        </label>
        <label>
          <span>Agent Judge 验收标准</span>
          <textarea
            v-model="dshForm.acceptance_criteria"
            rows="5"
            placeholder="- 相关测试通过&#10;- 结论必须能回指证据"
          />
        </label>
        <div class="dsh-actions">
          <button class="primary wide" type="submit" :disabled="!canSubmit">
            {{ running ? "DSH 运行中" : dshSessionId ? "继续 DSH 会话" : "发送给本地 DSH" }}
          </button>
          <button class="ghost" type="button" :disabled="running || !dshSessionId" @click="newDshSession">
            新建会话
          </button>
          <button class="danger" type="button" :disabled="!running || !dshOperationId" @click="stopDshTurn">
            停止
          </button>
        </div>
        <p class="dsh-hint">session: {{ dshSessionId || "新会话（发送后由 DSH 分配）" }}</p>
        </template>

        <template v-else>
        <p v-if="!dshAcpOptionsLoaded" class="dsh-hint">正在读取 ACP 能力的协商结果…</p>
        <p v-else-if="!acpEnabled" class="banner error">
          {{ acpDisabledReason || "本地 DSH ACP 运行时未启用" }}
        </p>
        <p v-if="dshAcpOptionsError" class="banner error">{{ dshAcpOptionsError }}</p>

        <div class="acp-capabilities">
          <span class="context-token">ACP v{{ acpCapabilities.protocol_version ?? 1 }}</span>
          <span class="context-token">{{ acpCapabilities.running ? "进程运行中" : "进程未启动" }}</span>
          <span class="context-token">list: {{ acpCapabilities.supports_session_list ? "是" : "否" }}</span>
          <span class="context-token">resume: {{ acpCapabilities.supports_session_resume ? "是" : "否" }}</span>
          <span class="context-token">close: {{ acpCapabilities.supports_session_close ? "是" : "否" }}</span>
          <span class="context-token">图片 prompt: {{ acpCapabilities.supports_images ? "支持" : "不支持" }}</span>
        </div>
        <p class="dsh-hint">
          能力来自本次连接的真实协商结果；权限只提供 allow-once / reject-once，不提供 allow-always，也不自动批准。
        </p>

        <label v-if="dshWorkspaces.length">
          <span>工作目录（后端授权）</span>
          <select v-model="dshAcpForm.workspace" :disabled="running">
            <option v-for="item in dshWorkspaces" :key="item" :value="item">{{ item }}</option>
          </select>
        </label>
        <label v-else>
          <span>工作目录（必须位于授权根目录内）</span>
          <input v-model="dshAcpForm.workspace" type="text" list="dsh-workspace-roots" placeholder="/path/to/project" />
          <datalist id="dsh-workspace-roots">
            <option v-for="item in dshWorkspaceRoots" :key="item" :value="item" />
          </datalist>
        </label>
        <label>
          <span>Prompt</span>
          <textarea
            v-model="dshAcpForm.message"
            rows="5"
            placeholder="描述要 ACP 交互执行的任务，例如：运行相关测试并解释失败原因"
            required
          />
        </label>
        <label>
          <span>Agent Judge 验收标准</span>
          <textarea
            v-model="dshAcpForm.acceptance_criteria"
            rows="4"
            placeholder="- 相关测试通过&#10;- 结论必须能回指证据"
          />
        </label>
        <div class="dsh-actions">
          <button class="primary wide" type="submit" :disabled="!canSubmit">
            {{ running ? "ACP 回合进行中" : dshAcpSessionId ? "继续 ACP 会话" : "开始 ACP 会话" }}
          </button>
          <button class="ghost" type="button" :disabled="!canCreateAcpSession" @click="newAcpSession">
            新建会话
          </button>
          <button class="ghost" type="button" :disabled="running || !dshAcpSessionId" @click="closeAcpSession(dshAcpSessionId)">
            关闭会话
          </button>
          <button class="danger" type="button" :disabled="!running || !dshAcpOperationId" @click="stopAcpTurn">
            取消回合
          </button>
        </div>
        <p class="dsh-hint">
          session: {{ dshAcpSessionId || "尚未创建（发送或点击新建会话时由后端分配）" }}；每个 session 同时只允许一个
          active prompt。
        </p>
        <div v-if="dshAcpSessions.length" class="acp-session-list">
          <div class="panel-head">
            <h4>后端当前持有的 ACP 会话</h4>
            <span class="chip quiet">{{ dshAcpSessions.length }}</span>
          </div>
          <ul>
            <li v-for="session in dshAcpSessions" :key="session.session_id">
              <div>
                <strong>{{ session.session_id }}</strong>
                <small>{{ session.workspace }}</small>
                <small>state: {{ session.state }}{{ session.active_operation ? " · 回合进行中" : "" }}</small>
              </div>
              <div class="acp-session-actions">
                <button class="ghost small" type="button" :disabled="running" @click="resumeAcpSession(session)">
                  恢复
                </button>
                <button class="danger small" type="button" :disabled="running" @click="closeAcpSession(session.session_id)">
                  关闭
                </button>
              </div>
            </li>
          </ul>
        </div>
        </template>
      </form>

      <section class="panel graph-panel">
        <div class="panel-head">
          <h3>Agent Trace Explorer</h3>
          <span class="chip">{{ traceSummary.steps }} spans</span>
        </div>
        <div v-if="activeTrace.spans.length" class="trace-explorer">
          <section class="runtime-summary" aria-label="Agent Runtime Summary">
            <div>
              <span>Status</span>
              <strong :data-status="traceSummary.status">{{ traceSummary.status }}</strong>
            </div>
            <div>
              <span>Duration</span>
              <strong>{{ traceSummary.duration }}</strong>
            </div>
            <div>
              <span>Steps</span>
              <strong>{{ traceSummary.steps }}</strong>
            </div>
            <div>
              <span>LLM Calls</span>
              <strong>{{ traceSummary.llmCalls }}</strong>
            </div>
            <div>
              <span>Tool Calls</span>
              <strong>{{ traceSummary.toolCalls }}</strong>
            </div>
            <div>
              <span>Tokens</span>
              <strong>{{ traceSummary.tokens }}</strong>
            </div>
            <div>
              <span>Cost</span>
              <strong>{{ traceSummary.cost }}</strong>
            </div>
          </section>

          <section class="runtime-diagnosis" :class="runtimeDiagnosis.kind" aria-label="Runtime Diagnosis">
            <div>
              <span>Runtime Diagnosis</span>
              <strong>{{ runtimeDiagnosis.title }}</strong>
              <p>{{ runtimeDiagnosis.message }}</p>
              <code v-if="runtimeDiagnosis.recommendedResponse">{{ runtimeDiagnosis.recommendedResponse }}</code>
            </div>
            <div v-if="runtimeDiagnosis.recommendedResponse" class="diagnosis-actions">
              <button class="ghost small" type="button" @click="useRecommendedResponse">
                {{ runtimeDiagnosis.actionLabel }}
              </button>
              <button
                class="primary small"
                type="button"
                :disabled="!currentSessionId || running"
                @click="continueSession"
              >
                {{ running ? "继续运行中" : "发送并继续" }}
              </button>
            </div>
            <button
              v-else-if="selectedSpan"
              class="ghost small"
              type="button"
              @click="selectedSpanId = attentionSpanId() || selectedSpanId"
            >
              查看关键节点
            </button>
          </section>

          <div class="trace-workbench">
            <section class="trace-tree" aria-label="Trace Tree">
              <button
                v-for="row in traceRows"
                :key="row.span.spanId"
                type="button"
                class="trace-row"
                :class="[String(row.span.type).toLowerCase(), normalizeTraceStatus(row.span.status), { selected: row.span.spanId === selectedSpan?.spanId }]"
                :style="{ '--depth': row.depth }"
                @click="selectSpan(row.span)"
              >
                <span class="trace-indent">
                  <span
                    v-if="row.hasChildren"
                    class="trace-toggle"
                    role="button"
                    tabindex="0"
                    @click.stop="toggleCollapsed(row.span)"
                    @keydown.enter.stop="toggleCollapsed(row.span)"
                  >
                    {{ row.collapsed ? "+" : "-" }}
                  </span>
                </span>
                <span class="trace-kind" :title="spanKindLabel(row.span)">{{ spanKindLabel(row.span) }}</span>
                <strong>{{ row.span.name }}</strong>
                <small>{{ spanMetric(row.span) }}</small>
                <span class="trace-state">{{ normalizeTraceStatus(row.span.status) }}</span>
              </button>
            </section>

            <aside class="trace-detail" aria-label="Node Detail Panel">
              <div class="detail-head">
                <div>
                  <p class="eyebrow">{{ selectedSpan ? spanKindLabel(selectedSpan) : "Span" }} Details</p>
                  <h4>{{ selectedSpan?.name || "No span selected" }}</h4>
                </div>
                <button class="ghost small" type="button" :disabled="!selectedSpan" @click="replayFromHere(selectedSpan)">
                  Replay From Here
                </button>
              </div>
              <dl v-if="selectedSpan" class="detail-meta">
                <div>
                  <dt>Status</dt>
                  <dd :data-status="normalizeTraceStatus(selectedSpan.status)">{{ normalizeTraceStatus(selectedSpan.status) }}</dd>
                </div>
                <div>
                  <dt>Duration</dt>
                  <dd>{{ formatDuration(selectedSpan.latency) }}</dd>
                </div>
                <div>
                  <dt>Token</dt>
                  <dd>{{ selectedSpan.tokenUsage?.total || 0 }}</dd>
                </div>
                <div>
                  <dt>Error</dt>
                  <dd>{{ selectedSpan.error?.type || "None" }}</dd>
                </div>
              </dl>
              <div v-if="selectedSpan" class="detail-blocks">
                <label>
                  <span>Input</span>
                  <pre>{{ formatJson(selectedSpan.input) }}</pre>
                </label>
                <label>
                  <span>Output</span>
                  <pre>{{ formatJson(selectedSpan.output) }}</pre>
                </label>
                <label>
                  <span>Attributes</span>
                  <pre>{{ formatJson(selectedSpan.attributes) }}</pre>
                </label>
                <label v-if="selectedSpan.error">
                  <span>Error Message</span>
                  <pre>{{ selectedSpan.error.message }}</pre>
                </label>
              </div>
            </aside>
          </div>

          <section class="trace-timeline" aria-label="Trace Timeline">
            <article
              v-for="event in timelineEvents"
              :key="event.id"
              :class="event.status"
              @click="selectedSpanId = event.id"
            >
              <time>{{ event.offset }}</time>
              <span>{{ event.label }}</span>
            </article>
          </section>
        </div>
        <p v-else class="empty">
          {{ provider === "dsh" ? "开始本地 DSH 会话后，这里会显示来自后端投影的运行时 trace。" : "开始评测后，这里会实时显示 agent trace、LLM 调用、skill/tool 结果和时间线。" }}
        </p>
      </section>
    </section>

    <section v-if="provider === 'dsh'" class="panel dsh-panel reveal">
      <div class="panel-head">
        <h3>DSH 对话流</h3>
        <div class="dsh-panel-actions">
          <span class="chip" :data-status="dshPhase">{{ dshPhaseLabel }}</span>
          <button class="ghost small" type="button" :disabled="running || !dshSessionId" @click="newDshSession">
            新建会话
          </button>
          <button class="danger small" type="button" :disabled="!running || !dshOperationId" @click="stopDshTurn">
            停止
          </button>
        </div>
      </div>
      <div v-if="dshStream.length" class="chat-stream">
        <article v-for="item in dshStream" :key="item.index" class="chat-item" :class="item.kind">
          <header class="chat-item-head">
            <span class="chat-kind">{{ dshKindLabel(item.kind) }}</span>
            <strong>{{ item.title }}</strong>
            <span v-if="item.status" class="chat-status" :data-status="item.status">{{ item.status }}</span>
            <span v-if="item.callId" class="chat-call">{{ item.callId }}</span>
            <button
              v-if="item.kind === 'thinking'"
              class="ghost small"
              type="button"
              @click="toggleDshItem(item)"
            >
              {{ item.expanded ? "收起思考" : "展开思考" }}
            </button>
          </header>
          <pre v-if="item.kind !== 'thinking' || item.expanded">{{ item.detail }}</pre>
          <p v-else class="chat-collapsed">思考内容默认折叠（{{ item.detail.length }} 字符）</p>
        </article>
      </div>
      <p v-else class="empty">发送 prompt 后，这里会按顺序显示 thinking、助手文本、工具调用 / 结果、状态与错误事件。</p>
    </section>

    <section v-if="provider === 'acp'" class="panel dsh-panel reveal">
      <div class="panel-head">
        <h3>ACP 交互工作台</h3>
        <div class="dsh-panel-actions">
          <span class="chip" :data-status="dshAcpPhase">
            {{ dshAcpStatus || dshAcpPhase }} · {{ dshAcpPhaseLabel }}
          </span>
          <button class="danger small" type="button" :disabled="!running || !dshAcpOperationId" @click="stopAcpTurn">
            取消回合
          </button>
        </div>
      </div>

      <div class="acp-outcome">
        <div>
          <span>回合状态</span>
          <strong :data-status="dshAcpPhase">{{ ACP_PHASE_LABELS[dshAcpPhase] }}</strong>
        </div>
        <div>
          <span>stop reason</span>
          <strong>{{ dshAcpStopReason || "-" }}</strong>
        </div>
        <div>
          <span>Run</span>
          <strong>{{ finalRun?.name || "未发布" }}</strong>
        </div>
        <p class="acp-outcome-note">{{ acpStatusLabel }}。completed 只表示 ACP prompt 已结束，只有 run_imported 表示标准 Auditrail run 已成功发布。</p>
      </div>

      <div v-if="dshAcpPermission" class="acp-permission" role="alert">
        <div class="panel-head">
          <h4>权限请求（等待决定）</h4>
          <span class="chip warning">allow-once / reject-once</span>
        </div>
        <p class="report">
          DSH 请求对工具调用 <code>{{ dshAcpPermission.tool_call_id }}</code> 授权。回合在得到答复前保持阻塞，断开连接会取消该请求。
        </p>
        <ul class="acp-permission-options">
          <li v-for="option in dshAcpPermission.options" :key="option.optionId">
            <code>{{ option.optionId }}</code>
            <span>{{ option.name || option.kind || "" }}</span>
          </li>
        </ul>
        <div class="approval-actions">
          <button
            class="primary"
            type="button"
            :disabled="dshAcpPermissionBusy"
            @click="answerAcpPermission('allow-once')"
          >
            仅允许一次
          </button>
          <button
            class="danger"
            type="button"
            :disabled="dshAcpPermissionBusy"
            @click="answerAcpPermission('reject-once')"
          >
            仅拒绝一次
          </button>
        </div>
        <small class="acp-permission-note">不提供 allow-always，也不会自动批准。</small>
      </div>

      <div v-if="dshStream.length" class="chat-stream">
        <article v-for="item in dshStream" :key="item.index" class="chat-item" :class="item.kind">
          <header class="chat-item-head">
            <span class="chat-kind">{{ dshKindLabel(item.kind) }}</span>
            <strong>{{ item.title }}</strong>
            <span v-if="item.status" class="chat-status" :data-status="item.status">{{ item.status }}</span>
            <span v-if="item.callId" class="chat-call">{{ item.callId }}</span>
            <button
              v-if="item.kind === 'thinking'"
              class="ghost small"
              type="button"
              @click="toggleDshItem(item)"
            >
              {{ item.expanded ? "收起思考" : "展开思考" }}
            </button>
          </header>
          <pre v-if="item.kind !== 'thinking' || item.expanded">{{ item.detail }}</pre>
          <p v-else class="chat-collapsed">思考内容默认折叠（{{ item.detail.length }} 字符）</p>
        </article>
      </div>
      <p v-else class="empty">
        发送 prompt 后，这里会按顺序显示 started、runtime_event、permission_requested、completed，以及 run_imported 或
        import 阶段的 error。
      </p>
    </section>

    <section
      v-if="provider === 'remote' && (pendingClarify || pendingApproval || pendingChatInteraction)"
      class="interaction-grid reveal"
    >
      <div v-if="pendingChatInteraction" class="panel interaction-card">
        <div class="panel-head">
          <h3>同会话继续</h3>
          <span class="chip">message</span>
        </div>
        <p class="report interaction-preview">{{ pendingChatInteraction.message || compact(pendingChatInteraction) }}</p>
        <textarea v-model="chatResponse" rows="3" placeholder="输入给远端 agent 的后续回复，例如：确认" />
        <button class="primary" type="button" :disabled="!chatResponse.trim() || running" @click="continueSession">
          {{ running ? "继续运行中" : "发送并继续评测" }}
        </button>
      </div>

      <div v-if="pendingClarify" class="panel interaction-card">
        <div class="panel-head">
          <h3>等待确认 / 澄清</h3>
          <span class="chip">clarify</span>
        </div>
        <p class="report">{{ pendingClarify.question || compact(pendingClarify) }}</p>
        <textarea v-model="clarifyResponse" rows="3" placeholder="输入给远端 agent 的回复" />
        <button class="primary" type="button" :disabled="!clarifyResponse.trim()" @click="respondClarify">
          发送回复
        </button>
      </div>

      <div v-if="pendingApproval" class="panel interaction-card">
        <div class="panel-head">
          <h3>工具审批</h3>
          <span class="chip">approval</span>
        </div>
        <p class="report">{{ pendingApproval.description || pendingApproval.content || compact(pendingApproval) }}</p>
        <div class="approval-actions">
          <button class="primary" type="button" @click="respondApproval('once')">允许一次</button>
          <button class="ghost" type="button" @click="respondApproval('session')">本会话允许</button>
          <button class="ghost" type="button" @click="respondApproval('always')">总是允许</button>
          <button class="danger" type="button" @click="respondApproval('deny')">拒绝</button>
        </div>
      </div>
    </section>

    <section class="report-grid reveal">
      <div class="panel">
        <div class="panel-head">
          <h3>实时事件</h3>
          <span class="chip quiet">{{ logs.length }}</span>
        </div>
        <ol v-if="logs.length" class="event-log">
          <li v-for="item in logs" :key="item.index">
            <span>{{ item.event }}</span>
            <code>{{ item.text }}</code>
          </li>
        </ol>
        <p v-else class="empty">暂无事件。</p>
      </div>

      <div class="panel">
        <div class="panel-head">
          <h3>评测结果</h3>
          <button class="ghost small" type="button" :disabled="!finalRun || judging" @click="runAgentJudge">
            {{ judging ? "评测中" : "运行 Agent Judge" }}
          </button>
        </div>
        <dl class="judge-meta">
          <div>
            <dt>提供方</dt>
            <dd>{{ providerLabel }}</dd>
          </div>
          <div v-if="provider === 'dsh'">
            <dt>DSH 状态</dt>
            <dd :data-status="dshPhase">{{ dshPhaseLabel }}</dd>
          </div>
          <div v-if="provider === 'acp'">
            <dt>ACP 回合</dt>
            <dd :data-status="dshAcpPhase">{{ dshAcpPhaseLabel }}</dd>
          </div>
          <div v-if="provider === 'acp'">
            <dt>stop reason</dt>
            <dd>{{ dshAcpStopReason || "-" }}</dd>
          </div>
          <div>
            <dt>Run</dt>
            <dd>{{ finalRun?.name || "-" }}</dd>
          </div>
          <div>
            <dt>工具调用</dt>
            <dd>{{ audit?.tool_call_count ?? "-" }}</dd>
          </div>
          <div>
            <dt>工具错误</dt>
            <dd>{{ (audit?.tool_calls || []).filter((item: any) => item.status === "error").length }}</dd>
          </div>
          <div>
            <dt>Agent Judge</dt>
            <dd>{{ judge?.overall_status || "未运行" }}</dd>
          </div>
        </dl>
        <p class="report">{{ judge?.summary || "运行完成后可触发 Agent Judge，按自然语言验收标准生成证据化判断。" }}</p>
        <div v-if="finalRun" class="dsh-actions">
          <button class="ghost small" type="button" @click="openFinalRun">打开运行（审计）</button>
          <button class="ghost small" type="button" @click="openTrajectory">查看轨迹</button>
        </div>
        <ul v-if="findings.length" class="findings">
          <li v-for="(finding, index) in findings" :key="index">
            <span class="severity" :data-level="String(finding.severity || '').toLowerCase()">
              {{ finding.severity }}
            </span>
            <div>
              <strong>{{ finding.title || finding.category || "finding" }}</strong>
              <p>{{ finding.recommendation || finding.message }}</p>
            </div>
          </li>
        </ul>
      </div>
    </section>
  </div>
</template>
