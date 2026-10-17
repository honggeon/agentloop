<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  costSourceLabel,
  EXTERNAL_CONFIDENCE_LABELS,
  EXTERNAL_LINK_METHOD_LABELS,
  fetchSuiteJobDetail,
  type AttemptDetail,
  type CaseExecutionDetail,
  type ExternalEvaluation,
  type ExternalTrace,
  type SuiteJobDetail
} from "../api";
import { formatPercent } from "../utils";

const route = useRoute();

const jobId = computed(() => String(route.params.jobId || ""));
const detail = ref<SuiteJobDetail | null>(null);
const loading = ref(false);
const error = ref("");
let pollTimer: ReturnType<typeof setInterval> | null = null;

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

// 外部评测行级状态是外部系统自己的结论，不映射到内部 judge 状态；
// unknown 显示「未评估」。
const EXTERNAL_EVAL_STATUS_LABELS: Record<string, string> = {
  pass: "通过",
  partial: "部分通过",
  fail: "未通过",
  unknown: "未评估"
};

const job = computed(() => detail.value?.suite_job || null);
const summary = computed(() => detail.value?.summary || null);
const budget = computed(() => detail.value?.budget || null);
const externalEvidence = computed(() => detail.value?.external_evidence || null);
const caseExecutions = computed(() => detail.value?.case_executions || []);
const externalTraces = computed(() => externalEvidence.value?.external_traces || []);
const externalEvaluations = computed(() => externalEvidence.value?.external_evaluations || []);

// Attempt Timeline 默认折叠历史 attempt；展开状态按 case_execution_id 记录。
const expandedExecutions = ref<Record<string, boolean>>({});

function attemptPool(): AttemptDetail[] {
  return (job.value?.attempts || []) as AttemptDetail[];
}

// 一个 CaseExecution 的全部 attempt（按 retry_index 排序，attempt number = retry_index + 1）。
function attemptsFor(execution: CaseExecutionDetail): AttemptDetail[] {
  const ids = new Set(execution.attempts);
  return attemptPool()
    .filter((attempt) => ids.has(attempt.attempt_id))
    .sort((a, b) => Number(a.retry_index || 0) - Number(b.retry_index || 0));
}

function finalAttemptFor(execution: CaseExecutionDetail): AttemptDetail | null {
  return attemptsFor(execution).find((attempt) => attempt.attempt_id === execution.final_attempt_id) || null;
}

function toggleExecution(execution: CaseExecutionDetail) {
  expandedExecutions.value[execution.case_execution_id] = !expandedExecutions.value[execution.case_execution_id];
}

function runStatusLabel(status: string | null | undefined): string {
  if (!status) return "—";
  return RUN_STATUS_LABELS[status] || status;
}

function jobStatusLabel(status: string | null | undefined): string {
  if (!status) return "未知";
  return SUITE_JOB_STATUS_LABELS[status] || status;
}

// unknown 显示「未评估」，与失败严格区分。
function finalQualityLabel(status: string | null | undefined): string {
  if (!status) return "未评估";
  return FINAL_QUALITY_LABELS[status] || "未评估";
}

function externalStatusLabel(status: string | null | undefined): string {
  if (!status) return "未评估";
  return EXTERNAL_EVAL_STATUS_LABELS[status] || "未评估";
}

function confidenceLabel(confidence: string | null | undefined): string {
  if (!confidence) return EXTERNAL_CONFIDENCE_LABELS.unlinked;
  return EXTERNAL_CONFIDENCE_LABELS[confidence] || confidence;
}

function linkMethodLabel(method: string | null | undefined): string {
  if (!method) return EXTERNAL_LINK_METHOD_LABELS.none;
  return EXTERNAL_LINK_METHOD_LABELS[method] || method;
}

