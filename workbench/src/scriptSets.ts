/** Script authoring — canonical types + REST/SSE client for /api/v1/script-sets.
 *
 * Payloads mirror design Decision 16 (REST/JSON + SSE) and Decision 11
 * (generation preview / semantic-call estimate). Transport mirrors api.ts
 * conventions (ApiError normalization); no UI state, no DOM references.
 */

import { ApiError, type ApiDeps } from "./api";
export type { ApiDeps };

// ---------------- Domain types (task 2.1, Decision 16) ----------------

export type TransitionPolicy = "ORDER_AWARE" | "ORDER_AGNOSTIC";

export interface LiveSessionBrief {
  title: string;
  host_name?: string;
  shop_name?: string;
  note?: string;
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
  /** Preserved gate outcome when the wire carried one; "DRAFT" otherwise. */
  gate_status?: "DRAFT" | "GATE_FAILED" | "GATE_PASSED";
  violations?: GateViolation[];
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
  name: string;
  revision: number;
  transition_policy: TransitionPolicy;
  brief: LiveSessionBrief;
  products: ScriptItem[];
  created_at: string;
  updated_at: string;
}

// ---------------- Backend wire DTO (real _set_wire contract) ----------------

/** Version entry as emitted by ``ScriptAuthoringServiceImpl._version_wire``. */
export interface BackendScriptVersionWire {
  id: string;
  version: number;
  source: string;
  display_text: string;
  spoken_text: string;
  gate_result: string | null;
  created_at: string;
}

/** Gate outcome as emitted by ``ScriptAuthoringServiceImpl._gate_wire``. */
export interface GateOutcome {
  state: "passed" | "gate_failed";
  violations: GateViolation[];
}

/** Per-product entry in the real backend ScriptSet response. */
export interface BackendScriptSetItem {
  state: ScriptItemState;
  current_version_id: string | null;
  approved_version_id: string | null;
  current_version: BackendScriptVersionWire | null;
  gate: GateOutcome | null;
}

/** Exact response shape returned by ``ScriptAuthoringServiceImpl._set_wire``
 * for GET/POST/PATCH ``/api/v1/script-sets`` — an ``items`` map keyed by
 * product_id, NOT the richer local ``ScriptSet`` view model. */
export interface BackendScriptSetResponse {
  id: string;
  name: string;
  transition_policy: TransitionPolicy;
  product_ids: string[];
  revision: number;
  items: Record<string, BackendScriptSetItem>;
}

export interface ScriptSetInput {
  name: string;
  transition_policy?: TransitionPolicy;
  product_ids: string[];
  brief?: LiveSessionBrief;
}

export interface ScriptSetPatch {
  transition_policy?: TransitionPolicy;
  brief?: LiveSessionBrief;
  product_ids?: string[];
}

/** Expand a real-backend ScriptSet wire response into the local ``ScriptSet``
 * view model the authoring flows/UI consume. Per-product identity (state,
 * current/approved version ids, gate outcome) is preserved from the wire;
 * plan/segments/history stay defaulted because the wire does not carry them. */
export function mapScriptSetResponse(wire: BackendScriptSetResponse): ScriptSet {
  const productIds = wire.product_ids.length ? wire.product_ids : Object.keys(wire.items);
  return {
    id: wire.id,
    name: wire.name,
    revision: wire.revision,
    transition_policy: wire.transition_policy,
    brief: { title: wire.name, host_name: "", shop_name: "", note: "" },
    products: productIds.map((productId) => mapScriptItem(productId, wire.items[productId])),
    created_at: "",
    updated_at: "",
  };
}

function mapScriptItem(productId: string, item: BackendScriptSetItem | undefined): ScriptItem {
  const mapped = emptyScriptItem(productId, item?.state ?? "EMPTY");
  mapped.approved_version_id = item?.approved_version_id ?? null;
  const cv = item?.current_version;
  if (cv) {
    const violations = item?.gate?.violations ?? [];
    mapped.current_version = {
      version_id: cv.id,
      version: cv.version,
      source: cv.source as ScriptSource,
      state: item?.state ?? "DRAFT",
      display_text: cv.display_text,
      spoken_text: cv.spoken_text,
      estimated_duration_s: 0,
      gate_violations: violations,
      gate_status: item?.gate ? (item.gate.state === "passed" ? "GATE_PASSED" : "GATE_FAILED") : "DRAFT",
      violations,
      fingerprint: null,
      created_at: cv.created_at,
    };
  }
  return mapped;
}

