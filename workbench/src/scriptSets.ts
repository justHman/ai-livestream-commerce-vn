/** Script authoring — canonical types + REST/SSE client for /api/v1/script-sets.
 *
 * Payloads mirror design Decision 16 (REST/JSON + SSE) and Decision 11
 * (generation preview / semantic-call estimate). Transport mirrors api.ts
 * conventions (ApiError normalization); no UI state, no DOM references.
 */

import { ApiError, type ApiDeps } from "./api";

// ---------------- Domain types (task 2.1, Decision 16) ----------------

export type TransitionPolicy = "ORDER_AWARE" | "ORDER_AGNOSTIC";

export interface LiveSessionBrief {
  shop_name: string;
  host_name: string;
  persona: string;
  selling_style: string;
  transition_policy: TransitionPolicy;
}

export type ScriptItemState =
  | "EMPTY"
  | "DRAFT"
  | "GATE_RUNNING"
  | "GATE_FAILED"
  | "REVIEWABLE"
  | "APPROVED"
  | "STALE"
  | "PLANNING"
  | "GENERATING"
  | "AI_FIXING"
  | "FAILED_CONTENT"
  | "FAILED_TRANSPORT";

export type ScriptSource = "manual" | "ai";

export interface GateViolation {
  rule_id: string;
  severity: "ERROR" | "WARNING";
  message: string;
  segment_index?: number;
}

export interface PlanSegment {
  index: number;
  title: string;
  intent: string;
  target_duration_s: number;
}

export interface ProductScriptPlan {
  plan_id: string;
  segment_count: number;
  target_duration_s: number;
  segments: PlanSegment[];
}

export interface SegmentVersion {
  version_id: string;
  version: number;
  source: ScriptSource;
  display_text: string;
  spoken_text: string;
  estimated_duration_s: number;
  gate_status: "DRAFT" | "GATE_FAILED" | "GATE_PASSED";
  violations: GateViolation[];
  created_at: string;
}

export interface ScriptSegment {
  segment_index: number;
  title: string;
  intent: string;
  target_duration_s: number;
  status: "pending" | "generating" | "gate_passed" | "gate_failed" | "failed_transport";
  selected_version_id: string | null;
  versions: SegmentVersion[];
}

export interface GenerationFingerprint {
  model: string;
  skill_version: string;
  rule_set_version: string;
  prompt_template_version: string;
  plan_version: string;
  target_duration_s: number;
}

export interface CompiledScriptVersion {
  version_id: string;
  version: number;
  source: ScriptSource;
  state: ScriptItemState;
  display_text: string;
  spoken_text: string;
  estimated_duration_s: number;
  gate_violations: GateViolation[];
  fingerprint: GenerationFingerprint | null;
  created_at: string;
}

export interface ApprovalRecord {
  approval_id: string;
  version_id: string;
  version: number;
  approved_by: string;
  approved_at: string;
  approval_hash: string;
}

export interface FailureInfo {
  reason: "content" | "transport";
  message: string;
}

export interface ScriptItem {
  product_id: string;
  product_name: string;
  state: ScriptItemState;
  source: ScriptSource | null;
  plan: ProductScriptPlan | null;
  segments: ScriptSegment[];
  versions: CompiledScriptVersion[];
  current_version: CompiledScriptVersion | null;
  approvals: ApprovalRecord[];
  approved_version_id: string | null;
  approved_revision: number | null;
  failure: FailureInfo | null;
  updated_at: string;
}

export interface ScriptSet {
  id: string;
  revision: number;
  brief: LiveSessionBrief;
  products: ScriptItem[];
  created_at: string;
  updated_at: string;
}

export interface ScriptSetInput {
  brief: LiveSessionBrief;
  product_ids: string[];
}

export interface ScriptSetPatch {
  brief?: LiveSessionBrief;
  product_ids?: string[];
}

export interface DraftInput {
  display_text: string;
  spoken_text?: string;
}

// ---------------- Preview (Decision 11) ----------------

export interface ProductPreview {
  product_id: string;
  target_duration_s: number;
  planned_segment_count: number;
  estimated_semantic_calls: number;
}

export interface GenerationPreview {
  products: ProductPreview[];
  estimated_semantic_calls_total: number;
}

export interface PreviewRequest {
  products: Array<{ product_id: string; target_duration_s: number }>;
}

// ---------------- Command responses ----------------

