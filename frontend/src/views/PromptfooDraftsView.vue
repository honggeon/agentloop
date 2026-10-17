<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  apiErrorMessage,
  approvePromptfooDraft,
  archivePromptfooDraft,
  fetchAuthContext,
  fetchPromptfooDrafts,
  rejectPromptfooDraft,
  DRAFT_STATUS_LABELS,
  type AuthContext,
  type PromptfooDraft,
  type PromptfooInputOverride
} from "../api";

const PAGE_SIZE = 50;

const authContext = ref<AuthContext | null>(null);
const drafts = ref<PromptfooDraft[]>([]);
const total = ref(0);
const offset = ref(0);
const hasMore = ref(false);
const loading = ref(false);
const error = ref("");
const actionError = ref("");
const actionNotice = ref("");
const busyId = ref("");

// approve 输入补全（input_override）：仅在 draft vars 缺 user_id/message 时使用。
const overrideForms = ref<Record<string, { user_id: string; message: string; session_id: string }>>({});
const rejectReasons = ref<Record<string, string>>({});

const isLocalDevMode = computed(() => authContext.value?.auth_mode === "disabled");
// approve 需要 admin；reject / archive 需要 operator。
const canApprove = computed(() => Boolean(authContext.value?.permissions?.approve_draft));
const canReview = computed(() => Boolean(authContext.value?.permissions?.review_draft));

const pageRange = computed(() => {
  if (!drafts.value.length) return "0 / 0";
  return `${offset.value + 1} - ${offset.value + drafts.value.length} / ${total.value}`;
});

function statusLabel(status: string | null | undefined): string {
  if (!status) return "草稿";
  return DRAFT_STATUS_LABELS[status] || status;
}

function mappedCount(draft: PromptfooDraft): number {
  return (draft.expectations || []).filter((item) => item.kind !== "unmapped").length;
}

function unmappedCount(draft: PromptfooDraft): number {
  if (typeof draft.unmapped_count === "number") return draft.unmapped_count;
  return (draft.expectations || []).filter((item) => item.kind === "unmapped").length;
}

// vars 缺 user_id 或 message 时，approve 需要用户补充 input_override 后重试。
function needsInputOverride(draft: PromptfooDraft): boolean {
  const vars = draft.vars || {};
  return !vars["user_id"] || !vars["message"];
}

function approveDisabled(draft: PromptfooDraft): boolean {
  return busyId.value === draft.draft_id || !canApprove.value || draft.declared_executable || draft.status !== "draft";
}

function approveTitle(draft: PromptfooDraft): string {
  if (!canApprove.value) return "当前角色无 approve 权限（需要 admin）";
  if (draft.declared_executable) return "声明了可执行内容，不能 approve";
  if (draft.status !== "draft") return "仅草稿状态可以 approve";
  if (needsInputOverride(draft)) return "该草稿缺少 user_id/message，请在下方补充后 approve";
  return "提升为正式 TestCase（幂等，重复 approve 不重复创建）";
}

function reviewTitle(): string {
  if (!canReview.value) return "当前角色无审核权限（需要 operator）";
  return "";
}

function replaceDraft(updated: PromptfooDraft) {
  const index = drafts.value.findIndex((item) => item.draft_id === updated.draft_id);
  if (index >= 0) {
    drafts.value.splice(index, 1, updated);
  }
}

async function loadDrafts() {
  loading.value = true;
  error.value = "";
  try {
    const payload = await fetchPromptfooDrafts(PAGE_SIZE, offset.value);
    drafts.value = payload.drafts;
    total.value = payload.total;
    hasMore.value = payload.has_more;
    for (const draft of payload.drafts) {
      if (!overrideForms.value[draft.draft_id]) {
        overrideForms.value[draft.draft_id] = { user_id: "", message: "", session_id: "" };
      }
    }
  } catch (err) {
    drafts.value = [];
    error.value = apiErrorMessage(err);
  } finally {
    loading.value = false;
  }
}

function buildOverride(draftId: string): PromptfooInputOverride | undefined {
  const form = overrideForms.value[draftId];
  if (!form) return undefined;
  const override: PromptfooInputOverride = {};
  if (form.user_id.trim()) override.user_id = form.user_id.trim();
  if (form.message.trim()) override.message = form.message.trim();
  if (form.session_id.trim()) override.session_id = form.session_id.trim();
  return Object.keys(override).length ? override : undefined;
}

