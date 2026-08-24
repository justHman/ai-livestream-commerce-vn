/** Script authoring — DOM mount: renders authoring state into the panel and
 * binds controls to flows. Follows main.ts conventions ($ lookup, innerHTML
 * for lists, escaped text, addEvent status feed). No business logic here. */

import type { Api } from "./api";
import type { Action } from "./authoring";
import {
  approveBatchFlow,
  AUTHORING_ACTOR,
  authoringReducer,
  cancelBatchFlow,
  canApprove,
  canEdit,
  canFix,
  canGenerate,
  canRegenerateSegment,
  canSubmit,
  createSetFlow,
  displayStateLabel,
  generateAllFlow,
  loadSetFlow,
  MAX_TARGET_DURATION_S,
  MIN_TARGET_DURATION_S,
  previewFlow,
  type AuthoringState,
} from "./authoring";
import {
  BatchEventStream,
  createScriptClient,
  idempotencyKey,
  type GenerationPreview,
  type ScriptClient,
  type ScriptItem,
  type ScriptSet,
  type TransitionPolicy,
} from "./scriptSets";

export interface AuthoringMountDeps {
  backendUrl: () => string;
  viewerToken: () => string;
  api: Api;
  onEvent: (message: string, tone?: string) => void;
}

export function mountAuthoring(deps: AuthoringMountDeps): {
  state: AuthoringState;
  dispatch: (action: Action) => void;
} {
  const $ = (id: string): HTMLElement => document.getElementById(id) ?? document.body;

  let state: AuthoringState = {
    scriptSet: null,
    setId: "",
    name: "",
    transitionPolicy: "ORDER_AGNOSTIC",
    brief: { title: "", host_name: "", shop_name: "", note: "" },
    productIds: [],
    targets: {},
    preview: null,
    previewPending: false,
    batch: { batchId: null, status: "idle", estimated_calls: 0, transportRetrying: false, lastError: null },
    selectedApproveIds: [],
    busy: {},
    status: "Chưa có ScriptSet.",
    errors: [],
  };

  const client: ScriptClient = createScriptClient({
    backendUrl: deps.backendUrl(),
    viewerToken: deps.viewerToken,
    adminToken: () => "",
  });

  let sse: BatchEventStream | null = null;
  let sseBatchId: string | null = null;

  function dispatch(action: Action): void {
    state = authoringReducer(state, action);
    render(action);
  }

  function addEvent(message: string, tone?: string): void {
    deps.onEvent(message, tone);
  }

  // ---------------- Flows bound to controls ----------------

  function flowDeps() {
    return { client, state, dispatch, onEvent: addEvent };
  }

  async function createSet(): Promise<void> {
    await createSetFlow(flowDeps());
  }

  async function loadSet(): Promise<void> {
    const setId = ($("scriptSetIdInput") as HTMLInputElement).value.trim();
    if (!setId) {
      addEvent("Nhập ScriptSet ID để tải.", "warning");
      return;
    }
    await loadSetFlow(flowDeps(), setId);
  }

  async function preview(): Promise<void> {
    await previewFlow(flowDeps());
  }

  async function generateAll(): Promise<void> {
    await generateAllFlow(flowDeps());
  }

  async function cancelBatch(): Promise<void> {
    await cancelBatchFlow(flowDeps());
  }

  async function approveBatch(): Promise<void> {
    await approveBatchFlow(flowDeps());
  }

  async function productAction(
    productId: string,
    action: "submit" | "generate" | "fix" | "approve",
  ): Promise<void> {
    const key = `product-${action}-${productId}`;
    if (state.busy[key]) return;
    dispatch({ type: "AUTHORING_BUSY", key, busy: true });
    try {
      if (action === "submit") {
        const result = await client.submit(state.setId, productId);
        const violations = result.gate?.violations?.length ?? 0;
        addEvent(
          `Submit ${productId}: ${result.state}${violations ? ` (${violations} vi phạm)` : ""}.`,
          result.gate?.state === "gate_failed" ? "warning" : "success",
        );
      } else if (action === "generate") {
        const target = state.targets[productId] ?? MIN_TARGET_DURATION_S;
        const keyValue = idempotencyKey({ setId: state.setId, productId, target });
        const job = await client.generateProduct(state.setId, productId, target, keyValue);
        addEvent(`Generate ${productId} đã chấp nhận (workflow ${job.workflow_id ?? "?"}).`, "success");
      } else if (action === "fix") {
        await client.fixProduct(state.setId, productId, idempotencyKey({ setId: state.setId, productId, op: "fix" }));
        addEvent(`Fix with AI ${productId} đã chấp nhận.`, "success");
      } else if (action === "approve") {
        const versionId = state.scriptSet?.products.find((p) => p.product_id === productId)?.current_version?.version_id;
        if (!versionId) throw new Error("409: chưa có phiên bản REVIEWABLE để duyệt");
        const result = await client.approveProduct(state.setId, productId, versionId, AUTHORING_ACTOR);
        addEvent(`Đã duyệt ${productId} (${result.approval.version_id}).`, "success");
      }
      await refresh();
    } catch (error) {
      addEvent(`${action} ${productId} thất bại: ${safeMessage(error)}`, "danger");
    } finally {
      dispatch({ type: "AUTHORING_BUSY", key, busy: false });
    }
  }

  async function regenerate(productId: string, segmentIndex: number): Promise<void> {
    try {
      await client.regenerateSegment(state.setId, productId, segmentIndex, idempotencyKey({ setId: state.setId, productId, segmentIndex, op: "regenerate" }));
      addEvent(`Regenerate segment ${segmentIndex} của ${productId} đã chấp nhận.`, "success");
      await refresh();
    } catch (error) {
      addEvent(`Regenerate ${productId}/${segmentIndex} thất bại: ${safeMessage(error)}`, "danger");
    }
  }

  async function saveDraft(productId: string): Promise<void> {
    const card = document.querySelector(`[data-product-id="${CSS.escape(productId)}"]`) as HTMLElement | null;
    const displayInput = card?.querySelector<HTMLTextAreaElement>('[data-draft-field="display"]');
    const spokenInput = card?.querySelector<HTMLTextAreaElement>('[data-draft-field="spoken"]');
    const display = displayInput?.value ?? "";
    const spoken = spokenInput?.value ?? "";
    if (!display.trim()) {
      addEvent(`Draft ${productId}: cần display text.`, "warning");
      return;
    }
    try {
      const result = await client.putDraft(state.setId, productId, { display_text: display, spoken_text: spoken.trim() ? spoken : undefined });
      addEvent(`Đã lưu draft ${productId} (${result.state}).`, "success");
      await refresh();
    } catch (error) {
      addEvent(`Lưu draft ${productId} thất bại: ${safeMessage(error)}`, "danger");
    }
  }

  async function refresh(): Promise<void> {
    if (!state.setId) return;
    try {
      const set = await client.getScriptSet(state.setId);
      dispatch({ type: "AUTHORING_SET", value: { scriptSet: set, targets: keepTargets(state.targets, set) } });
    } catch (error) {
      addEvent(`Tải lại ScriptSet thất bại: ${safeMessage(error)}`, "danger");
    }
  }

  // ---------------- SSE ----------------

  function attachSse(): void {
    sse?.disconnect();
    const batchId = state.batch.batchId;
    if (!batchId || !state.setId) return;
    sseBatchId = batchId;
    sse = new BatchEventStream({
      backendUrl: deps.backendUrl(),
      viewerToken: deps.viewerToken,
      scriptSetId: state.setId,
      batchId,
      onEvent: (event) => {
        dispatch({ type: "AUTHORING_SSE_EVENT", value: event });
        if (event.type === "product.failed") addEvent(`${event.product_id ?? ""} thất bại: ${event.reason ?? "unknown"}`, "danger");
        if (event.type === "batch.snapshot") addEvent("Đã đồng bộ snapshot batch.", "success");
      },
      onStatus: addEvent,
    });
    void sse.connect();
  }

  // ---------------- Render ----------------

  function render(_action: Action): void {
    renderForm();
    renderSet();
    renderPreview();
    renderBatch();
    renderItems();
    renderStatus();
    // Bind SSE once per batch; BatchEventStream reconnects on transport errors (snapshot replay).
    if (state.setId && state.batch.batchId && state.batch.batchId !== sseBatchId) attachSse();
  }

  function renderForm(): void {
    if (state.setId) {
      ($("scriptSetCreateForm") as HTMLElement).classList.add("hidden");
      return;
    }
    ($("scriptSetCreateForm") as HTMLElement).classList.remove("hidden");
    if (document.activeElement !== $("scriptSetName")) ($("scriptSetName") as HTMLInputElement).value = state.name;
    if (document.activeElement !== $("briefTitle")) ($("briefTitle") as HTMLInputElement).value = state.brief.title;
    if (document.activeElement !== $("briefShopName")) ($("briefShopName") as HTMLInputElement).value = state.brief.shop_name ?? "";
    if (document.activeElement !== $("briefHostName")) ($("briefHostName") as HTMLInputElement).value = state.brief.host_name ?? "";
    if (document.activeElement !== $("briefNote")) ($("briefNote") as HTMLTextAreaElement).value = state.brief.note ?? "";
    ($("briefTransition") as HTMLSelectElement).value = state.transitionPolicy;
    const list = $("scriptSetProductList");
    const fragment = document.createDocumentFragment();
    for (const productId of state.productIds) {
      const label = document.createElement("label");
      label.className = "script-product-pick";
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = true;
      check.addEventListener("change", () => {
        // Keep the catalog list stable; toggling selection is done via productIds edit.
        addEvent("Bỏ chọn sản phẩm: xóa ID khỏi danh sách bên dưới rồi tạo ScriptSet.", "warning");
      });
      const span = document.createElement("span");
      span.textContent = productId;
      label.append(check, span);
      fragment.appendChild(label);
    }
    list.replaceChildren(fragment);
  }

  function renderSet(): void {
    const set = state.scriptSet;
    ($("scriptSetSummary") as HTMLElement).textContent = set
      ? `ScriptSet ${set.id} · revision ${set.revision} · ${set.products.length} sản phẩm · policy ${set.transition_policy}`
      : "Chưa có ScriptSet.";
  }

  function renderPreview(): void {
    const preview = state.preview;
    const container = $("scriptPreviewResults");
    if (!preview) {
      container.textContent = "Chạy generation-preview (không tốn token) trước khi Generate All.";
      return;
    }
    const rows = preview.products
      .map(
        (p) =>
          `<div class="script-preview-row"><span>${escapeHtml(p.product_id)} · ${p.target_duration_s}s</span>` +
          `<span>K=${p.planned_segment_count} · ~${p.estimated_semantic_calls} calls</span></div>`,
      )
      .join("");
    container.innerHTML =
      `<div class="script-preview-total">Tổng ước tính: ${preview.estimated_semantic_calls_total} semantic calls</div>${rows}`;
  }

  function renderBatch(): void {
    const batch = state.batch;
    const el = $("scriptBatchStatus") as HTMLElement;
    const busy = state.busy["generate-all"];
    ($("generateAllBtn") as HTMLButtonElement).disabled = !state.setId || busy || batch.status === "running";
    ($("cancelBatchBtn") as HTMLButtonElement).disabled = !(batch.batchId && batch.status === "running");
    ($("approveBatchBtn") as HTMLButtonElement).disabled = !state.selectedApproveIds.length || Boolean(state.busy["approve"]);
    el.textContent = batch.batchId
      ? `Batch ${batch.batchId} · ${batch.status} · ~${batch.estimated_calls} semantic calls` +
        (batch.transportRetrying ? " · ĐANG THỬ LẠI TRANSPORT" : "")
      : "Chưa có batch.";
    el.dataset.tone = batch.transportRetrying ? "warning" : batch.status === "partial_failure" ? "danger" : "";
    ($("batchLastError") as HTMLElement).textContent = batch.lastError ?? "";
  }

  function renderItems(): void {
    const container = $("scriptItemList");
    const set = state.scriptSet;
    if (!set) {
      container.textContent = "Chưa có sản phẩm.";
      return;
    }
    container.replaceChildren(...set.products.map((item) => renderItemCard(item, state)));
  }

  function renderStatus(): void {
    ($("scriptSetStatus") as HTMLElement).textContent = state.status;
    ($("scriptSetErrors") as HTMLElement).textContent = state.errors.join("\n");
  }

  // ---------------- Binding ----------------

  function bind(): void {
    ($("createScriptSetBtn") as HTMLButtonElement).addEventListener("click", () => void createSet());
    ($("loadScriptSetBtn") as HTMLButtonElement).addEventListener("click", () => void loadSet());
    ($("scriptPreviewBtn") as HTMLButtonElement).addEventListener("click", () => void preview());
    ($("generateAllBtn") as HTMLButtonElement).addEventListener("click", () => void generateAll());
    ($("cancelBatchBtn") as HTMLButtonElement).addEventListener("click", () => void cancelBatch());
    ($("approveBatchBtn") as HTMLButtonElement).addEventListener("click", () => void approveBatch());
    ($("scriptSetName") as HTMLInputElement).addEventListener("input", (event) => dispatch({ type: "AUTHORING_SET", value: { name: (event.target as HTMLInputElement).value } }));
    ($("briefTitle") as HTMLInputElement).addEventListener("input", (event) => dispatch({ type: "AUTHORING_BRIEF", field: "title", value: (event.target as HTMLInputElement).value }));
    ($("briefShopName") as HTMLInputElement).addEventListener("input", (event) => dispatch({ type: "AUTHORING_BRIEF", field: "shop_name", value: (event.target as HTMLInputElement).value }));
    ($("briefHostName") as HTMLInputElement).addEventListener("input", (event) => dispatch({ type: "AUTHORING_BRIEF", field: "host_name", value: (event.target as HTMLInputElement).value }));
    ($("briefNote") as HTMLTextAreaElement).addEventListener("input", (event) => dispatch({ type: "AUTHORING_BRIEF", field: "note", value: (event.target as HTMLTextAreaElement).value }));
    ($("briefTransition") as HTMLSelectElement).addEventListener("change", (event) => dispatch({ type: "AUTHORING_SET", value: { transitionPolicy: (event.target as HTMLSelectElement).value as TransitionPolicy } }));
    ($("scriptSetProductIds") as HTMLInputElement).addEventListener("input", (event) => {
      const ids = (event.target as HTMLInputElement).value.split(",").map((s) => s.trim()).filter(Boolean);
      dispatch({ type: "AUTHORING_SET", value: { productIds: ids } });
    });
    ($("scriptItemList") as HTMLElement).addEventListener("click", (event) => {
      const button = (event.target as HTMLElement).closest<HTMLButtonElement>("button[data-author-action]");
      if (!button) return;
      const productId = button.dataset.productId;
      const action = button.dataset.authorAction;
      const segmentIndex = button.dataset.segmentIndex;
      if (!productId || !action) return;
      if (action === "select-approve") {
        dispatch({ type: "AUTHORING_SELECT_APPROVE", productId, selected: button.dataset.selected === "1" ? false : true });
        return;
      }
      if (action === "submit" || action === "generate" || action === "fix" || action === "approve") {
        void productAction(productId, action);
        return;
      }
      if (action === "regenerate" && segmentIndex !== undefined) {
        void regenerate(productId, Number(segmentIndex));
        return;
      }
      if (action === "save-draft") {
        void saveDraft(productId);
      }
    });
    // Target duration inputs live inside item cards. 'change' (not 'input')
    // so re-render does not steal focus mid-typing.
    ($("scriptItemList") as HTMLElement).addEventListener("change", (event) => {
      const input = event.target as HTMLInputElement;
      if (input.dataset.targetFor) {
        const value = Number(input.value);
        dispatch({ type: "AUTHORING_TARGET", productId: input.dataset.targetFor, value: Number.isFinite(value) ? value : MIN_TARGET_DURATION_S });
      }
    });
  }

  bind();
  return { state, dispatch };
}

