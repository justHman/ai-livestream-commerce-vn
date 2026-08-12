/** Script authoring — client state machine, legal-state guards, batch flows.
 *
 * Domain-pure: mirrors design Decisions 2 (state machine), 10 (batch = batch
 * UX), 11 (preview), 12 (idempotency), 16 (SSE snapshot/revision). No DOM.
 * Flows return results for tests; the mount layer binds them to controls.
 */

import type { ApiError } from "./api";
import type { Api } from "./api";
import {
  idempotencyKey,
  type AcceptedJob,
  type GenerationPreview,
  type LiveSessionBrief,
  type ScriptClient,
  type ScriptEvent,
  type ScriptItemState,
  type ScriptSet,
} from "./scriptSets";

// ---------------- Reducer state ----------------

export interface ProductTargets {
  [productId: string]: number;
}

export interface AuthoringState {
  scriptSet: ScriptSet | null;
  setId: string;
  brief: LiveSessionBrief;
  productIds: string[];
  targets: ProductTargets;
  preview: GenerationPreview | null;
  previewPending: boolean;
  batch: {
    batchId: string | null;
    status: "idle" | "pending_confirm" | "running" | "completed" | "cancelled" | "partial_failure";
    estimated_calls: number;
    transportRetrying: boolean;
    lastError: string | null;
  };
  selectedApproveIds: string[];
  busy: { [key: string]: boolean };
  status: string;
  errors: string[];
}

export const MIN_TARGET_DURATION_S = 60;
export const MAX_TARGET_DURATION_S = 3600;

