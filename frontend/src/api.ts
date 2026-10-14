// 默认同源请求（由 Vite dev server 的 /api 代理转发到后端，避免跨域）；
// 需要直连远程后端时，显式设置 VITE_API_BASE_URL（会重新引入跨域，需在 CORS_ORIGINS 放行）。
const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "").replace(/\/$/, "");

export interface RunItem {
  name: string;
  path: string;
  has_summary: boolean;
  has_audit: boolean;
  has_trajectory: boolean;
  has_judge: boolean;
  server_host?: string;
  source_base_url?: string;
  server_netloc?: string;
}

export interface JudgeEvidence {
  source: string;
  message_index?: number;
  quote: string;
  reason?: string;
  location?: string;
}

export interface Finding {
  severity: string;
  category?: string;
  title?: string;
  type?: string;
  message?: string;
  recommendation?: string;
  evidence?: JudgeEvidence[];
}

export interface JudgeDimension {
  score: number;
  status: string;
  summary: string;
  evidence: JudgeEvidence[];
  recommendation: string;
  source: string;
}

export interface JudgeReliability {
  evidence_coverage: number;
  unsupported_claim_count: number;
  invalid_evidence_count: number;
  fact_inference_mismatch_count: number;
  valid_evidence_count: number;
  total_evidence_count: number;
  notes: string[];
  invalid_evidence?: JudgeEvidence[];
}

// 引擎来源信息，记录在 agent_judge.json 的 judge 字段中；旧报告没有该字段。
export interface JudgeProvenance {
  engine?: string;
  runtime?: string;
  profile?: string;
  workspace?: string;
  model?: string;
  [key: string]: unknown;
}

export interface JudgeReport {
  status?: string;
  overall_status?: string;
  completion_score?: number;
  confidence?: string;
  summary?: string;
  findings?: Finding[];
  dimensions?: Record<string, JudgeDimension>;
  judge_reliability?: JudgeReliability;
  judge?: JudgeProvenance;
  metrics?: Record<string, unknown>;
  charts?: Record<string, unknown>;
  // GET /api/runs/:id/judges/:engine 标注该报告属于哪个引擎；根报告没有该字段。
  judge_engine?: string;
}

// 一个 run 可以为每个裁判引擎保存一份报告。根目录 agent_judge.json|md 与
// chart_data.json 始终是最近一次发布的 active 报告，旧客户端继续可用。
export interface JudgeReportSummary {
  engine: string;
  available: boolean;
  published_at?: string;
  json_path: string;
  markdown_path: string;
  chart_path: string;
}

export interface JudgeReportList {
  run_id?: string;
  version?: number;
  active_engine: string;
  reports: JudgeReportSummary[];
}

// 单一取值 + 是否发生分歧；未知（unknown）低于真实判断，不算分歧也不算失败。
export interface JudgeStatusComparison {
  by_engine: Record<string, string>;
  disagreement: boolean;
}

export interface JudgeDimensionComparison {
  status: Record<string, string>;
  score: Record<string, number | null>;
  disagreement: boolean;
  unknown_engines: string[];
}

export interface JudgeReliabilityComparison {
  evidence_coverage: number;
  invalid_evidence_count: number;
  fact_inference_mismatch_count: number;
  valid_evidence_count: number;
  total_evidence_count: number;
}

export interface JudgeFactFinding {
  severity: string;
  category: string;
  title: string;
}

export interface JudgeComparisonReport {
  run_id?: string;
  active_engine: string;
  engines: string[];
  overall_status: JudgeStatusComparison;
  completion_score: {
    by_engine: Record<string, number>;
    spread: number;
    disagreement: boolean;
  };
  reliability: Record<string, JudgeReliabilityComparison>;
  dimensions: Record<string, JudgeDimensionComparison>;
  // 跨引擎结论差异：只列事实，不自动挑选「最佳 Judge」。
  disagreements: Array<{ kind: string; by_engine: Record<string, string | number> }>;
  deterministic_findings: Record<
    string,
    { count: number; severity_counts: Array<{ severity: string; count: number }>; findings: JudgeFactFinding[] }
  >;
  judge_finding_counts: Record<string, { total: number; by_severity: Record<string, number> }>;
  fact_inference_mismatch: Record<string, { count: number; notes: string[] }>;
}

export interface BatchRunItem {
  run_id: string;
  model: string;
  server_host?: string;
  source_base_url?: string;
  status: string;
  completion_score: number | null;
  is_success: boolean;
  tool_call_count: number;
  tool_error_count: number;
  tool_error_rate: number;
  failure_root_cause: string;
}