export interface DraftResult {
  version_id: string;
  version: number;
  state: ScriptItemState;
  spoken_text: string;
  estimated_duration_s?: number;
  violations?: GateViolation[];
}

export interface SubmitResult {
  state: "gate_running" | "gate_failed" | "reviewable";
  version_id?: string;
  violations?: GateViolation[];
}

export interface AcceptedJob {
  status: "accepted" | "existing";
  workflow_id?: string;
  job_id?: string;
  batch_id?: string;
}

export interface ApprovalResult {
  approval: ApprovalRecord;
  state: ScriptItemState;
}

export interface BatchApprovalResult {
  approvals: ApprovalRecord[];
}

// ---------------- Batch + SSE (Decision 16) ----------------

export type BatchProductStatus =
  | "queued"
  | "planning"
  | "generating"
  | "reviewable"
  | "failed"
  | "cancelled"
  | "completed";

export interface BatchProductProgress {
  product_id: string;
  status: BatchProductStatus;
  current_segment_index: number | null;
  segment_count: number | null;
  estimated_semantic_calls: number;
  attempts: number;
  failure: FailureInfo | null;
}

export type BatchStatus = "queued" | "running" | "completed" | "cancelled" | "partial_failure";

export interface GenerationBatch {
  batch_id: string;
  script_set_id: string;
  script_set_revision: number;
  requested_product_ids: string[];
  status: BatchStatus;
  revision: number;
  products: BatchProductProgress[];
  estimated_semantic_calls_total: number;
  created_at: string;
  updated_at: string;
}

export interface GenerateBatchRequest {
  products: Array<{ product_id: string; target_duration_s: number }>;
}

export type ScriptEventType =
  | "batch.snapshot"
  | "batch.progress"
  | "product.planning_started"
  | "product.plan_ready"
  | "segment.started"
  | "segment.gate_passed"
  | "segment.gate_failed"
  | "product.reviewable"
  | "product.failed"
  | "batch.completed"
  | "batch.cancelled";

export interface ScriptEvent {
  event_id: string;
  revision: number;
  type: ScriptEventType;
  script_set_id: string;
  batch_id: string;
  product_id?: string;
  segment_index?: number;
  segment_count?: number;
  failure?: FailureInfo;
  snapshot?: GenerationBatch;
  payload?: { attempts?: number; status?: BatchStatus };
}

// ---------------- Client ----------------