async function approveDraft(draft: PromptfooDraft) {
  if (approveDisabled(draft)) return;
  busyId.value = draft.draft_id;
  actionError.value = "";
  actionNotice.value = "";
  try {
    const result = await approvePromptfooDraft(draft.draft_id, buildOverride(draft.draft_id));
    replaceDraft(result.draft);
    actionNotice.value = result.idempotent_replay
      ? `草稿 ${draft.draft_id} 此前已 approve，本次为幂等重放，未重复创建 TestCase。`
      : `草稿 ${draft.draft_id} 已提升为 TestCase：${result.test_case?.case_id || result.draft.promoted_case_id || "—"}`;
  } catch (err) {
    // 409 draft_invalid_state 等：message 是后端安全文案，直接展示。
    actionError.value = apiErrorMessage(err);
  } finally {
    busyId.value = "";
  }
}

async function rejectDraft(draft: PromptfooDraft) {
  if (!canReview.value || busyId.value === draft.draft_id || draft.status !== "draft") return;
  busyId.value = draft.draft_id;
  actionError.value = "";
  actionNotice.value = "";
  try {
    const reason = (rejectReasons.value[draft.draft_id] || "").trim();
    const result = await rejectPromptfooDraft(draft.draft_id, reason || undefined);
    replaceDraft(result.draft);
    actionNotice.value = `草稿 ${draft.draft_id} 已拒绝。`;
  } catch (err) {
    actionError.value = apiErrorMessage(err);
  } finally {
    busyId.value = "";
  }
}

async function archiveDraft(draft: PromptfooDraft) {
  if (!canReview.value || busyId.value === draft.draft_id || draft.status === "archived") return;
  busyId.value = draft.draft_id;
  actionError.value = "";
  actionNotice.value = "";
  try {
    const result = await archivePromptfooDraft(draft.draft_id);
    replaceDraft(result.draft);
    actionNotice.value = `草稿 ${draft.draft_id} 已归档。`;
  } catch (err) {
    actionError.value = apiErrorMessage(err);
  } finally {
    busyId.value = "";
  }
}

function prevPage() {
  if (offset.value <= 0) return;
  offset.value = Math.max(0, offset.value - PAGE_SIZE);
  void loadDrafts();
}

function nextPage() {
  if (!hasMore.value) return;
  offset.value += PAGE_SIZE;
  void loadDrafts();
}

onMounted(async () => {
  try {
    authContext.value = await fetchAuthContext();
  } catch {
    authContext.value = null;
  }
  void loadDrafts();
});
</script>