function formatLatency(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value >= 1000 ? `${(value / 1000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function formatTokens(tokens: ExternalTrace["tokens"]): string {
  if (!tokens || tokens.total === null) return "—";
  return `${tokens.total}（入 ${tokens.input ?? "—"} / 出 ${tokens.output ?? "—"}）`;
}

// 外部成本来源用统一的 costSourceLabel；missing/null 显示「—」。
function formatExternalCost(trace: ExternalTrace): string {
  const cost = trace.cost;
  if (!cost || cost.value === null || !cost.source || cost.source === "missing") return "—";
  const amount = `${cost.currency || "USD"} ${cost.value.toFixed(4)}`;
  return cost.source === "observed" ? amount : `${costSourceLabel(cost.source)} ${amount}`;
}

// 外部 score 永远带 score_min~score_max 范围标注，不被解释成 0-100 completion_score。
function formatExternalScore(record: ExternalEvaluation): string {
  if (record.score === null) return "—";
  if (record.score_min !== null && record.score_max !== null) {
    return `${record.score}（范围 ${record.score_min} ~ ${record.score_max}）`;
  }
  return String(record.score);
}

function formatPassRate(value: number | null | undefined): string {
  if (value === null || value === undefined) return "未评估";
  return formatPercent(value);
}

function isActiveJob(status: string | null | undefined): boolean {
  return status === "queued" || status === "running";
}

async function loadDetail() {
  loading.value = true;
  error.value = "";
  try {
    detail.value = await fetchSuiteJobDetail(jobId.value);
    if (isActiveJob(String(detail.value.suite_job.status))) {
      startPolling();
    } else {
      stopPolling();
    }
  } catch (err) {
    detail.value = null;
    error.value = err instanceof Error ? err.message : String(err);
    stopPolling();
  } finally {
    loading.value = false;
  }
}

// active job（queued/running）1s 轮询，终态停止。
function startPolling() {
  stopPolling();
  pollTimer = setInterval(async () => {
    try {
      detail.value = await fetchSuiteJobDetail(jobId.value);
      if (!isActiveJob(String(detail.value.suite_job.status))) stopPolling();
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

watch(jobId, () => {
  stopPolling();
  void loadDetail();
});

onMounted(loadDetail);
onUnmounted(stopPolling);
</script>

<template>
  <div class="page suite-job-page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">SuiteJob 详情</p>
        <h2 class="run-title">
          <span class="mono">{{ jobId }}</span>
          <span v-if="job" class="chip" :data-status="job.status">{{ jobStatusLabel(String(job.status)) }}</span>
          <span v-if="job?.cancel_requested" class="chip quiet">已请求取消</span>
          <span v-if="job?.stale" class="chip quiet">执行器已中断（stale）</span>
          <span v-if="job?.recovered" class="chip quiet">已恢复（进程重启后续跑）</span>
        </h2>
        <p v-if="job" class="lede agent-header-meta">
          <span>
            测试集：
            <router-link :to="{ name: 'platform' }">{{ job.suite_ref?.title || job.suite_ref?.id || "—" }}</router-link>
            <template v-if="job.suite_ref?.id">（{{ job.suite_ref.id }}<template v-if="job.suite_ref.version"> v{{ job.suite_ref.version }}</template>）</template>
          </span>
          <span>
            Agent：
            <router-link v-if="job.agent_id" :to="{ name: 'agent-detail', params: { agentId: job.agent_id } }">
              {{ job.agent_name || job.agent_id }}
            </router-link>
            <template v-else>unknown</template>
            <template v-if="job.agent_environment">（环境：{{ job.agent_environment }}）</template>
          </span>
          <span>创建：{{ job.created_at || "—" }}</span>
          <span>开始：{{ job.started_at || "—" }}</span>
          <span>结束：{{ job.finished_at || "—" }}</span>
          <span v-if="budget?.deadline_at">截止时间：{{ budget.deadline_at }}</span>
          <span>单 case 重试上限：{{ budget?.max_retries ?? job.max_retries ?? 0 }} 次</span>
          <span v-if="job.limits">并发上限：全局 {{ job.limits.global }} / 单 Agent {{ job.limits.agent }}</span>
        </p>
        <p v-if="job?.status_reason" class="hint">原因：{{ job.status_reason }}</p>
      </div>
      <div class="hero-actions">
        <router-link class="back-link" :to="{ name: 'platform' }">返回平台</router-link>
      </div>
    </header>

    <p v-if="error" class="banner error" role="alert">加载 SuiteJob 详情失败：{{ error }}</p>
    <p v-else-if="loading && !detail" class="banner loading">加载 SuiteJob 详情中…</p>
    <p v-else-if="!detail && !loading" class="empty">未找到该 SuiteJob，或详情加载失败。</p>

    <template v-if="detail && summary">
      <section class="metrics agent-kpis" aria-label="执行汇总">
        <div class="metric">
          <span>Case 总数</span>
          <strong>{{ summary.total }}</strong>
          <small>测试集按顺序逐个执行</small>
        </div>
        <div class="metric">
          <span>已完成</span>
          <strong>{{ summary.completed }}</strong>
          <small>排队 {{ summary.queued }} / 运行中 {{ summary.running }}</small>
        </div>
        <div class="metric">
          <span>失败 / 超时</span>
          <strong>{{ summary.failed }} / {{ summary.timed_out }}</strong>
          <small>已取消 {{ summary.cancelled }} / 已跳过 {{ summary.skipped }}</small>
        </div>
        <div class="metric">
          <span>未评估</span>
          <strong>{{ summary.unknown }}</strong>
          <small>尚无最终尝试的 case（unknown 不算失败）</small>
        </div>
        <div class="metric">
          <span>尝试 / 重试</span>
          <strong>{{ summary.attempts_total }} / {{ summary.retries }}</strong>
          <small>每次重试创建新 Run，不覆盖旧证据</small>
        </div>
        <div class="metric">
          <span>通过率</span>
          <strong>{{ formatPassRate(summary.pass_rate) }}</strong>
          <small>已评估 {{ summary.evaluated }} 个，通过 {{ summary.passed }} 个；分母只含已评估 case</small>
        </div>
      </section>

      <section v-if="budget" class="panel platform-panel" aria-label="预算与限额">
        <div class="panel-head">
          <h3>预算与限额</h3>
          <span v-if="budget.budget_exceeded" class="chip" data-status="failed">预算已耗尽</span>
        </div>
        <p class="hint">
          Token 用量：{{ budget.budget_used.total_tokens }}
          <template v-if="budget.budget.max_total_tokens"> / 上限 {{ budget.budget.max_total_tokens }}</template>
          <template v-else>（不设上限）</template>
          ；尝试用量：{{ budget.budget_used.attempts }}
          <template v-if="budget.budget.max_attempts"> / 上限 {{ budget.budget.max_attempts }}</template>
          <template v-else>（不设上限）</template>。
        </p>
        <p class="hint">
          超时：
          <template v-if="budget.timeout_seconds">套件总超时 {{ budget.timeout_seconds }} 秒</template>
          <template v-else>不设套件总超时</template>
          <template v-if="budget.deadline_at">；截止时间 {{ budget.deadline_at }}</template>
          ；并发上限：
          <template v-if="budget.limits">全局 {{ budget.limits.global }} / 单 Agent {{ budget.limits.agent }}</template>
          <template v-else>默认</template>。
        </p>
        <p v-if="budget.concurrency" class="hint">
          当前并发（进程内快照，重启后归零）：全局活跃 {{ budget.concurrency.global_active }}
          <template v-if="budget.concurrency.agent_active !== undefined">
            / 单 Agent（{{ budget.concurrency.agent_id }}）活跃 {{ budget.concurrency.agent_active }}
          </template>
          。
        </p>
        <p v-if="budget.budget_exceeded_reason" class="banner error">预算耗尽原因：{{ budget.budget_exceeded_reason }}</p>
      </section>

      <section class="panel platform-panel" aria-label="Case 执行">
        <div class="panel-head">
          <h3>Case 执行</h3>
          <span class="chip quiet">{{ caseExecutions.length }}</span>
        </div>
        <table v-if="caseExecutions.length" class="platform-table">
          <thead>
            <tr>
              <th>#</th>
              <th>TestCase</th>
              <th>最终状态</th>
              <th>尝试次数</th>
              <th>重试原因</th>
              <th>最终质量</th>
              <th>Run</th>
              <th>尝试时间线</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="execution in caseExecutions" :key="execution.case_execution_id">
              <tr>
                <td>{{ execution.order + 1 }}</td>
                <td>
                  <code>{{ execution.case_id || "—" }}</code> {{ execution.case_title || "" }}
                </td>
                <td>
                  <span class="chip" :data-status="execution.final_status || 'unknown'">
                    {{ runStatusLabel(execution.final_status) }}
                  </span>
                </td>
                <td>{{ execution.attempt_count }}</td>
                <td>
                  <span v-if="finalAttemptFor(execution)?.status_reason" class="hint">
                    {{ finalAttemptFor(execution)?.status_reason }}
                  </span>
                  <span v-else>—</span>
                </td>
                <td>
                  <span
                    v-if="finalAttemptFor(execution)?.final_quality_status"
                    class="chip"
                    :data-status="finalAttemptFor(execution)?.final_quality_status"
                  >
                    {{ finalQualityLabel(finalAttemptFor(execution)?.final_quality_status) }}
                  </span>
                  <span v-else>未评估</span>
                </td>
                <td>
                  <router-link
                    v-if="finalAttemptFor(execution)?.run_id"
                    :to="{ name: 'run', params: { runId: String(finalAttemptFor(execution)?.run_id) } }"
                  >
                    打开运行详情
                  </router-link>
                  <span v-else>—</span>
                </td>
                <td>
                  <button class="ghost small" type="button" @click="toggleExecution(execution)">
                    {{ expandedExecutions[execution.case_execution_id] ? "收起" : "展开" }}
                  </button>
                </td>
              </tr>
              <tr v-if="expandedExecutions[execution.case_execution_id]">
                <td colspan="8">
                  <table class="platform-table">
                    <thead>
                      <tr>
                        <th>尝试</th>
                        <th>状态</th>
                        <th>重试原因</th>
                        <th>开始</th>
                        <th>结束</th>
                        <th>Run</th>
                      </tr>
                    </thead>
                    <tbody>
                      <tr v-for="attempt in attemptsFor(execution)" :key="attempt.attempt_id">
                        <td>
                          第 {{ Number(attempt.retry_index || 0) + 1 }} 次尝试
                          <span v-if="attempt.is_final_attempt" class="chip quiet">最终尝试</span>
                          <span v-else class="chip quiet">历史尝试</span>
                        </td>
                        <td>
                          <span class="chip" :data-status="attempt.status">{{ runStatusLabel(String(attempt.status)) }}</span>
                        </td>
                        <td>
                          <span v-if="attempt.status_reason" class="hint">{{ attempt.status_reason }}</span>
                          <span v-else>—</span>
                        </td>
                        <td>{{ attempt.started_at || "—" }}</td>
                        <td>{{ attempt.finished_at || "—" }}</td>
                        <td>
                          <router-link v-if="attempt.run_id" :to="{ name: 'run', params: { runId: attempt.run_id } }">
                            打开运行详情
                          </router-link>
                          <span v-else>—</span>
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <p v-else class="empty">该 SuiteJob 还没有任何 Case 执行记录。</p>
      </section>

      <section class="panel platform-panel" aria-label="外部证据">
        <div class="panel-head">
          <h3>外部证据</h3>
          <span v-if="externalEvidence" class="chip quiet">
            {{ externalEvidence.summary.count }} 条（{{ externalEvidence.summary.source_systems.join(", ") || "无来源系统" }}）
          </span>
        </div>
        <p class="hint">
          {{ externalEvidence?.note || "外部证据仅用于展示，不影响确定性审计与裁判结论。" }}
          外部证据与上方确定性审计结果相互独立，不参与 judge overall status 与分数封顶。
        </p>
        <p v-if="externalEvidence" class="hint">
          已关联 Trace {{ externalEvidence.summary.linked_count }} 条，未关联
          {{ externalEvidence.summary.unlinked_count }} 条；外部评测 {{ externalEvidence.summary.evaluation_count }}
          条；来源系统：{{ externalEvidence.summary.source_systems.join(", ") || "无" }}。
        </p>

        <h4>外部 Trace</h4>
        <table v-if="externalTraces.length" class="platform-table">
          <thead>
            <tr>
              <th>来源</th>
              <th>名称</th>
              <th>关联</th>
              <th>延迟</th>
              <th>Token</th>
              <th>成本</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="trace in externalTraces" :key="trace.trace_id">
              <td>{{ trace.source_system }}</td>
              <td>
                {{ trace.name || trace.source_id }}
                <span class="hint mono">（{{ trace.source_id }}）</span>
                <span v-if="trace.error" class="hint">— {{ trace.error }}</span>
              </td>
              <td>
                <span class="chip quiet">{{ confidenceLabel(trace.link?.confidence) }}</span>
                <span class="hint">{{ linkMethodLabel(trace.link?.method) }}</span>
                <router-link
                  v-if="trace.run_id"
                  class="hint"
                  :to="{ name: 'run', params: { runId: trace.run_id } }"
                >
                  关联 Run
                </router-link>
              </td>
              <td>{{ formatLatency(trace.latency_ms) }}</td>
              <td>{{ formatTokens(trace.tokens) }}</td>
              <td>{{ formatExternalCost(trace) }}</td>
              <td>{{ trace.status || "—" }}</td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">未关联外部 Trace。</p>

        <h4>外部评测</h4>
        <table v-if="externalEvaluations.length" class="platform-table">
          <thead>
            <tr>
              <th>指标</th>
              <th>状态</th>
              <th>分数</th>
              <th>标签</th>
              <th>来源</th>
              <th>关联</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="record in externalEvaluations" :key="record.evaluation_id">
              <td>{{ record.metric_name }}</td>
              <td>
                <span class="chip" :data-status="record.status">{{ externalStatusLabel(record.status) }}</span>
              </td>
              <td>{{ formatExternalScore(record) }}</td>
              <td>{{ record.label || "—" }}</td>
              <td>{{ record.source_system }}</td>
              <td>
                <span class="chip quiet">{{ confidenceLabel(record.provenance?.confidence) }}</span>
                <router-link
                  v-if="record.run_id"
                  class="hint"
                  :to="{ name: 'run', params: { runId: record.run_id } }"
                >
                  关联 Run
                </router-link>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-else class="empty">暂无外部评测记录。</p>
      </section>
    </template>
  </div>
</template>
