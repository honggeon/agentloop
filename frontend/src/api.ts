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
  // Run Contract v1 生命周期状态；旧 run 由后端按报告文件推断为 completed。
  run_status?: string | null;
  status_reason?: string | null;
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
  // Phase 0 起允许 null：无证据维度不伪造 0 分，界面显示「未评估」。
  score: number | null;
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

// 运行时提供方：远程 Agent HTTP 服务、本机 DSH Headless 运行时，或本机 DSH ACP
// 交互运行时（长驻进程 + 会话 + 一次性权限应答）。
export type RuntimeProviderId = "remote" | "dsh" | "acp";

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
    acp: DshAcpManifest;
  };
  judge: JudgeEngineOptions;
}

// ACP 只在 /api/dsh/acp/options 报告每次连接实际协商到的能力；这里只保留
// 静态开关，真实能力必须查 capabilities_route，不得在前端写死。
export interface DshAcpManifest {
  enabled: boolean;
  profile: string;
  permission_options: string[];
  allow_always: boolean;
  capabilities_route: string;
}

export interface DshAcpCapabilities {
  protocol_version: number;
  running: boolean;
  supports_images: boolean;
  supports_session_list: boolean;
  supports_session_resume: boolean;
  supports_session_close: boolean;
  permission_options: string[];
}

// 只允许 allow-once / reject-once；allow-always 永远不出现在这里。
export interface DshAcpPermissionPolicy {
  allow: string[];
  reject: string[];
  allow_always: boolean;
}

export interface DshAcpOptions {
  enabled: boolean;
  disabled_reason?: string | null;
  profile?: string;
  capabilities: Partial<DshAcpCapabilities>;
  permission_policy?: DshAcpPermissionPolicy;
}

export interface DshAcpSession {
  session_id: string;
  workspace: string;
  state: string;
  active_operation?: string | null;
  last_error?: string | null;
}

export interface DshAcpOperationStatus {
  operation_id: string;
  session_id: string;
  state: string;
  done: boolean;
  error: DshChatErrorPayload | null;
  run_id: string | null;
}

export interface DshAcpSessionResult {
  session_id: string;
  workspace: string;
  state: string;
  capabilities?: DshAcpCapabilities;
}

export interface DshAcpPermissionOption {
  optionId: string;
  name?: string;
  kind?: string;
}

// SSE permission_requested：请求在 session/prompt 仍阻塞时到达，必须由用户
// 明确回答 allow-once / reject-once。
export interface DshAcpPermissionRequest {
  request_id: string | number;
  session_id: string;
  tool_call_id: string;
  options: DshAcpPermissionOption[];
  operation_id: string;
}

// SSE completed 只表示 ACP 回合已结束，不表示任务成功；只有 run_imported
// 才表示标准 Auditrail run 已发布。dropped_events > 0 表示消费者读得太慢，
// 高流量进度事件已被丢弃（终态事件永不丢弃）。
export interface DshAcpCompletedPayload {
  session_id: string;
  stop_reason: string;
  text: string;
  cancelled: boolean;
  dropped_events?: number;
}

export interface DshAcpPromptPayload {
  session_id: string;
  message: string;
  acceptance_criteria?: string;
}