export interface RootCauseItem {
  label: string;
  count: number;
}

export interface BatchSummary {
  total_runs: number;
  successful_runs: number;
  failed_runs: number;
  success_rate: number;
  tool_error_rate: number;
  average_tool_calls: number;
  total_tool_calls: number;
  tool_error_count: number;
  root_causes: RootCauseItem[];
  runs: BatchRunItem[];
}

export interface ImportSessionPayload {
  base_url: string;
  user_id: string;
  session_id: string;
  token?: string;
  timeout?: number;
}

export interface ImportSessionResult {
  run: RunItem;
  batch_summary: BatchSummary;
}

export interface TrajectoryToolCall {
  id?: string;
  call_id?: string;
  response_item_id?: string;
  type?: string;
  function?: { name?: string; arguments?: string };
  name?: string;
  arguments?: string;
}

export interface TrajectoryMessage {
  index: number;
  message_id?: string;
  message_uuid?: string;
  session_id?: string;
  role: string;
  timestamp?: string | number;
  content?: string;
  kind?: string;
  tool_call_id?: string;
  tool_name?: string;
  tool_calls?: TrajectoryToolCall[];
  finish_reason?: string;
  reasoning?: string;
  reasoning_content?: string;
}

export interface TrajectoryPayload {
  run_id: string;
  messages: TrajectoryMessage[];
}

export interface RuntimeGraphNode {
  id: string;
  type: string;
  label: string;
  status: string;
  detail?: string;
}

export interface RuntimeGraphEdge {
  from: string;
  to: string;
}

export interface RuntimeGraph {
  nodes: RuntimeGraphNode[];
  edges: RuntimeGraphEdge[];
  current_node?: string | null;
}

export type RuntimeSpanType = "Agent" | "LLM" | "Tool" | "Skill" | "Retriever" | "Memory" | "Reflection";
export type RuntimeSpanStatus = "pending" | "running" | "success" | "failed";

export interface RuntimeTokenUsage {
  input: number;
  output: number;
  total: number;
}

export interface RuntimeSpan {
  spanId: string;
  parentSpanId?: string | null;
  name: string;
  type: RuntimeSpanType | string;
  startTime: string;
  endTime: string;
  latency: number;
  input?: unknown;
  output?: unknown;
  status: RuntimeSpanStatus | string;
  tokenUsage?: RuntimeTokenUsage;
  error?: { type?: string; message?: string } | null;
  attributes?: Record<string, unknown>;
}

export interface RuntimeTrace {
  traceId: string;
  sessionId: string;
  agentName: string;
  agentVersion: string;
  startTime: string;
  endTime: string;
  status: RuntimeSpanStatus | string;
  totalLatency: number;
  totalTokens: number;
  cost: number;
  metrics?: {
    steps?: number;
    llmCalls?: number;
    toolCalls?: number;
    skillCalls?: number;
  };
  spans: RuntimeSpan[];
}

export type RuntimeHealthStatus = "success" | "failed" | "running" | string;

export interface RuntimeLifecycleStage {
  name: string;
  startTime: string;
  endTime: string;
  duration: number;
  status: RuntimeHealthStatus;
}

export interface RuntimeMetrics {
  traceId: string;
  status: RuntimeHealthStatus;
  startTime: string;
  endTime: string;
  duration: number;
  lifecycleStages: RuntimeLifecycleStage[];
  steps: number;
  executionMetrics: {
    total_duration: number;
    total_steps: number;
    completed_steps: number;
    failed_steps: number;
    retry_count: number;
    timeout_count: number;
  };
  llmMetrics: {
    calls: number;
    inputTokens: number;
    outputTokens: number;
    totalTokens: number;
    averageLatency: number;
    maxLatency: number;
  };
  llmDashboard: LlmDashboard;
  toolMetrics: {
    calls: number;
    success: number;
    failed: number;
    averageLatency: number;
    timeout: number;
  };
  errorMetrics: {
    llmError: number;
    toolError: number;
    timeout: number;
    invalidOutput: number;
    validationFailed: number;
  };
  costMetrics: {
    tokenCost: number;
    totalCost: number;
  };
  healthScore: {
    total: number;
    taskSuccess: number;
    toolStability: number;
    performance: number;
    costEfficiency: number;
    errorRate: number;
  };
  performanceBreakdown: Array<{ label: string; value: number; percent: number }>;
  errors: Array<{ type: string; count: number; traceNodeId?: string; message?: string }>;
}

