const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || "http://localhost:18080").replace(/\/$/, "");

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

export interface JudgeReport {
  status?: string;
  overall_status?: string;
  completion_score?: number;
  confidence?: string;
  summary?: string;
  findings?: Finding[];
  dimensions?: Record<string, JudgeDimension>;
  judge_reliability?: JudgeReliability;
  metrics?: Record<string, unknown>;
  charts?: Record<string, unknown>;
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

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`);
  if (!response.ok) {
    throw new Error(await response.text());
  }
  return response.json() as Promise<T>;
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