export interface DshAcpSessionPayload {
  workspace: string;
  session_id?: string;
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

// --- 本机 DSH ACP 交互运行时 ------------------------------------------------

export function fetchDshAcpOptions(): Promise<DshAcpOptions> {
  return apiGet<DshAcpOptions>("/api/dsh/acp/options");
}

export function fetchDshAcpSessions(): Promise<{ sessions: DshAcpSession[] }> {
  return apiGet<{ sessions: DshAcpSession[] }>("/api/dsh/acp/sessions");
}

export function createDshAcpSession(payload: DshAcpSessionPayload): Promise<DshAcpSessionResult> {
  return apiPostJson<DshAcpSessionResult>("/api/dsh/acp/session/new", payload);
}

export function resumeDshAcpSession(payload: DshAcpSessionPayload): Promise<DshAcpSessionResult> {
  return apiPostJson<DshAcpSessionResult>("/api/dsh/acp/session/resume", payload);
}

export function closeDshAcpSession(sessionId: string): Promise<{ ok: boolean; session_id: string }> {
  return apiDelete<{ ok: boolean; session_id: string }>(
    `/api/dsh/acp/session/${encodeURIComponent(sessionId)}`
  );
}

// 一个 session 同时只允许一个 active prompt；事件顺序为 started、
// runtime_event / permission_requested、completed，最后是 run_imported 或
// error{stage:"import"}。
export function streamDshAcpPrompt(
  payload: DshAcpPromptPayload,
  onEvent: (event: LiveEvent) => void
): Promise<void> {
  return streamPostJson("/api/dsh/acp/prompt", payload, onEvent);
}

export function cancelDshAcpOperation(operationId: string): Promise<DshCancelResult> {
  return apiPostJson<DshCancelResult>("/api/dsh/acp/cancel", { operation_id: operationId });
}

export function respondDshAcpPermission(
  operationId: string,
  requestId: string | number,
  optionId: string
): Promise<{ ok: boolean; request_id: string | number; option_id: string }> {
  return apiPostJson<{ ok: boolean; request_id: string | number; option_id: string }>(
    "/api/dsh/acp/permission/respond",
    { operation_id: operationId, request_id: requestId, option_id: optionId }
  );
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
}

// --- Phase 0 平台切片：Agent / TestCase / 单次异步 Run --------------------------

// Agent 配置只保存 endpoint 与能力声明；凭据永远通过环境变量传递，不入库。
export interface AgentConfig {
  agent_id: string;
  name: string;
  version: string;
  adapter: string;
  endpoint: string;
  capabilities: string[];
  metadata: Record<string, unknown>;
  config_hash?: string;
  // Phase 2 正式元数据：environment 可以为 null（不会默认成 production）；
  // created_at 只在创建时写入，旧配置为 null；source_host 仅用于来源展示。
  description?: string;
  environment?: string | null;
  tags?: string[];
  enabled?: boolean;
  source_host?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface TestCaseExpectation {
  id: string;
  type: string;
  required?: boolean;
  path?: string;
  [key: string]: unknown;
}

// 统一 TestCase：adapter=http_session 由平台执行；adapter=command 是
// auditrail.cases 旧格式的兼容视图（legacy=true），只能在 CLI 执行。
export interface TestCase {
  case_id: string;
  version: number;
  title: string;
  adapter: string;
  input: Record<string, unknown>;
  fixtures: Array<{ path: string; content?: unknown }>;
  expectations: TestCaseExpectation[];
  timeout_seconds: number;
  adapter_config?: Record<string, unknown>;
  legacy?: boolean;
  xfail?: boolean;
  defect?: string;
}

// Run 生命周期枚举与 Judge overall（success/partial/failed/unknown）严格不同。
export type PlatformRunStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "timed_out"
  | "cancelled"
  | "skipped";

export interface RunErrorInfo {
  phase: string;
  error_type: string;
  retryable: boolean;
  safe_message: string;
  external_status?: number | null;
  timestamp?: string;
}

export interface RunStatusPayload {
  run_id: string;
  status: PlatformRunStatus | string;
  status_reason?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  agent_ref?: { id?: string; version?: string; config_hash?: string } | null;
  test_case_ref?: { id?: string; version?: number } | null;
  artifact_manifest: string[];
  legacy_inferred: boolean;
  stale: boolean;
  error?: RunErrorInfo;
  evaluation_status?: string;
  evaluation_error?: string;
}

export interface CreateRunResult {
  run_id: string;
  status: PlatformRunStatus | string;
  status_url: string;
}

export function fetchAgents(): Promise<{ agents: AgentConfig[] }> {
  return apiGet<{ agents: AgentConfig[] }>("/api/agents");
}

export function createAgent(payload: Partial<AgentConfig>): Promise<{ agent: AgentConfig }> {
  return apiPostJson<{ agent: AgentConfig }>("/api/agents", payload);
}

// Phase 2：更新 Agent 元数据（agent_id/created_at 不可变，历史 Run 不受影响）。
export function updateAgent(agentId: string, payload: Partial<AgentConfig>): Promise<{ agent: AgentConfig }> {
  return apiPutJson<{ agent: AgentConfig }>(`/api/agents/${encodeURIComponent(agentId)}`, payload);
}

// 删除 Agent 配置；历史 Run 保留并显示为 missing_configuration/derived。
export function deleteAgent(agentId: string): Promise<{ deleted: string }> {
  return apiDelete<{ deleted: string }>(`/api/agents/${encodeURIComponent(agentId)}`);
}

export function fetchTestCases(): Promise<{ test_cases: TestCase[]; errors: Array<{ case_id: string; error: string }> }> {
  return apiGet<{ test_cases: TestCase[]; errors: Array<{ case_id: string; error: string }> }>("/api/test-cases");
}

export function createTestCase(payload: Partial<TestCase>): Promise<{ test_case: TestCase }> {
  return apiPostJson<{ test_case: TestCase }>("/api/test-cases", payload);
}

export function fetchTestCase(caseId: string): Promise<{ test_case: TestCase }> {
  return apiGet<{ test_case: TestCase }>(`/api/test-cases/${encodeURIComponent(caseId)}`);
}

export function createPlatformRun(
  payload: { agent_id: string; test_case_id: string; judge?: boolean },
  idempotencyKey?: string
): Promise<CreateRunResult> {
  return apiPostJsonWithHeaders<CreateRunResult>(
    "/api/runs",
    payload,
    idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}
  );
}

export function fetchRunStatus(runId: string): Promise<RunStatusPayload> {
  return apiGet<RunStatusPayload>(`/api/runs/${encodeURIComponent(runId)}/status`);
}

// --- Phase 1 平台切片：TestSuite CRUD 与 SuiteJob 顺序批量运行 --------------------

// 统一 TestSuite：case_ids 的顺序即批量执行顺序；enabled=false 的测试集不能发起运行。
export interface TestSuite {
  suite_id: string;
  version: number;
  title: string;
  description: string;
  case_ids: string[];
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

// SuiteJob 生命周期与 Run 生命周期严格不同：没有 timed_out / skipped。
export type SuiteJobStatus = "queued" | "running" | "completed" | "failed" | "cancelled";

// CaseAttempt 复用 Run 生命周期状态；skipped 表示因取消/超时/预算耗尽而未开始。
// Phase 1.1：同一 case 的多次重试是多个 attempt（retry_index 递增），
// 每次重试绑定一个全新的 Run，不覆盖旧证据。
export interface SuiteJobAttempt {
  attempt_id: string;
  order: number;
  case_id: string;
  case_title: string;
  case_version?: number | null;
  retry_index?: number;
  status: PlatformRunStatus | string;
  run_id?: string | null;
  status_reason?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  // Phase 2：同一 CaseExecution 的重试链关联；旧 job 缺失时由后端派生。
  case_execution_id?: string;
  attempt_count?: number;
  is_final_attempt?: boolean;
  attempt_history_url?: string;
  run_status_url?: string;
  run_context_url?: string;
  evaluation_status?: string;
  final_quality_status?: string;
}

// 按 case 最终 attempt 状态计数；skipped/queued 永远不算 pass，通过率分母只含 completed。
// runs 统计产生了 Run 的 attempt 总数，retries 统计 retry_index>0 的重试次数。
export interface SuiteJobStats {
  total: number;
  queued: number;
  running: number;
  completed: number;
  failed: number;
  timed_out: number;
  cancelled: number;
  skipped: number;
  runs: number;
  retries: number;
}

// Phase 1.1 执行预算：max_attempts 为执行预算（含重试），max_total_tokens 为
// 成本预算（按运行产物的观测 token 计）。null 表示不限制。
export interface SuiteJobBudget {
  max_attempts?: number | null;
  max_total_tokens?: number | null;
}

export interface SuiteJobBudgetUsed {
  attempts: number;
  total_tokens: number;
}

export interface SuiteJobLimits {
  global: number;
  agent: number;
}

export interface SuiteJob {
  job_id: string;
  status: SuiteJobStatus | string;
  status_reason?: string | null;
  cancel_requested?: boolean;
  judge: boolean;
  max_retries?: number;
  timeout_seconds?: number | null;
  budget?: SuiteJobBudget;
  budget_used?: SuiteJobBudgetUsed;
  limits?: SuiteJobLimits | null;
  // 进程重启后被恢复续跑的 job 标记 recovered=true，与正常执行区分展示。
  recovered?: boolean;
  suite_ref?: { id?: string; version?: number; title?: string } | null;
  agent_ref?: { id?: string; name?: string; environment?: string | null; version?: string; config_hash?: string } | null;
  // Phase 2 展示便利字段；旧 job 没有 agent_ref 时为 null（显示 unknown/derived）。
  agent_id?: string | null;
  agent_name?: string | null;
  agent_environment?: string | null;
  attempts?: SuiteJobAttempt[];
  created_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
  stats: SuiteJobStats;
  stale: boolean;
}

export interface CreateSuiteJobResult {
  job_id: string;
  status: SuiteJobStatus | string;
  status_url: string;
  // 相同 Idempotency-Key + 相同请求的重复提交返回 true，不创建第二个 job。
  idempotent_replay?: boolean;
}

export function fetchTestSuites(): Promise<{ test_suites: TestSuite[] }> {
  return apiGet<{ test_suites: TestSuite[] }>("/api/test-suites");
}

export function createTestSuite(payload: Partial<TestSuite>): Promise<{ test_suite: TestSuite }> {
  return apiPostJson<{ test_suite: TestSuite }>("/api/test-suites", payload);
}

export function fetchTestSuite(
  suiteId: string
): Promise<{ test_suite: TestSuite; test_cases: TestCase[]; missing_case_ids: string[] }> {
  return apiGet<{ test_suite: TestSuite; test_cases: TestCase[]; missing_case_ids: string[] }>(
    `/api/test-suites/${encodeURIComponent(suiteId)}`
  );
}

export function updateTestSuite(suiteId: string, payload: Partial<TestSuite>): Promise<{ test_suite: TestSuite }> {
  return apiPutJson<{ test_suite: TestSuite }>(`/api/test-suites/${encodeURIComponent(suiteId)}`, payload);
}

export function deleteTestSuite(suiteId: string): Promise<{ deleted: string }> {
  return apiDelete<{ deleted: string }>(`/api/test-suites/${encodeURIComponent(suiteId)}`);
}

// Phase 1.1 批量运行启动选项：重试、套件总超时、执行/成本预算。
export interface LaunchSuiteJobPayload {
  agent_id: string;
  judge?: boolean;
  max_retries?: number;
  timeout_seconds?: number | null;
  budget?: SuiteJobBudget;
}

// 发起一个 SuiteJob：后端按 case_ids 顺序逐个执行，单 case 失败不阻断后续 case；
// 失败且可重试的 case 按 max_retries 自动重试（每次重试创建新 Run）。
// 传入 Idempotency-Key 时，相同 key + 相同请求的重复提交返回原 job（不重复执行）。
export function launchSuiteJob(
  suiteId: string,
  payload: LaunchSuiteJobPayload,
  idempotencyKey?: string
): Promise<CreateSuiteJobResult> {
  return apiPostJsonWithHeaders<CreateSuiteJobResult>(
    `/api/test-suites/${encodeURIComponent(suiteId)}/runs`,
    payload,
    idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}
  );
}

export function fetchSuiteJobs(): Promise<{ suite_jobs: SuiteJob[] }> {
  return apiGet<{ suite_jobs: SuiteJob[] }>("/api/suite-jobs");
}

export function fetchSuiteJob(jobId: string): Promise<{ suite_job: SuiteJob }> {
  return apiGet<{ suite_job: SuiteJob }>(`/api/suite-jobs/${encodeURIComponent(jobId)}`);
}

// 取消是协作式的：进行中的 case 收为 cancelled，未开始的 case 标记 skipped，
// 已完成的 Run 与全部证据保留。
export function cancelSuiteJob(jobId: string): Promise<{ suite_job: SuiteJob }> {
  return apiPost<{ suite_job: SuiteJob }>(`/api/suite-jobs/${encodeURIComponent(jobId)}/cancel`);
}

// 恢复 stale 的 SuiteJob（进程重启后遗留的 queued/running 任务）：孤儿 attempt
// 标记为 failed/executor_interrupted（可重试），未完成的 case 从新 Run 继续执行。
export function recoverStaleSuiteJobs(): Promise<{ resumed: string[] }> {
  return apiPost<{ resumed: string[] }>("/api/suite-jobs/recover");
}

// --- LangSmith 风格 Agent 详情工作区：只读聚合视图 ------------------------------
//
// 后端 GET /api/agents* 复用现有 run 产物聚合出 Agent 维度视图；前端只消费，
// 不在组件内聚合 JSON。null 表示数据不存在或无法计算，[] 表示明确的空结果，
// unknown 表示「未评估」，与失败严格区分。

// 时间范围：7 天 / 30 天 / 全部。
export type AgentOverviewRange = "7d" | "30d" | "all";

// 运行状态筛选；stale 单独筛出执行器中断的运行。
export type AgentRunStatusFilter = "all" | "completed" | "failed" | "timed_out" | "cancelled" | "stale";

// 聚合阶段的局部错误：单条 run 扫描失败不应拖垮整个页面。
export interface AgentScanError {
  run_id?: string | null;
  stage: string;
  message: string;
}

// Phase 2 运行索引状态：used=false 时页面提示「索引不可用，当前使用扫描结果」。
export interface RunIndexBlock {
  used: boolean;
  status: "ready" | "rebuilding" | "unavailable" | "fallback" | string;
  last_scan_at?: string | null;
  indexed_count?: number;
  error_count?: number;
}

// 成本来源：observed 观测 / imported 导入 / estimated 估算 / missing 无数据 /
// mixed 多来源。估算值必须标注「估算」，无数据显示「—」。
export type CostSource = "observed" | "imported" | "estimated" | "missing" | "mixed" | string;

export interface CostBlock {
  total: number | null;
  currency: string | null;
  source: CostSource;
}

export interface AgentRunStats {
  run_count: number;
  last_run_at: string | null;
  last_run_status: string | null;
}

// /api/agents 中的注册 Agent：在 AgentConfig 基础上附加运行统计。
export interface AgentListItem {
  agent_id: string;
  name: string;
  version?: string;
  adapter?: string;
  endpoint?: string;
  capabilities?: string[];
  metadata?: Record<string, unknown>;
  max_concurrency?: number | null;
  config_hash?: string;
  // Phase 2 元数据（旧配置缺省时为 null/空）。
  description?: string;
  environment?: string | null;
  tags?: string[];
  enabled?: boolean;
  source_host?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  derived: boolean;
  stats: AgentRunStats;
}

// 仅从运行数据派生出的 Agent（没有独立注册配置），界面必须标注来源。
export interface DerivedAgentSummary {
  agent_id: string;
  name: string;
  derived: true;
  source_host: string | null;
  stats: AgentRunStats;
}

export interface AgentDirectoryPayload {
  agents: AgentListItem[];
  derived_agents: DerivedAgentSummary[];
  index?: RunIndexBlock;
  partial: boolean;
  errors: AgentScanError[];
}

// Agent 详情页 Header 契约。
export interface AgentHeader {
  agent_id: string;
  name: string;
  type: string | null;
  environment: string | null;
  source_host: string | null;
  derived: boolean;
  // registered = 有注册配置；missing_configuration = 仅从运行数据可知。
  config_status?: "registered" | "missing_configuration" | string;
  description?: string | null;
  tags?: string[];
  enabled?: boolean | null;
  created_at: string | null;
  updated_at?: string | null;
  last_run_at: string | null;
  last_run_status: string | null;
}

export interface AgentDetailPayload {
  agent: AgentHeader;
  capabilities: string[];
  config: Record<string, unknown> | null;
  run_count: number;
  index?: RunIndexBlock;
  partial: boolean;
  errors: AgentScanError[];
}

export interface AgentModelMetric {
  model: string;
  provider: string | null;
  llm_call_count: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  average_latency_ms: number | null;
  error_count: number;
  // 成本来源：observed 观测 / imported 导入 / estimated 估算（须标注）/
  // mixed 多来源 / missing|null 显示「—」。
  cost: number | null;
  cost_source: CostSource | null;
}

export interface AgentToolMetric {
  tool_name: string;
  call_count: number;
  success_count: number;
  error_count: number;
  // 工具调用错误率（分母是工具调用次数），不要误称为 run 失败率。
  error_rate: number | null;
  dangerous_count: number;
  audit_source: string;
}

export interface AgentTraceDay {
  date: string;
  run_count: number;
  completed_count: number;
  failed_count: number;
  timed_out_count: number;
  cancelled_count: number;
  error_count: number;
}

export interface AgentKpis {
  trace_count: number;
  run_count: number;
  error_rate: number | null;
  completion_score: number | null;
  token_count: number | null;
  total_cost: number | null;
  total_cost_source: CostSource | null;
  judge_evidence_coverage: number | null;
}

// 单个健康指标：status 只有 pass/partial/fail/unknown；unknown 显示「未评估」，
// 与 fail 区分；label/detail 由后端给出中文文案，界面不得只靠颜色传达状态。
export interface AgentHealthMetric {
  value: number | null;
  status: "pass" | "partial" | "fail" | "unknown" | string;
  source: "observed" | "imported" | "estimated" | "missing" | string;
  label: string;
  detail: string;
}

export interface AgentHealth {
  task_completion: AgentHealthMetric;
  tool_stability: AgentHealthMetric;
  performance: AgentHealthMetric;
  cost_efficiency: AgentHealthMetric;
  deterministic_finding_count: AgentHealthMetric;
  high_severity_finding_count: AgentHealthMetric;
}

// Agent 运行行：同时用于 overview 的 recent_runs 与 runs 分页列表。
export interface AgentRunRow {
  run_id: string;
  agent_id: string | null;
  case_id: string | null;
  // Phase 2 SuiteJob 关联：旧 Run 没有 suite_ref 时全为 null，不伪造关联。
  suite_id: string | null;
  job_id: string | null;
  case_execution_id: string | null;
  attempt_id: string | null;
  retry_index: number | null;
  attempt_count: number | null;
  is_final_attempt: boolean | null;
  status: string;
  stale: boolean;
  judge_overall_status: string | null;
  completion_score: number | null;
  started_at: string | null;
  finished_at: string | null;
  duration_ms: number | null;
  tool_error_count: number;
  deterministic_finding_count: number;
  high_severity_finding_count: number;
  has_evidence: boolean;
  legacy_inferred: boolean;
  // 单次运行的成本汇总：null 表示无数据（missing），不用 0 冒充。
  cost: number | null;
  cost_source: CostSource;
}

export interface AgentOverview {
  agent: AgentHeader;
  models: AgentModelMetric[];
  tools: AgentToolMetric[];
  traces: AgentTraceDay[];
  kpis: AgentKpis;
  health: AgentHealth;
  recent_runs: AgentRunRow[];
  data_range: AgentOverviewRange | string;
  index?: RunIndexBlock;
  cost?: CostBlock;
  partial: boolean;
  errors: AgentScanError[];
}

export interface AgentRunsPage {
  items: AgentRunRow[];
  total: number;
  range: AgentOverviewRange | string;
  status: AgentRunStatusFilter | string;
  limit: number;
  offset: number;
  index?: RunIndexBlock;
  partial: boolean;
  errors: AgentScanError[];
}

// 与 fetchAgents 指向同一端点，但返回完整工作区载荷（含派生 Agent 与局部错误）。
export function fetchAgentDirectory(): Promise<AgentDirectoryPayload> {
  return apiGet<AgentDirectoryPayload>("/api/agents");
}

export function fetchAgentDetail(agentId: string): Promise<AgentDetailPayload> {
  return apiGet<AgentDetailPayload>(`/api/agents/${encodeURIComponent(agentId)}`);
}

export function fetchAgentOverview(agentId: string, range: AgentOverviewRange = "7d"): Promise<AgentOverview> {
  return apiGet<AgentOverview>(`/api/agents/${encodeURIComponent(agentId)}/overview?range=${encodeURIComponent(range)}`);
}

export function fetchAgentRuns(
  agentId: string,
  params: { range?: AgentOverviewRange; status?: AgentRunStatusFilter; limit?: number; offset?: number } = {}
): Promise<AgentRunsPage> {
  const search = new URLSearchParams();
  search.set("range", params.range || "7d");
  search.set("status", params.status || "all");
  search.set("limit", String(params.limit ?? 50));
  search.set("offset", String(params.offset ?? 0));
  return apiGet<AgentRunsPage>(`/api/agents/${encodeURIComponent(agentId)}/runs?${search.toString()}`);
}

// --- Phase 2：Run/SuiteJob 关联、成本数据与运行索引 ----------------------------------

// 成本来源的中文展示标签；missing/null 由视图统一显示「—」或「暂无数据」。
export const COST_SOURCE_LABELS: Record<string, string> = {
  observed: "观测",
  imported: "导入",
  estimated: "估算",
  missing: "无数据",
  mixed: "多来源"
};

export function costSourceLabel(source: string | null | undefined): string {
  if (!source || source === "missing") return "无数据";
  return COST_SOURCE_LABELS[source] || source;
}

// GET /api/runs/{run_id}/context：run.json 为关联权威来源，不一致以 warnings 返回。
export interface RunContextPayload {
  run: RunStatusPayload & { suite_ref?: Record<string, unknown> | null };
  agent: {
    id: string | null;
    name: string | null;
    environment: string | null;
    config_status: "registered" | "missing_configuration" | "missing" | "unknown" | string;
    config: Record<string, unknown> | null;
  };
  suite: {
    suite_id: string | null;
    suite_version?: number | null;
    suite_title?: string | null;
    job_id: string | null;
    job_status?: string | null;
    case_execution_id?: string | null;
    attempt_id?: string | null;
    retry_index?: number | null;
  };
  case_execution: {
    case_execution_id: string;
    case_id: string | null;
    case_title?: string | null;
    attempt_count: number;
    attempt_history: Array<{
      attempt_id: string;
      run_id: string | null;
      retry_index: number;
      status: string;
      is_final_attempt: boolean;
    }>;
  } | null;
  attempt: {
    attempt_id: string;
    retry_index: number;
    status: string;
    status_reason?: string | null;
    run_id: string | null;
    is_final_attempt: boolean;
    started_at?: string | null;
    finished_at?: string | null;
  } | null;
  related_runs: Array<{ run_id: string; status?: string | null; attempt_id?: string; retry_index?: number }>;
  warnings: Array<{ code: string; message: string }>;
}

export interface SuiteJobRunsPayload {
  job_id: string;
  status: string;
  suite_ref: Record<string, unknown> | null;
  agent: RunContextPayload["agent"];
  case_executions: Array<{
    case_execution_id: string;
    order: number;
    case_id: string | null;
    case_title?: string | null;
    attempt_count: number;
    attempts: string[];
    final_attempt_id: string;
    final_status: string | null;
  }>;
  runs: Array<{
    attempt_id: string;
    case_execution_id: string;
    case_id: string | null;
    retry_index: number;
    status: string;
    run_id: string | null;
    run: Record<string, unknown> | null;
  }>;
  warnings: Array<{ code: string; message: string }>;
}

export interface SuiteJobAttemptPayload {
  job_id: string;
  attempt: {
    attempt_id: string;
    case_execution_id: string;
    case_id: string | null;
    retry_index: number;
    status: string;
    status_reason?: string | null;
    run_id: string | null;
    is_final_attempt: boolean;
  };
  attempt_history: Array<{
    attempt_id: string;
    retry_index: number;
    status: string;
    run_id: string | null;
    is_final_attempt: boolean;
    run: Record<string, unknown> | null;
  }>;
  suite_ref: Record<string, unknown> | null;
  agent_ref: Record<string, unknown> | null;
}

export function fetchRunContext(runId: string): Promise<RunContextPayload> {
  return apiGet<RunContextPayload>(`/api/runs/${encodeURIComponent(runId)}/context`);
}

export function fetchSuiteJobRuns(jobId: string): Promise<SuiteJobRunsPayload> {
  return apiGet<SuiteJobRunsPayload>(`/api/suite-jobs/${encodeURIComponent(jobId)}/runs`);
}

export function fetchSuiteJobAttempt(jobId: string, attemptId: string): Promise<SuiteJobAttemptPayload> {
  return apiGet<SuiteJobAttemptPayload>(
    `/api/suite-jobs/${encodeURIComponent(jobId)}/attempts/${encodeURIComponent(attemptId)}`
  );
}

// 统一成本记录：total_cost 永远带 currency；没有数据时是 null（missing），不是 0。
export interface CostRecord {
  record_id: string;
  run_id: string | null;
  agent_id: string | null;
  provider: string | null;
  model: string | null;
  input_tokens: number | null;
  output_tokens: number | null;
  total_tokens: number | null;
  input_cost: number | null;
  output_cost: number | null;
  total_cost: number | null;
  currency: string | null;
  source: CostSource;
  source_system: string | null;
  source_id: string | null;
  observed_at: string | null;
  imported_at?: string | null;
  price_version?: string | null;
  metadata: Record<string, unknown>;
}

export interface CostSummary {
  total: number | null;
  currency: string | null;
  source: CostSource;
  sources?: string[];
  record_count: number;
  conflict_count?: number;
}

export interface RunCostsPayload {
  run_id: string;
  records: CostRecord[];
  summary: CostSummary;
}

export interface AgentCostsPayload {
  agent_id: string;
  records: CostRecord[];
  summary: CostSummary;
}

export interface CostImportReport {
  import_id: string;
  status: "completed" | "completed_with_errors" | "failed" | "dry_run" | string;
  source_system: string;
  source_file: string;
  dry_run: boolean;
  total_records: number;
  imported_records: number;
  duplicate_records: number;
  invalid_records: number;
  errors: Array<{ line: number; error: string }>;
  warnings: string[];
  imported_at: string;
}

export function fetchRunCosts(runId: string): Promise<RunCostsPayload> {
  return apiGet<RunCostsPayload>(`/api/runs/${encodeURIComponent(runId)}/costs`);
}

export function fetchAgentCosts(agentId: string): Promise<AgentCostsPayload> {
  return apiGet<AgentCostsPayload>(`/api/agents/${encodeURIComponent(agentId)}/costs`);
}

export function importCosts(payload: {
  source_system: string;
  source_file: string;
  dry_run?: boolean;
  run_id?: string;
  agent_id?: string;
}): Promise<CostImportReport> {
  return apiPostJson<CostImportReport>("/api/costs/import", payload);
}

export function fetchCostImport(importId: string): Promise<CostImportReport> {
  return apiGet<CostImportReport>(`/api/costs/imports/${encodeURIComponent(importId)}`);
}

// 本地运行索引管理（无生产级权限控制，后端响应含 authority: "local"）。
export interface RunIndexStatus {
  schema_version: number;
  status: "ready" | "missing" | "unavailable" | string;
  last_scan_at: string | null;
  indexed_count: number;
  error_count: number;
  errors: Array<{ run_id?: string; stage: string; message: string }>;
  authority?: string;
  note?: string;
}

export function fetchRunIndexStatus(): Promise<RunIndexStatus> {
  return apiGet<RunIndexStatus>("/api/admin/run-index/status");
}

export function rebuildRunIndex(): Promise<RunIndexStatus> {
  return apiPost<RunIndexStatus>("/api/admin/run-index/rebuild");
}

export async function apiPostJsonWithHeaders<T>(path: string, body: unknown, headers: Record<string, string>): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body)
  });
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
  return apiPostJsonWithHeaders<T>(path, body, {});
}

export async function apiPutJson<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    method: "PUT",
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