export function createScriptClient(deps: ApiDeps) {
  const base = () => deps.backendUrl.replace(/\/$/, "");

  function adminHeaders(json = false): Record<string, string> {
    const token = deps.adminToken();
    const headers: Record<string, string> = {};
    if (token) headers["Authorization"] = `Bearer ${token}`;
    if (json) headers["Content-Type"] = "application/json";
    return headers;
  }

  async function requestJson<T>(path: string, options: RequestInit = {}): Promise<T> {
    const response = await fetch(base() + path, options);
    const text = await response.text();
    let body: unknown = null;
    if (text) {
      try {
        body = JSON.parse(text) as T;
      } catch {
        throw new ApiError(response.status, `HTTP ${response.status}: phản hồi không phải JSON`);
      }
    }
    if (!response.ok) {
      const detail = (body as Record<string, unknown>)?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : Array.isArray(detail)
            ? (detail as Array<{ loc: string[]; msg: string }>)
                .map((i) => `${i.loc.join(".")}: ${i.msg}`)
                .join("; ")
            : `HTTP ${response.status}`;
      throw new ApiError(response.status, message, detail);
    }
    return body as T;
  }

  const esc = encodeURIComponent;

  async function createScriptSet(input: ScriptSetInput): Promise<ScriptSet> {
    return requestJson<ScriptSet>("/api/v1/script-sets", {
      method: "POST",
      headers: adminHeaders(true),
      body: JSON.stringify(input),
    });
  }

  async function getScriptSet(setId: string): Promise<ScriptSet> {
    return requestJson<ScriptSet>(`/api/v1/script-sets/${esc(setId)}`, {
      headers: adminHeaders(),
    });
  }

  async function patchScriptSet(setId: string, patch: ScriptSetPatch): Promise<ScriptSet> {
    return requestJson<ScriptSet>(`/api/v1/script-sets/${esc(setId)}`, {
      method: "PATCH",
      headers: adminHeaders(true),
      body: JSON.stringify(patch),
    });
  }

  async function putDraft(setId: string, productId: string, input: DraftInput): Promise<DraftResult> {
    return requestJson<DraftResult>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/draft`,
      { method: "PUT", headers: adminHeaders(true), body: JSON.stringify(input) },
    );
  }

  async function submit(setId: string, productId: string): Promise<SubmitResult> {
    return requestJson<SubmitResult>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/submit`,
      { method: "POST", headers: adminHeaders(true) },
    );
  }

  async function previewProduct(
    setId: string,
    productId: string,
    targetDurationS: number,
  ): Promise<ProductPreview> {
    return requestJson<ProductPreview>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/generation-preview`,
      { method: "POST", headers: adminHeaders(true), body: JSON.stringify({ target_duration_s: targetDurationS }) },
    );
  }

  async function previewBatch(setId: string, req: PreviewRequest): Promise<GenerationPreview> {
    return requestJson<GenerationPreview>(
      `/api/v1/script-sets/${esc(setId)}/generation-preview`,
      { method: "POST", headers: adminHeaders(true), body: JSON.stringify(req) },
    );
  }

  async function generateProduct(
    setId: string,
    productId: string,
    targetDurationS: number,
    key: string,
  ): Promise<AcceptedJob> {
    return requestJson<AcceptedJob>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/generate`,
      {
        method: "POST",
        headers: { ...adminHeaders(true), "Idempotency-Key": key },
        body: JSON.stringify({ target_duration_s: targetDurationS }),
      },
    );
  }

  async function regenerateSegment(
    setId: string,
    productId: string,
    segmentIndex: number,
    key: string,
  ): Promise<AcceptedJob> {
    return requestJson<AcceptedJob>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/segments/${segmentIndex}/regenerate`,
      { method: "POST", headers: { ...adminHeaders(true), "Idempotency-Key": key }, body: JSON.stringify({}) },
    );
  }

  async function fixProduct(setId: string, productId: string, key: string): Promise<AcceptedJob> {
    return requestJson<AcceptedJob>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/fix`,
      { method: "POST", headers: { ...adminHeaders(true), "Idempotency-Key": key }, body: JSON.stringify({}) },
    );
  }

  async function approveProduct(setId: string, productId: string): Promise<ApprovalResult> {
    return requestJson<ApprovalResult>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/approve`,
      { method: "POST", headers: adminHeaders(true) },
    );
  }

  async function approveBatch(setId: string, productIds: string[]): Promise<BatchApprovalResult> {
    return requestJson<BatchApprovalResult>(
      `/api/v1/script-sets/${esc(setId)}/approve-batch`,
      { method: "POST", headers: adminHeaders(true), body: JSON.stringify({ product_ids: productIds }) },
    );
  }

  async function generateBatch(setId: string, req: GenerateBatchRequest, key: string): Promise<AcceptedJob> {
    return requestJson<AcceptedJob>(
      `/api/v1/script-sets/${esc(setId)}/generate-batch`,
      {
        method: "POST",
        headers: { ...adminHeaders(true), "Idempotency-Key": key },
        body: JSON.stringify(req),
      },
    );
  }

  async function getBatch(setId: string, batchId: string): Promise<GenerationBatch> {
    return requestJson<GenerationBatch>(
      `/api/v1/script-sets/${esc(setId)}/generation-batches/${esc(batchId)}`,
      { headers: adminHeaders() },
    );
  }

  async function cancelBatch(setId: string, batchId: string): Promise<{ ok: boolean }> {
    return requestJson<{ ok: boolean }>(
      `/api/v1/script-sets/${esc(setId)}/generation-batches/${esc(batchId)}/cancel`,
      { method: "POST", headers: adminHeaders() },
    );
  }

  return {
    createScriptSet,
    getScriptSet,
    patchScriptSet,
    putDraft,
    submit,
    previewProduct,
    previewBatch,
    generateProduct,
    regenerateSegment,
    fixProduct,
    approveProduct,
    approveBatch,
    generateBatch,
    getBatch,
    cancelBatch,
  };
}

export type ScriptClient = ReturnType<typeof createScriptClient>;

// ---------------- Idempotency (Decision 12) ----------------

/** Deterministic idempotency key — identical payloads produce the same key, so
 * repeated equivalent requests refer to the existing workflow instead of
 * double-spending model calls. */
export function idempotencyKey(value: unknown): string {
  const raw = JSON.stringify(value);
  let hash = 0x811c9dc5;
  for (let i = 0; i < raw.length; i++) {
    hash ^= raw.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0).toString(16).padStart(8, "0");
}

// ---------------- SSE (Decision 16) ----------------

export function sseUrl(backend: string, setId: string, batchId: string, token: string): string {
  const url = new URL(backend);
  url.pathname = `/api/v1/script-sets/${encodeURIComponent(setId)}/generation-batches/${encodeURIComponent(batchId)}/events`;
  if (token) url.search = `token=${encodeURIComponent(token)}`;
  return url.toString();
}

export interface SseHandlers {
  onEvent: (event: ScriptEvent) => void;
  onStatus?: (message: string, tone?: string) => void;
}

/** Pure SSE frame parser: 'event:' / 'data:' / 'id:' lines. */
export function parseSseFrame(raw: string): { event: string; data: string; id?: string } | null {
  let event = "message";
  let id: string | undefined;
  const dataLines: string[] = [];
  for (const line of raw.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("id:")) id = line.slice(3).trim();
    else if (line.startsWith("data:")) dataLines.push(line.startsWith("data: ") ? line.slice(6) : line.slice(5));
  }
  if (!dataLines.length) return null;
  return { event, data: dataLines.join("\n"), id };
}

/** Deduplicating SSE feed — snapshot + revision guard so a reconnect replay
 * never re-applies events (task 13.5). */
export class SseFeed {
  private lastRevision = 0;
  private snapshotApplied = false;
  private seenIds = new Set<string>();

  constructor(private handlers: SseHandlers) {}

  push(raw: string): void {
    const frame = parseSseFrame(raw);
    if (!frame) return;
    let event: ScriptEvent | null = null;
    try {
      event = JSON.parse(frame.data) as ScriptEvent;
    } catch {
      this.handlers.onStatus?.("SSE event không phải JSON.", "danger");
      return;
    }
    this.accept(event);
  }

  accept(event: ScriptEvent): void {
    if (event.type === "batch.snapshot") {
      // Snapshot replaces prior state — replay after reconnect is idempotent.
      this.lastRevision = event.revision;
      this.snapshotApplied = true;
      this.handlers.onEvent(event);
      return;
    }
    if (!this.snapshotApplied) return; // wait for snapshot before live events
    if (event.revision <= this.lastRevision || this.seenIds.has(event.event_id)) return;
    if (this.seenIds.size > 500) this.seenIds.clear(); // ponytail: bounded-session dedup set
    this.seenIds.add(event.event_id);
    this.lastRevision = event.revision;
    this.handlers.onEvent(event);
  }

  get revision(): number {
    return this.lastRevision;
  }
}

const SSE_EVENT_TYPES: ScriptEventType[] = [
  "batch.snapshot",
  "batch.progress",
  "product.planning_started",
  "product.plan_ready",
  "segment.started",
  "segment.gate_passed",
  "segment.gate_failed",
  "product.reviewable",
  "product.failed",
  "batch.completed",
  "batch.cancelled",
];

export class ScriptEventSource {
  private source: EventSource | null = null;
  readonly feed: SseFeed;

  constructor(
    private deps: {
      backendUrl: string;
      adminToken: () => string;
      scriptSetId: string;
      batchId: string;
    } & SseHandlers,
  ) {
    this.feed = new SseFeed({ onEvent: deps.onEvent, onStatus: deps.onStatus });
  }

  connect(): void {
    this.disconnect();
    const url = sseUrl(
      this.deps.backendUrl,
      this.deps.scriptSetId,
      this.deps.batchId,
      this.deps.adminToken(),
    );
    const source = new EventSource(url);
    this.source = source;
    source.onopen = () => this.deps.onStatus?.("SSE generation đã kết nối.", "success");
    source.onerror = () => this.deps.onStatus?.("SSE generation mất kết nối — đang thử lại.", "warning");
    const push = (raw: string): void => this.feed.push(raw);
    source.onmessage = (event) => push((event as MessageEvent<string>).data);
    // Named events never reach onmessage — register per known type.
    for (const type of SSE_EVENT_TYPES) {
      source.addEventListener(type, (event) => push((event as MessageEvent<string>).data));
    }
  }

  disconnect(): void {
    if (this.source) {
      this.source.close();
      this.source = null;
    }
  }

  get connected(): boolean {
    return this.source !== null;
  }
}