export interface LlmCallDetail {
  callId: string;
  traceId: string;
  name: string;
  model: string;
  provider: string;
  temperature: number | null;
  maxTokens: number | null;
  startTime: string;
  endTime: string;
  latency: {
    queueTime: number;
    generationTime: number;
    total: number;
  };
  tokenUsage: {
    inputTokens: number;
    outputTokens: number;
    totalTokens: number;
  };
  prompt: {
    system: string;
    user: string;
    context: string;
    version: string;
    systemPromptHash: string;
    template: string;
  };
  response: string;
  status: string;
  error: { type: string; message: string } | null;
  cost: number;
  quality: {
    responseLength: number;
    formatCompliance: number | null;
    hallucinationRisk: number | null;
    instructionFollowing: number | null;
    selfEvaluationScore: number | null;
    qualityScore: number;
  };
}

export interface LlmDashboard {
  summary: {
    traceId: string;
    calls: number;
    models: string[];
    inputTokens: number;
    outputTokens: number;
    totalTokens: number;
    averageLatency: number;
    maxLatency: number;
    estimatedCost: number;
    errorRate: number;
  };
  timeline: Array<{
    index: number;
    callId: string;
    name: string;
    model: string;
    provider: string;
    latency: number;
    tokens: number;
    status: string;
    startTime: string;
    endTime: string;
    error: { type: string; message: string } | null;
  }>;
  calls: LlmCallDetail[];
  tokenAnalytics: {
    inputTrend: Array<{ callId: string; value: number }>;
    outputTrend: Array<{ callId: string; value: number }>;
    totalTrend: Array<{ callId: string; value: number }>;
    averageInputTokens: number;
    averageOutputTokens: number;
    maxContext: number;
  };
  modelPerformance: Array<{
    model: string;
    provider: string;
    calls: number;
    success: number;
    successRate: number;
    averageLatency: number;
    tokens: number;
    cost: number;
  }>;
  promptVersions: Array<{
    version: string;
    systemPromptHash: string;
    template: string;
    calls: number;
    successRate: number;
    averageTokens: number;
    cost: number;
  }>;
  qualityMetrics: {
    qualityScore: number;
    responseLength: number;
    formatCompliance: number;
    hallucinationRisk: number;
    instructionFollowing: number;
    selfEvaluationScore: number;
  };
  errorAnalysis: {
    timeout: number;
    rateLimit: number;
    contextLengthExceeded: number;
    invalidResponse: number;
    jsonParseError: number;
    contentFilter: number;
    other: number;
  };
  filters: string[];
}

export interface RuntimeTrends {
  sampleSize: number;
  successRate: number;
  averageDuration: number;
  averageTokens: number;
  averageCost: number;
}

export interface LiveEvaluatePayload {
  base_url: string;
  user_id: string;
  message: string;
  acceptance_criteria: string;
  token?: string;
  model?: string;
  title?: string;
  skill?: string;
  files?: Array<{ filename: string; content_base64: string }>;
  max_turns?: number;
  timeout?: number;
  auto_continue?: boolean;
  auto_continue_limit?: number;
}

export interface LiveEvent {
  event: string;
  payload: any;
}

// 运行时提供方：远程 Agent HTTP 服务，或本机 DSH Headless 运行时。
export type RuntimeProviderId = "remote" | "dsh";

export interface DshProviderInfo {
  id: string;
  enabled: boolean;
  disabled_reason?: string | null;
}

// 裁判引擎：DeepAgent 本地裁判，或使用专用 profile 的 DSH 裁判。
export type JudgeEngineId = "deepagent" | "dsh";

export interface JudgeEngineInfo {
  id: string;
  label: string;
  available: boolean;
  disabled_reason?: string | null;
}

export interface JudgeEngineOptions {
  default_engine: string;
  engines: JudgeEngineInfo[];
  selected: string;
}

// GET /api/dsh/options 返回的后端授权清单：只有这里列出的 profile / workspace
// 才会被后端接受，前端不得自行拼出其它取值。
export interface DshOptions {
  providers: DshProviderInfo[];
  dsh: {
    enabled: boolean;
    disabled_reason?: string | null;
    profiles: string[];
    workspace_roots: string[];
    workspaces: string[];
    default_timeout_seconds: number;
    max_timeout_seconds: number;
    max_concurrency: number;
  };
  judge: JudgeEngineOptions;
}

export interface DshChatPayload {
  message: string;
  workspace: string;
  profile: string;
  session_id?: string | null;
  acceptance_criteria?: string;
  timeout?: number;
}