export function initialStateAuthoring(): AuthoringState {
  return {
    scriptSet: null,
    setId: "",
    brief: { shop_name: "", host_name: "", persona: "", selling_style: "", transition_policy: "ORDER_AGNOSTIC" },
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
}

// ---------------- Legal-state guards (tasks 13.2, 13.6) ----------------

export function canSubmit(state: ScriptItemState): boolean {
  return state === "DRAFT" || state === "GATE_FAILED";
}

export function canGenerate(state: ScriptItemState): boolean {
  return state === "EMPTY" || state === "DRAFT" || state === "GATE_FAILED" || state === "STALE";
}

export function canFix(state: ScriptItemState): boolean {
  return state === "GATE_FAILED";
}

export function canApprove(state: ScriptItemState): boolean {
  return state === "REVIEWABLE";
}

export function canEdit(state: ScriptItemState): boolean {
  return state === "EMPTY" || state === "DRAFT" || state === "GATE_FAILED" || state === "STALE";
}

export function canRegenerateSegment(
  segment: { status: string; versions: unknown[] },
  itemState: ScriptItemState,
): boolean {
  return segment.status === "gate_failed" && itemState === "GATE_FAILED";
}

export function isApprovedFresh(state: ScriptItemState): boolean {
  return state === "APPROVED";
}

export function isStale(state: ScriptItemState): boolean {
  return state === "STALE";
}

export function isRuntimeReady(state: ScriptItemState): boolean {
  return state === "APPROVED";
}

export function displayStateLabel(state: ScriptItemState): string {
  const labels: Record<ScriptItemState, string> = {
    EMPTY: "Chưa có nội dung",
    DRAFT: "Bản nháp",
    GATE_RUNNING: "Đang chạy gate",
    GATE_FAILED: "Gate thất bại",
    REVIEWABLE: "Sẵn sàng duyệt",
    APPROVED: "Đã duyệt",
    STALE: "Đã hết hạn duyệt",
    PLANNING: "Đang lập kế hoạch",
    GENERATING: "Đang tạo segment",
    AI_FIXING: "Đang sửa bằng AI",
    FAILED_CONTENT: "Thất bại nội dung",
    FAILED_TRANSPORT: "Lỗi kết nối provider",
  };
  return labels[state] ?? state;
}

// ---------------- Batch event application (tasks 13.4, 13.5) ----------------

export interface AuthoringDeps {
  scriptClient: ScriptClient;
  api: Api;
  adminToken: () => string;
}

export function applyBatchEvent(state: AuthoringState, event: ScriptEvent): AuthoringState {
  if (event.type === "batch.snapshot") {
    return {
      ...state,
      batch: { ...state.batch, batchId: event.batch_id, status: batchStatusOf(event.snapshot?.status) },
    };
  }
  if (event.failure) {
    const transport = event.failure.reason === "transport";
    return {
      ...state,
      batch: {
        ...state.batch,
        transportRetrying: transport,
        lastError: transport ? event.failure.message : null,
      },
    };
  }
  if (event.type === "batch.completed" || event.type === "batch.cancelled") {
    return {
      ...state,
      batch: {
        ...state.batch,
        status: event.type === "batch.completed" ? "completed" : "cancelled",
        transportRetrying: false,
      },
      busy: {},
      errors: [],
    };
  }
  return state;
}

function batchStatusOf(status: string | undefined): AuthoringState["batch"]["status"] {
  if (status === "running") return "running";
  if (status === "partial_failure") return "partial_failure";
  if (status === "completed") return "completed";
  if (status === "cancelled") return "cancelled";
  return "running";
}

export type Action =
  | { type: "AUTHORING_SET"; value: Partial<AuthoringState> }
  | { type: "AUTHORING_BRIEF"; field: keyof LiveSessionBrief; value: string }
  | { type: "AUTHORING_TARGET"; productId: string; value: number }
  | { type: "AUTHORING_PREVIEW"; value: GenerationPreview }
  | { type: "AUTHORING_PREVIEW_PENDING"; pending: boolean }
  | { type: "AUTHORING_BATCH"; value: Partial<AuthoringState["batch"]> }
  | { type: "AUTHORING_SSE_EVENT"; value: ScriptEvent }
  | { type: "AUTHORING_SELECT_APPROVE"; productId: string; selected: boolean }
  | { type: "AUTHORING_BUSY"; key: string; busy: boolean }
  | { type: "AUTHORING_STATUS"; status: string; errors?: string[] }
  | { type: "AUTHORING_ERRORS"; errors: string[] };

export function authoringReducer(state: AuthoringState, action: Action): AuthoringState {
  switch (action.type) {
    case "AUTHORING_SET":
      return { ...state, ...action.value };
    case "AUTHORING_BRIEF":
      return { ...state, brief: { ...state.brief, [action.field]: action.value } };
    case "AUTHORING_TARGET":
      return { ...state, targets: { ...state.targets, [action.productId]: action.value } };
    case "AUTHORING_PREVIEW":
      return { ...state, preview: action.value };
    case "AUTHORING_PREVIEW_PENDING":
      return { ...state, previewPending: action.pending };
    case "AUTHORING_BATCH":
      return { ...state, batch: { ...state.batch, ...action.value } };
    case "AUTHORING_SSE_EVENT":
      return applyBatchEvent(state, action.value);
    case "AUTHORING_SELECT_APPROVE": {
      const selected = new Set(state.selectedApproveIds);
      if (action.selected) selected.add(action.productId);
      else selected.delete(action.productId);
      return { ...state, selectedApproveIds: [...selected] };
    }
    case "AUTHORING_BUSY":
      return { ...state, busy: { ...state.busy, [action.key]: action.busy } };
    case "AUTHORING_STATUS":
      return { ...state, status: action.status, errors: action.errors ?? state.errors };
    case "AUTHORING_ERRORS":
      return { ...state, errors: action.errors };
    default:
      return state;
  }
}

// ---------------- Flows (testable, no DOM) ----------------

export interface FlowDeps {
  client: ScriptClient;
  state: AuthoringState;
  dispatch: (action: Action) => void;
  onEvent?: (message: string, tone?: string) => void;
}

function errorMessage(error: unknown): string {
  const api = error as ApiError;
  return api?.status ? `${api.status}: ${api.message ?? "lỗi API"}` : error instanceof Error ? error.message : String(error);
}

function setBusy(deps: FlowDeps, key: string, busy: boolean): void {
  deps.dispatch({ type: "AUTHORING_BUSY", key, busy });
}

export async function createSetFlow(deps: FlowDeps): Promise<void> {
  const { client, state, dispatch } = deps;
  const productIds = state.productIds.filter((id) => id.trim());
  dispatch({ type: "AUTHORING_ERRORS", errors: [] });
  if (!state.brief.shop_name.trim() || !state.brief.host_name.trim()) {
    dispatch({ type: "AUTHORING_STATUS", status: "Thiếu tên shop hoặc tên MC trong brief.", errors: ["brief: cần tên shop và tên MC."] });
    return;
  }
  if (!productIds.length) {
    dispatch({ type: "AUTHORING_STATUS", status: "Chọn ít nhất một sản phẩm.", errors: ["product_ids: chọn ít nhất một sản phẩm."] });
    return;
  }
  setBusy(deps, "save", true);
  try {
    const set = await client.createScriptSet({ brief: state.brief, product_ids: productIds });
    const targets: ProductTargets = {};
    for (const id of productIds) targets[id] = state.targets[id] ?? MIN_TARGET_DURATION_S;
    dispatch({
      type: "AUTHORING_SET",
      value: {
        scriptSet: set,
        setId: set.id,
        targets,
        preview: null,
        batch: { batchId: null, status: "idle", estimated_calls: 0, transportRetrying: false, lastError: null },
        selectedApproveIds: [],
        status: `ScriptSet ${set.id} đã tạo (revision ${set.revision}).`,
        errors: [],
      },
    });
  } catch (error) {
    dispatch({ type: "AUTHORING_STATUS", status: `Tạo ScriptSet thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
  } finally {
    setBusy(deps, "save", false);
  }
}

export async function loadSetFlow(deps: FlowDeps, setId: string): Promise<void> {
  const { client, dispatch } = deps;
  setBusy(deps, "load", true);
  try {
    const set = await client.getScriptSet(setId);
    const targets: ProductTargets = {};
    for (const item of set.products) targets[item.product_id] = item.plan?.target_duration_s ?? item.segments[0]?.target_duration_s ?? MIN_TARGET_DURATION_S;
    dispatch({
      type: "AUTHORING_SET",
      value: {
        scriptSet: set,
        setId: set.id,
        productIds: set.products.map((p) => p.product_id),
        targets,
        preview: null,
        batch: { batchId: null, status: "idle", estimated_calls: 0, transportRetrying: false, lastError: null },
        selectedApproveIds: [],
        status: `Đã tải ScriptSet ${set.id} (revision ${set.revision}).`,
        errors: [],
      },
    });
  } catch (error) {
    dispatch({ type: "AUTHORING_STATUS", status: `Tải ScriptSet thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
  } finally {
    setBusy(deps, "load", false);
  }
}

export function previewProducts(state: AuthoringState): Array<{ product_id: string; target_duration_s: number }> {
  return state.productIds
    .filter((id) => id.trim())
    .map((id) => ({ product_id: id, target_duration_s: clampDuration(state.targets[id]) }));
}

function clampDuration(value: number | undefined): number {
  if (typeof value !== "number" || !Number.isFinite(value)) return MIN_TARGET_DURATION_S;
  return Math.min(MAX_TARGET_DURATION_S, Math.max(MIN_TARGET_DURATION_S, Math.round(value)));
}

export async function previewFlow(deps: FlowDeps): Promise<void> {
  const { client, state, dispatch } = deps;
  const products = previewProducts(state);
  if (!products.length) {
    dispatch({ type: "AUTHORING_STATUS", status: "Chọn ít nhất một sản phẩm để preview.", errors: ["product_ids: trống."] });
    return;
  }
  dispatch({ type: "AUTHORING_PREVIEW_PENDING", pending: true });
  try {
    const preview = await client.previewBatch(state.setId, { products });
    dispatch({ type: "AUTHORING_PREVIEW", value: preview });
    dispatch({ type: "AUTHORING_STATUS", status: `Preview không tốn token: tổng ${preview.estimated_semantic_calls_total} semantic calls cho ${preview.products.length} sản phẩm.`, errors: [] });
  } catch (error) {
    dispatch({ type: "AUTHORING_STATUS", status: `Preview thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
  } finally {
    dispatch({ type: "AUTHORING_PREVIEW_PENDING", pending: false });
  }
}

function batchCallEstimate(state: AuthoringState): number {
  return state.preview?.estimated_semantic_calls_total ?? 0;
}

export async function generateAllFlow(deps: FlowDeps): Promise<AcceptedJob | null> {
  const { client, state, dispatch } = deps;
  const products = previewProducts(state);
  if (!products.length) {
    dispatch({ type: "AUTHORING_STATUS", status: "Không có sản phẩm để Generate All.", errors: ["product_ids: trống."] });
    return null;
  }
  if (state.batch.status === "running" || state.batch.status === "pending_confirm") return null; // idempotent guard
  const estimate = batchCallEstimate(state);
  if (estimate <= 0) {
    dispatch({ type: "AUTHORING_STATUS", status: "Chạy generation-preview trước để biết số semantic calls.", errors: ["preview: cần preview trước khi Generate All."] });
    return null;
  }
  setBusy(deps, "generate-all", true);
  const key = idempotencyKey({ setId: state.setId, revision: state.scriptSet?.revision, products });
  try {
    const job = await client.generateBatch(state.setId, { products }, key);
    dispatch({ type: "AUTHORING_BATCH", value: { batchId: job.batch_id ?? null, status: "running", estimated_calls: estimate, transportRetrying: false, lastError: null } });
    dispatch({ type: "AUTHORING_STATUS", status: `Generate All đã khởi động (batch ${job.batch_id ?? "?"}), ~${estimate} semantic calls.`, errors: [] });
    return job;
  } catch (error) {
    dispatch({ type: "AUTHORING_STATUS", status: `Generate All thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
    return null;
  } finally {
    setBusy(deps, "generate-all", false);
  }
}

export async function cancelBatchFlow(deps: FlowDeps): Promise<void> {
  const { client, state, dispatch } = deps;
  if (!state.batch.batchId || !state.setId) return;
  try {
    await client.cancelBatch(state.setId, state.batch.batchId);
    dispatch({ type: "AUTHORING_BATCH", value: { status: "cancelled" } });
    dispatch({ type: "AUTHORING_STATUS", status: "Đã gửi lệnh hủy batch.", errors: [] });
  } catch (error) {
    dispatch({ type: "AUTHORING_STATUS", status: `Hủy batch thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
  }
}

export async function approveBatchFlow(deps: FlowDeps): Promise<void> {
  const { client, state, dispatch } = deps;
  const ids = state.selectedApproveIds;
  if (!ids.length) {
    dispatch({ type: "AUTHORING_STATUS", status: "Chọn ít nhất một bản REVIEWABLE để duyệt.", errors: ["approve: chưa chọn phiên bản nào."] });
    return;
  }
  setBusy(deps, "approve", true);
  try {
    const result = await client.approveBatch(state.setId, ids);
    dispatch({ type: "AUTHORING_STATUS", status: `Đã duyệt ${result.approvals.length} phiên bản.`, errors: [] });
    await loadSetFlow(deps, state.setId);
  } catch (error) {
    dispatch({ type: "AUTHORING_STATUS", status: `Approve batch thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
  } finally {
    setBusy(deps, "approve", false);
  }
}

// ---------------- Mock transport (stage 2 offline / tests) ----------------

/** Zero-LLM in-memory ScriptClient for offline stage-2 work and tests:
 * manual draft -> submit -> gate -> reviewable -> approve. */
export function createMockScriptClient(): ScriptClient {
  const sets = new Map<string, ScriptSet>();
  let seq = 0;

  function newScriptSet(input: { brief: LiveSessionBrief; product_ids: string[] }): ScriptSet {
    seq += 1;
    const id = `sets-mock-${seq}`;
    const set: ScriptSet = {
      id,
      revision: 1,
      brief: input.brief,
      products: input.product_ids.map((productId, index) => {
        const names: Record<string, string> = {
          P001: "Kem chống nắng La Roche-Posay SPF50+",
          P002: "Serum Vitamin C 20%",
          P003: "Áo thun cotton form rộng",
          P004: "Áo hoodie HeyGen trắng",
        };
        return {
          product_id: productId,
          product_name: names[productId] ?? `Sản phẩm ${productId}`,
          state: "EMPTY",
          source: null,
          plan: null,
          segments: [],
          versions: [],
          current_version: null,
          approvals: [],
          approved_version_id: null,
          approved_revision: null,
          failure: null,
          updated_at: new Date(0, index, 1).toISOString(),
        };
      }),
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    };
    sets.set(id, set);
    return set;
  }

  function touch(set: ScriptSet): void {
    set.revision += 1;
    set.updated_at = new Date().toISOString();
  }

  function segmentFor(set: ScriptSet, productId: string, index: number) {
    const item = set.products.find((p) => p.product_id === productId);
    if (!item) throw new Error(`product ${productId} not found`);
    if (!item.plan) {
      item.plan = {
        plan_id: `${item.product_id}-plan`,
        segment_count: 2,
        target_duration_s: 600,
        segments: [
          { index: 0, title: "Mở đầu và giới thiệu", intent: "hook", target_duration_s: 300 },
          { index: 1, title: "Lợi ích và chốt đơn", intent: "cta", target_duration_s: 300 },
        ],
      };
    }
    const planSegment = item.plan.segments.find((s) => s.index === index);
    let segment = item.segments[index];
    if (!segment) {
      segment = {
        segment_index: index,
        title: planSegment?.title ?? `Segment ${index}`,
        intent: planSegment?.intent ?? "segment",
        target_duration_s: planSegment?.target_duration_s ?? 300,
        status: "pending",
        selected_version_id: null,
        versions: [],
      };
      item.segments[index] = segment;
    }
    return {
      item,
      planSegment,
      versions: segment.versions,
      display: planSegment?.title ?? `Segment ${index}`,
      intent: planSegment?.intent ?? "segment",
      target: planSegment?.target_duration_s ?? 300,
    };
  }

  const client: ScriptClient = {
    async createScriptSet(input) {
      return newScriptSet(input);
    },
    async getScriptSet(setId) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      return structuredClone(set);
    },
    async patchScriptSet(setId, patch) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      set.brief = patch.brief ?? set.brief;
      if (patch.product_ids) {
        set.products = patch.product_ids.map((id) => set.products.find((p) => p.product_id === id) ?? newScriptSet({ brief: set.brief, product_ids: [id] }).products[0]!);
      }
      touch(set);
      return structuredClone(set);
    },
    async putDraft(setId, productId, input) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      const { item } = segmentFor(set, productId, 0);
      const spoken = input.spoken_text ?? input.display_text;
      item.source = "manual";
      item.state = "DRAFT";
      item.current_version = {
        version_id: `${productId}-v${item.versions.length + 1}`,
        version: item.versions.length + 1,
        source: "manual",
        state: "DRAFT",
        display_text: input.display_text,
        spoken_text: spoken,
        estimated_duration_s: Math.round(spoken.length / 12),
        gate_violations: [],
        fingerprint: null,
        created_at: new Date().toISOString(),
      };
      item.versions.push(item.current_version);
      touch(set);
      return { version_id: item.current_version.version_id, version: item.current_version.version, state: "DRAFT", spoken_text: spoken };
    },
    async submit(setId, productId) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      const item = set.products.find((p) => p.product_id === productId);
      if (!item) throw new Error("not found");
      const current = item.current_version;
      if (!current) return { state: "gate_failed", violations: [{ rule_id: "FORMAT_REQUIRED", severity: "ERROR", message: "Chưa có nội dung draft." }] };
      item.state = "GATE_RUNNING";
      touch(set);
      if (current.display_text.includes("--")) {
        item.state = "GATE_FAILED";
        current.state = "GATE_FAILED";
        current.gate_violations = [{ rule_id: "STYLE_DISALLOWED_PUNCTUATION", severity: "ERROR", message: "Phát hiện dấu gạch ngang em-dash bị cấm." }];
        touch(set);
        return { state: "gate_failed", violations: current.gate_violations };
      }
      item.state = "REVIEWABLE";
      current.state = "REVIEWABLE";
      touch(set);
      return { state: "reviewable", version_id: current.version_id };
    },
    async previewProduct(_setId, _productId, targetDurationS) {
      const k = Math.max(1, Math.ceil(targetDurationS / 600));
      return { product_id: _productId, target_duration_s: targetDurationS, planned_segment_count: k, estimated_semantic_calls: k + 1 };
    },
    async previewBatch(_setId, req) {
      const products = req.products.map((p) => {
        const k = Math.max(1, Math.ceil(p.target_duration_s / 600));
        return { product_id: p.product_id, target_duration_s: p.target_duration_s, planned_segment_count: k, estimated_semantic_calls: k + 1 };
      });
      return { products, estimated_semantic_calls_total: products.reduce((sum, p) => sum + p.estimated_semantic_calls, 0) };
    },
    async generateProduct(setId, productId, targetDurationS, _key) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      const { item } = segmentFor(set, productId, 0);
      item.state = "GENERATING";
      item.plan = {
        plan_id: `${productId}-plan`,
        segment_count: 2,
        target_duration_s: targetDurationS,
        segments: [
          { index: 0, title: "Mở đầu và giới thiệu", intent: "hook", target_duration_s: Math.round(targetDurationS / 2) },
          { index: 1, title: "Lợi ích và chốt đơn", intent: "cta", target_duration_s: Math.round(targetDurationS / 2) },
        ],
      };
      item.segments = [
        { segment_index: 0, title: item.plan.segments[0]!.title, intent: "hook", target_duration_s: item.plan.segments[0]!.target_duration_s, status: "pending", selected_version_id: null, versions: [] },
        { segment_index: 1, title: item.plan.segments[1]!.title, intent: "cta", target_duration_s: item.plan.segments[1]!.target_duration_s, status: "pending", selected_version_id: null, versions: [] },
      ];
      touch(set);
      return { status: "accepted", workflow_id: `wf-${productId}` };
    },
    async regenerateSegment(setId, productId, segmentIndex, _key) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      const { item, versions, display, target } = segmentFor(set, productId, segmentIndex);
      if (item.state !== "GATE_FAILED") throw new Error("409: regenerate chỉ hợp lệ khi sản phẩm đang GATE_FAILED");
      const versionNumber = versions.length + 1;
      const version = {
        version_id: `${productId}-s${segmentIndex}-v${versionNumber}`,
        version: versionNumber,
        source: "ai" as const,
        display_text: display,
        spoken_text: display,
        estimated_duration_s: target,
        gate_status: "GATE_PASSED" as const,
        violations: [],
        created_at: new Date().toISOString(),
      };
      versions.push(version);
      const segment = item.segments[segmentIndex];
      if (segment) {
        segment.status = "gate_passed";
        segment.selected_version_id = version.version_id;
      }
      item.state = "DRAFT";
      item.source = "ai";
      item.current_version = {
        version_id: `compiled-${version.version_id}`,
        version: item.versions.length + 1,
        source: "ai",
        state: "DRAFT",
        display_text: item.segments.map((s) => s.title).join(". "),
        spoken_text: item.segments.map((s) => s.title).join(". "),
        estimated_duration_s: item.segments.reduce((sum, s) => sum + s.target_duration_s, 0),
        gate_violations: [],
        fingerprint: { model: "mock", skill_version: "s1", rule_set_version: "r1", prompt_template_version: "p1", plan_version: "plan1", target_duration_s: item.plan?.target_duration_s ?? 600 },
        created_at: new Date().toISOString(),
      };
      item.versions.push(item.current_version);
      touch(set);
      return { status: "accepted", workflow_id: `wf-${productId}-seg${segmentIndex}` };
    },
    async fixProduct(setId, productId, _key) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      const item = set.products.find((p) => p.product_id === productId);
      if (!item) throw new Error("not found");
      if (item.state !== "GATE_FAILED") throw new Error("409: fix chỉ hợp lệ khi gate_failed");
      item.state = "DRAFT";
      item.source = "ai";
      const fixed = item.current_version
        ? { ...item.current_version, version_id: `${productId}-v${item.versions.length + 1}`, version: item.versions.length + 1, state: "DRAFT" as const, gate_violations: [], display_text: item.current_version.display_text.replace("--", "–"), spoken_text: item.current_version.spoken_text.replace("--", "–") }
        : null;
      if (fixed) {
        item.current_version = fixed;
        item.versions.push(fixed);
      }
      touch(set);
      return { status: "accepted", workflow_id: `wf-fix-${productId}` };
    },
    async approveProduct(setId, productId) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      const item = set.products.find((p) => p.product_id === productId);
      if (!item) throw new Error("not found");
      if (item.state !== "REVIEWABLE" || !item.current_version) throw new Error("409: chỉ duyệt phiên bản REVIEWABLE");
      const approval = {
        approval_id: `appr-${productId}-${item.approvals.length + 1}`,
        version_id: item.current_version.version_id,
        version: item.current_version.version,
        approved_by: "operator",
        approved_at: new Date().toISOString(),
        approval_hash: `hash-${item.current_version.version_id}`,
      };
      item.approvals.push(approval);
      item.approved_version_id = item.current_version.version_id;
      item.approved_revision = item.current_version.version;
      item.state = "APPROVED";
      item.current_version.state = "APPROVED";
      touch(set);
      return { approval, state: "APPROVED" };
    },
    async approveBatch(setId, productIds) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      const approvals = [];
      for (const productId of productIds) {
        const result = await client.approveProduct(setId, productId);
        approvals.push(result.approval);
      }
      return { approvals };
    },
    async generateBatch(setId, req, _key) {
      const set = sets.get(setId);
      if (!set) throw new Error("not found");
      for (const product of req.products) {
        await client.generateProduct(setId, product.product_id, product.target_duration_s, _key);
      }
      return { status: "accepted", batch_id: `batch-mock-${++seq}` };
    },
    async getBatch(_setId, batchId) {
      return {
        batch_id: batchId,
        script_set_id: _setId,
        script_set_revision: 1,
        requested_product_ids: [],
        status: "running",
        revision: 1,
        products: [],
        estimated_semantic_calls_total: 0,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      };
    },
    async cancelBatch() {
      return { ok: true };
    },
  };
  return client;
}