// ---------------- helpers ----------------

function keepTargets(prev: AuthoringState["targets"], set: ScriptSet): AuthoringState["targets"] {
  const next: AuthoringState["targets"] = {};
  for (const item of set.products) {
    next[item.product_id] = prev[item.product_id] ?? item.plan?.target_duration_s ?? item.segments[0]?.target_duration_s ?? MIN_TARGET_DURATION_S;
  }
  return next;
}

function safeMessage(error: unknown): string {
  return error instanceof Error ? error.message : "Lỗi không xác định";
}

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function renderItemCard(item: ScriptItem, state: AuthoringState): HTMLElement {
  const card = document.createElement("article");
  card.className = "script-item-card";
  card.dataset.productId = item.product_id;
  card.dataset.state = item.state;
  card.innerHTML = `
    <div class="script-item-header">
      <div class="script-item-title">
        <strong>${escapeHtml(item.product_name)}</strong>
        <span class="script-state" data-state="${item.state}">${displayStateLabel(item.state)}</span>
      </div>
      <div class="script-item-meta">${item.approved_version_id ? `Đã duyệt v${item.approved_revision} · ` : ""}${item.source ?? "chưa có nguồn"}</div>
      <div class="field script-target-field">
        <label for="target-${escapeHtml(item.product_id)}">Target duration (giây)</label>
        <input id="target-${escapeHtml(item.product_id)}" type="number" min="${MIN_TARGET_DURATION_S}" max="${MAX_TARGET_DURATION_S}" step="60" data-target-for="${escapeHtml(item.product_id)}" value="${state.targets[item.product_id] ?? MIN_TARGET_DURATION_S}">
      </div>
    </div>
    ${renderPlanHtml(item)}
    ${renderSegmentsHtml(item, state)}
    ${renderVersionsHtml(item)}
    ${renderDraftHtml(item, state)}
    <div class="actions script-item-actions">
      ${actionButton(item, state, "submit", "Submit gate")}
      ${actionButton(item, state, "generate", "Generate Script")}
      ${actionButton(item, state, "fix", "Fix with AI")}
      ${actionButton(item, state, "approve", "Approve")}
    </div>
  `;
  return card;
}