export type DshRuntimeEventType =
  | "session"
  | "status"
  | "thinking"
  | "text"
  | "tool_call"
  | "tool_result"
  | "final"
  | "error"
  | "unknown";

// 与 auditrail.dsh_runtime.DshRuntimeEvent.to_dict() 一致。
export interface DshRuntimeEvent {
  type: DshRuntimeEventType | string;
  data: Record<string, any>;
  phase?: string;
  source_type?: string;
  truncated?: boolean;
}

export interface DshAgentEventPayload {
  operation_id: string;
  session_id?: string | null;
  event: DshRuntimeEvent;
}

export interface DshChatErrorPayload {
  code: string;
  detail: string;
  stage: string;
  operation_id?: string;
  session_id?: string | null;
  cancelled?: boolean;
  timed_out?: boolean;
}

export interface DshCancelResult {
  ok: boolean;
  operation_id: string;
  session_id?: string | null;
  state: string;
}

export function fetchDshOptions(): Promise<DshOptions> {
  return apiGet<DshOptions>("/api/dsh/options");
}

export function fetchJudgeEngines(): Promise<JudgeEngineOptions> {
  return apiGet<JudgeEngineOptions>("/api/judge/engines");
}

// 一个 run 的多 Judge 报告清单与对比（不自动选择「最佳 Judge」）。
export function fetchJudgeReports(runId: string): Promise<JudgeReportList> {
  return apiGet<JudgeReportList>(`/api/runs/${encodeURIComponent(runId)}/judges`);
}

export function fetchJudgeReport(runId: string, engine: string): Promise<JudgeReport> {
  return apiGet<JudgeReport>(`/api/runs/${encodeURIComponent(runId)}/judges/${encodeURIComponent(engine)}`);
}

export function fetchJudgeComparison(runId: string): Promise<JudgeComparisonReport> {
  return apiGet<JudgeComparisonReport>(`/api/runs/${encodeURIComponent(runId)}/judges/compare`);
}

export function activateJudgeReport(runId: string, engine: string): Promise<JudgeReportList> {
  return apiPost<JudgeReportList>(`/api/runs/${encodeURIComponent(runId)}/judges/${encodeURIComponent(engine)}/active`);
}

export function streamDshChat(payload: DshChatPayload, onEvent: (event: LiveEvent) => void): Promise<void> {
  return streamPostJson("/api/dsh/chat", payload, onEvent);
}

export function cancelDshChat(operationId: string): Promise<DshCancelResult> {
  return apiPost<DshCancelResult>(`/api/dsh/chat/${encodeURIComponent(operationId)}/cancel`);
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

// 本地一键启动能力仅在 dev server 同源模式下可用：
// 直连远程后端（设置了 VITE_API_BASE_URL）时，/api 不走本机 Vite 代理，
// 页面上启动本地服务也无法让远程后端恢复，因此隐藏启动入口。
export const canLaunchLocally = API_BASE_URL === "";

export interface LauncherResult {
  ok: boolean;
  output?: string;
  error?: string;
}

export async function checkBackendHealth(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE_URL}/api/health`);
    return response.ok;
  } catch {
    return false;
  }
}

export async function startLocalServices(): Promise<LauncherResult> {
  const response = await fetch("/__launcher/start", { method: "POST" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<LauncherResult>;
}

export async function apiDownload(path: string): Promise<Blob> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.blob();
}

export async function apiPost<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { method: "POST" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

export async function apiPostJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

export async function apiDelete<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, { method: "DELETE" });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

export async function streamPostJson(path: string, body: unknown, onEvent: (event: LiveEvent) => void): Promise<void> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  });
  if (!response.ok) {
    throw new Error(await response.text());
  }
  if (!response.body) {
    throw new Error("Streaming response body is not available.");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split("\n\n");
    buffer = parts.pop() || "";
    for (const part of parts) {
      const parsed = parseSseBlock(part);
      if (parsed) onEvent(parsed);
    }
  }

  buffer += decoder.decode();
  const parsed = parseSseBlock(buffer);
  if (parsed) onEvent(parsed);
}

function parseSseBlock(block: string): LiveEvent | null {
  const lines = block.split(/\r?\n/);
  let event = "message";
  const data: string[] = [];
  for (const line of lines) {
    if (line.startsWith("event:")) {
      event = line.slice(6).trim() || "message";
    } else if (line.startsWith("data:")) {
      data.push(line.slice(5).trimStart());
    }
  }
  if (!data.length) return null;
  const raw = data.join("\n");
  try {
    return { event, payload: JSON.parse(raw) };
  } catch {
    return { event, payload: raw };
  }
}