function emptyScriptItem(productId: string, state: ScriptItemState): ScriptItem {
  return {
    product_id: productId,
    product_name: productId,
    state,
    source: null,
    plan: null,
    segments: [],
    versions: [],
    current_version: null,
    approvals: [],
    approved_version_id: null,
    approved_revision: null,
    failure: null,
    updated_at: "",
  };
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
  maximum_semantic_calls: number;
}

export interface GenerationPreview {
  products: ProductPreview[];
  estimated_semantic_calls_total: number;
}

export interface PreviewRequest {
  products: Array<{ product_id: string; target_duration_s: number }>;
}

// ---------------- Command responses (real backend envelopes) ----------------

/** PUT .../draft — backend returns only the resulting item state. */
export interface DraftResult {
  ok: boolean;
  product_id: string;
  state: ScriptItemState;
}

/** POST .../submit — gate outcome rides in ``gate``. */
export interface SubmitResult {
  ok: boolean;
  product_id: string;
  state: ScriptItemState;
  gate: GateOutcome | null;
}

/** Single-product async job acceptance (generate / regenerate / fix): either a
 * queued workflow or an idempotent replay of the existing workflow. */
export interface AcceptedJob {
  workflow_id: string;
  product_id?: string;
  segment_index?: number;
  status?: "queued";
  idempotent?: boolean;
}

export interface ApprovalInfo {
  version_id: string;
  actor: string;
  approved_at: string;
}

/** POST .../approve — per-product approval envelope. */
export interface ApprovalResult {
  ok: boolean;
  product_id: string;
  state: ScriptItemState;
  approval: ApprovalInfo;
}

/** POST .../approve-batch — approvals keyed by product_id, NOT an array. */
export interface BatchApprovalResult {
  ok: boolean;
  approvals: Record<string, ApprovalResult>;
}

// ---------------- Batch + SSE (Decision 16) ----------------

/** Batch status vocabulary from GET .../generation-batches/{batch_id}. */
export type BatchStatus =
  | "queued"
  | "running"
  | "completed"
  | "partial_completed"
  | "failed"
  | "cancelled"
  | "cancelling";

export interface GenerationBatch {
  batch_id: string;
  status: BatchStatus;
  product_ids: string[];
}

/** Real wire shape of GET .../generation-batches/{batch_id}: the backend emits
 * exactly ``{batch_id, status, product_ids}``. */
export interface BatchSnapshot {
  batch_id: string;
  status: string;
  product_ids: string[];
}

/** POST .../generate-batch acceptance: new queue run or idempotent replay. */
export interface BatchGenerationResult {
  batch_id: string;
  workflow_summary: WorkflowSummary;
  status: BatchStatus;
  idempotent?: boolean;
}

export interface WorkflowSummary {
  products: Array<{ product_id: string; estimated_semantic_calls: number }>;
  estimated_semantic_calls_total: number;
}

export interface GenerateBatchRequest {
  product_ids: string[];
  target_duration_s: number;
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

  function viewerHeaders(json = false): Record<string, string> {
    const token = deps.viewerToken();
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
    const wire = await requestJson<BackendScriptSetResponse>("/api/v1/script-sets", {
      method: "POST",
      headers: viewerHeaders(true),
      body: JSON.stringify(input),
    });
    return mapScriptSetResponse(wire);
  }

  async function getScriptSet(setId: string): Promise<ScriptSet> {
    const wire = await requestJson<BackendScriptSetResponse>(`/api/v1/script-sets/${esc(setId)}`, {
      headers: viewerHeaders(),
    });
    return mapScriptSetResponse(wire);
  }