function renderPlanHtml(item: ScriptItem): string {
  if (!item.plan) return "";
  return `<div class="script-plan">Kế hoạch: ${item.plan.segment_count} segments · ${item.plan.target_duration_s}s · ${item.plan.segments.map((s) => `${s.index}:${escapeHtml(s.intent)}`).join(", ")}</div>`;
}

function renderSegmentsHtml(item: ScriptItem, _state: AuthoringState): string {
  if (!item.segments.length) return "";
  const rows = item.segments
    .map((segment) => {
      const selected = segment.versions.find((v) => v.version_id === segment.selected_version_id) ?? segment.versions[segment.versions.length - 1];
      const display = selected ? escapeHtml(selected.display_text) : "(chưa có nội dung)";
      const spoken = selected ? escapeHtml(selected.spoken_text) : "(chưa có)";
      const violations = (selected?.violations ?? []).map((v) => `<div class="script-violation">${escapeHtml(v.rule_id)}: ${escapeHtml(v.message)}</div>`).join("");
      const regenerateDisabled = !canRegenerateSegment(segment, item.state);
      return `
        <details class="script-segment">
          <summary>${segment.segment_index}. ${escapeHtml(segment.title)} · ${segment.target_duration_s}s · ${segment.status}</summary>
          <div class="script-segment-body">
            <div class="script-spoken"><strong>Spoken (bản được duyệt):</strong> ${spoken}</div>
            <div class="script-display"><strong>Display:</strong> ${display}</div>
            ${violations}
            <div class="actions"><button type="button" class="secondary" data-author-action="regenerate" data-product-id="${escapeHtml(item.product_id)}" data-segment-index="${segment.segment_index}" ${regenerateDisabled ? "disabled" : ""}>Regenerate Segment</button></div>
          </div>
        </details>`;
    })
    .join("");
  return `<div class="script-segments">${rows}</div>`;
}