<template>
  <div class="page">
    <header class="page-hero">
      <div class="hero-copy">
        <p class="eyebrow">集成</p>
        <h2>promptfoo 草稿审核</h2>
        <p class="lede">
          promptfoo 导入的用例先落为草稿，经人工审核后才可提升为正式 TestCase。未映射断言保留为
          声明式、不可执行的期望；provider 声明仅声明，不执行。
        </p>
      </div>
      <div class="hero-actions">
        <button class="ghost small" type="button" :disabled="loading" @click="loadDrafts">
          {{ loading ? "刷新中…" : "刷新" }}
        </button>
      </div>
    </header>

    <p v-if="isLocalDevMode" class="banner quiet">本地开发模式：认证已禁用，所有操作按本地身份放行。</p>
    <p v-if="error" class="banner error" role="alert">加载草稿列表失败：{{ error }}</p>
    <p v-if="actionError" class="banner error" role="alert">{{ actionError }}</p>
    <p v-if="actionNotice" class="banner success">{{ actionNotice }}</p>

    <section class="panel platform-panel" aria-label="草稿列表">
      <div class="panel-head">
        <h3>草稿列表</h3>
        <span class="chip quiet">{{ pageRange }}</span>
      </div>

      <p v-if="loading && !drafts.length" class="empty">加载草稿中…</p>
      <p v-else-if="!drafts.length" class="empty">暂无 promptfoo 草稿。</p>

      <article v-for="draft in drafts" :key="draft.draft_id" class="panel platform-panel">
        <div class="panel-head">
          <h3>
            <span class="mono">{{ draft.draft_id }}</span>
            {{ draft.title }}
          </h3>
          <span class="chip" :data-status="draft.status === 'approved' ? 'pass' : draft.status === 'rejected' ? 'failed' : 'queued'">
            {{ statusLabel(draft.status) }}
          </span>
        </div>

        <p class="hint">
          case_id：<span class="mono">{{ draft.case_id }}</span>
          ；来源：{{ draft.source_system }}<template v-if="draft.source_index !== null && draft.source_index !== undefined"> #{{ draft.source_index }}</template>
          ；safe_to_execute：{{ draft.safe_to_execute ? "是" : "否" }}
          <template v-if="draft.reviewed_by">；审核人：{{ draft.reviewed_by }}（{{ draft.reviewed_at || "—" }}）</template>
          <template v-if="draft.promoted_case_id">；已提升为：<span class="mono">{{ draft.promoted_case_id }}</span></template>
        </p>

        <p v-if="draft.provider_declaration" class="hint">
          provider 声明：<span class="mono">{{ draft.provider_declaration.id }}</span>
          <template v-if="draft.provider_declaration.label">（{{ draft.provider_declaration.label }}）</template>
          <span class="chip quiet">仅声明，不执行</span>
          <span v-if="draft.provider_declaration.executable" class="chip" data-status="failed">声明可执行</span>
        </p>

        <p v-if="draft.declared_executable" class="banner error">
          该草稿声明了可执行内容（{{ (draft.forbidden_keys || []).join(", ") || "command/plugin/script" }}）：
          声明了可执行内容，不能 approve，平台没有安全执行能力。
        </p>

        <p class="hint">
          断言：已映射 {{ mappedCount(draft) }} 条，未映射 {{ unmappedCount(draft) }} 条（未映射保留为声明式期望，不可执行）。
        </p>
        <table v-if="draft.expectations && draft.expectations.length" class="platform-table">
          <thead>
            <tr>
              <th>kind</th>
              <th>原始类型</th>
              <th>值</th>
              <th>说明</th>
              <th>可执行</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(assertion, index) in draft.expectations" :key="index">
              <td>
                <span class="mono">{{ assertion.kind }}</span>
                <span v-if="assertion.kind === 'unmapped'" class="chip quiet">未映射</span>
              </td>
              <td>{{ assertion.original_type || "—" }}</td>
              <td>{{ assertion.value === undefined || assertion.value === null ? "—" : String(assertion.value) }}</td>
              <td>{{ assertion.reason || "—" }}</td>
              <td>{{ assertion.safe_to_execute ? "是" : "否" }}</td>
            </tr>
          </tbody>
        </table>

        <div v-if="draft.status === 'draft' && needsInputOverride(draft)" class="panel">
          <p class="hint">该草稿 vars 缺少 user_id / message，approve 前请补充 input_override：</p>
          <p class="hint">
            user_id：
            <input v-model="overrideForms[draft.draft_id].user_id" type="text" placeholder="user_id" />
            message：
            <input v-model="overrideForms[draft.draft_id].message" type="text" placeholder="message" />
            session_id（可选）：
            <input v-model="overrideForms[draft.draft_id].session_id" type="text" placeholder="session_id" />
          </p>
        </div>

        <p class="hint">
          拒绝原因（可选）：
          <input v-model="rejectReasons[draft.draft_id]" type="text" placeholder="reject 时随请求提交" />
        </p>

        <p class="hero-action-group">
          <button
            class="ghost small"
            type="button"
            :disabled="approveDisabled(draft)"
            :title="approveTitle(draft)"
            @click="approveDraft(draft)"
          >
            {{ busyId === draft.draft_id ? "处理中…" : "approve（提升为 TestCase）" }}
          </button>
          <button
            class="ghost small danger"
            type="button"
            :disabled="!canReview || busyId === draft.draft_id || draft.status !== 'draft'"
            :title="reviewTitle() || '仅草稿状态可以 reject'"
            @click="rejectDraft(draft)"
          >
            reject
          </button>
          <button
            class="ghost small"
            type="button"
            :disabled="!canReview || busyId === draft.draft_id || draft.status === 'archived'"
            :title="reviewTitle() || '归档后不再出现在待审核列表'"
            @click="archiveDraft(draft)"
          >
            archive
          </button>
        </p>
      </article>

      <p class="hint">
        <button class="ghost small" type="button" :disabled="offset <= 0 || loading" @click="prevPage">上一页</button>
        <button class="ghost small" type="button" :disabled="!hasMore || loading" @click="nextPage">下一页</button>
      </p>
    </section>
  </div>
</template>