  async function patchScriptSet(setId: string, patch: ScriptSetPatch): Promise<ScriptSet> {
    const wire = await requestJson<BackendScriptSetResponse>(`/api/v1/script-sets/${esc(setId)}`, {
      method: "PATCH",
      headers: viewerHeaders(true),
      body: JSON.stringify(patch),
    });
    return mapScriptSetResponse(wire);
  }

  async function putDraft(setId: string, productId: string, input: DraftInput): Promise<DraftResult> {
    return requestJson<DraftResult>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/draft`,
      { method: "PUT", headers: viewerHeaders(true), body: JSON.stringify(input) },
    );
  }

  async function submit(setId: string, productId: string): Promise<SubmitResult> {
    return requestJson<SubmitResult>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/submit`,
      { method: "POST", headers: viewerHeaders(true) },
    );
  }

  async function previewProduct(
    setId: string,
    productId: string,
    targetDurationS: number,
  ): Promise<ProductPreview> {
    return requestJson<ProductPreview>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/generation-preview`,
      { method: "POST", headers: viewerHeaders(true), body: JSON.stringify({ product_id: productId, target_duration_s: targetDurationS }) },
    );
  }

  async function previewBatch(setId: string, req: PreviewRequest): Promise<GenerationPreview> {
    // Backend exposes only the per-product generation-preview route; a batch
    // preview is the aggregate of one per-product preview per selected product.
    const products = await Promise.all(
      req.products.map((p) => previewProduct(setId, p.product_id, p.target_duration_s)),
    );
    return {
      products,
      estimated_semantic_calls_total: products.reduce((sum, p) => sum + p.estimated_semantic_calls, 0),
    };
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
        headers: { ...viewerHeaders(true), "Idempotency-Key": key },
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
      { method: "POST", headers: { ...viewerHeaders(true), "Idempotency-Key": key }, body: JSON.stringify({}) },
    );
  }

  async function fixProduct(setId: string, productId: string, key: string): Promise<AcceptedJob> {
    return requestJson<AcceptedJob>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/fix`,
      { method: "POST", headers: { ...viewerHeaders(true), "Idempotency-Key": key }, body: JSON.stringify({}) },
    );
  }

  async function approveProduct(setId: string, productId: string, versionId: string, actor: string): Promise<ApprovalResult> {
    return requestJson<ApprovalResult>(
      `/api/v1/script-sets/${esc(setId)}/products/${esc(productId)}/approve`,
      { method: "POST", headers: viewerHeaders(true), body: JSON.stringify({ version_id: versionId, actor }) },
    );
  }

  async function approveBatch(setId: string, productIds: string[], versionIds: Record<string, string>, actor: string): Promise<BatchApprovalResult> {
    return requestJson<BatchApprovalResult>(
      `/api/v1/script-sets/${esc(setId)}/approve-batch`,
      { method: "POST", headers: viewerHeaders(true), body: JSON.stringify({ product_ids: productIds, version_ids: versionIds, actor }) },
    );
  }

  async function generateBatch(setId: string, req: GenerateBatchRequest, key: string): Promise<BatchGenerationResult> {
    return requestJson<BatchGenerationResult>(
      `/api/v1/script-sets/${esc(setId)}/generate-batch`,
      {
        method: "POST",
        headers: { ...viewerHeaders(true), "Idempotency-Key": key },
        body: JSON.stringify(req),
      },
    );
  }

  async function getBatch(setId: string, batchId: string): Promise<BatchSnapshot> {
    return requestJson<BatchSnapshot>(
      `/api/v1/script-sets/${esc(setId)}/generation-batches/${esc(batchId)}`,
      { headers: viewerHeaders() },
    );
  }

  async function cancelBatch(setId: string, batchId: string): Promise<{ batch_id: string; status: BatchStatus }> {
    return requestJson<{ batch_id: string; status: BatchStatus }>(
      `/api/v1/script-sets/${esc(setId)}/generation-batches/${esc(batchId)}/cancel`,
      { method: "POST", headers: viewerHeaders() },
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
      viewerToken: () => string;
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
      this.deps.viewerToken(),
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