function renderVersionsHtml(item: ScriptItem): string {
  if (!item.versions.length) return "";
  const list = item.versions
    .map(
      (v) =>
        `<li>v${v.version} · ${v.source} · ${v.state} · ${v.estimated_duration_s}s · ${v.version_id}</li>`,
    )
    .join("");
  return `<details class="script-versions"><summary>Lịch sử phiên bản (${item.versions.length})</summary><ul>${list}</ul></details>`;
}

function renderDraftHtml(item: ScriptItem, _state: AuthoringState): string {
  const editable = canEdit(item.state);
  const current = item.current_version;
  return `
    <div class="script-draft">
      <label>Draft display text</label>
      <textarea data-draft-field="display" ${editable ? "" : "disabled"} rows="3" maxlength="2000">${escapeHtml(current?.display_text ?? "")}</textarea>
      <label>Draft spoken text (nếu khác)</label>
      <textarea data-draft-field="spoken" ${editable ? "" : "disabled"} rows="3" maxlength="2000">${escapeHtml(current?.spoken_text ?? "")}</textarea>
      <div class="actions"><button type="button" class="secondary" data-author-action="save-draft" data-product-id="${escapeHtml(item.product_id)}" ${editable ? "" : "disabled"}>Lưu bản nháp</button></div>
    </div>
  `;
}

function actionButton(item: ScriptItem, state: AuthoringState, action: string, label: string): string {
  let disabled = false;
  if (action === "submit") disabled = !canSubmit(item.state);
  else if (action === "generate") disabled = !canGenerate(item.state);
  else if (action === "fix") disabled = !canFix(item.state);
  else if (action === "approve") disabled = !canApprove(item.state);
  const busy = state.busy[`product-${action}-${item.product_id}`];
  if (busy) disabled = true;
  const approveSelected = state.selectedApproveIds.includes(item.product_id) ? ' data-selected="1"' : "";
  const dataAction = action === "approve" ? "select-approve" : action;
  return `<button type="button" data-author-action="${dataAction}" data-product-id="${escapeHtml(item.product_id)}" ${disabled ? "disabled" : ""}${approveSelected} class="${action === "approve" ? "approve-select" : ""}">${label}</button>`;
}

export function formatDuration(s: number): string {
  return `${Math.round(s)}s`;
}

export function previewTotal(preview: GenerationPreview | null): number {
  return preview?.estimated_semantic_calls_total ?? 0;
}
