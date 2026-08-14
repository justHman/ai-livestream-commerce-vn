/** Entity Data Studio — client draft state and flows for the universal entity
 * context (tasks 9.1-9.8). Domain-pure: no DOM. The mount layer binds controls
 * and events; tests exercise the flows here without a browser.
 *
 * Save is a single PUT carrying common keys, arbitrary fact_rows and knowledge
 * blocks — it never depends on AI suggestions. Suggestions only feed the draft
 * after explicit operator acceptance, and the authoritative write happens via
 * putEntity. Unknown labels pass through verbatim; the backend maps them to
 * custom.* keys, so the UI never validates keys.
 */

import type { Api } from "./api";
import type {
  EntityDocument,
  EntityType,
  FactRowIn,
  KnowledgeBlockIn,
  KnowledgeBlockKind,
  SimpleEntityUpsertReq,
  SuggestedFact,
} from "./api_types";

export const LOCAL_DRAFT_KEY = "livento-datastudio-draft-v1";
export const LOCAL_DRAFT_VERSION = 1;

/** Canonical registry keys the simple form maps to (backend `common` limit). */
export const COMMON_FIELDS: ReadonlyArray<{
  key: string;
  id: string;
  label: string;
  kind: "text" | "number" | "bool";
}> = [
  { key: "identity.brand", id: "dsBrand", label: "Thương hiệu", kind: "text" },
  { key: "identity.sku", id: "dsSku", label: "Mã SKU", kind: "text" },
  { key: "commerce.price.current", id: "dsPrice", label: "Giá hiện tại (VND)", kind: "number" },
  { key: "commerce.price.original", id: "dsOriginalPrice", label: "Giá gốc (VND)", kind: "number" },
  { key: "commerce.promotion", id: "dsPromotion", label: "Khuyến mãi", kind: "text" },
  { key: "commerce.shipping", id: "dsShipping", label: "Vận chuyển", kind: "text" },
  { key: "commerce.warranty", id: "dsWarranty", label: "Bảo hành", kind: "text" },
  { key: "commerce.stock.available", id: "dsInStock", label: "Còn hàng", kind: "bool" },
  { key: "commerce.stock.quantity", id: "dsStockQuantity", label: "Số lượng tồn", kind: "number" },
];

export interface KnowledgeBlockDraft {
  id: string;
  kind: KnowledgeBlockKind;
  title: string;
  content: string;
  tags: string;
}

export interface SuggestionDraft {
  key: string;
  label: string;
  value: string;
  unit?: string | null;
  type: "int" | "float" | "str" | "bool";
}

export interface DataStudioState {
  entityType: EntityType;
  entities: EntityDocument[];
  loadedId: string | null;
  document: EntityDocument | null;
  name: string;
  aliases: string;
  tags: string;
  common: Record<string, string>;
  factRows: FactRowIn[];
  blocks: KnowledgeBlockDraft[];
  suggestions: SuggestionDraft[];
  suggestionsNote: string | null;
  selectors: string;
  renderText: string | null;
  status: string;
  errors: string[];
  busy: Record<string, boolean>;
}

export function initialStateDataStudio(): DataStudioState {
  return {
    entityType: "product",
    entities: [],
    loadedId: null,
    document: null,
    name: "",
    aliases: "",
    tags: "",
    common: {},
    factRows: [],
    blocks: [],
    suggestions: [],
    suggestionsNote: null,
    selectors: "",
    renderText: null,
    status: "Chọn hoặc tạo entity.",
    errors: [],
    busy: {},
  };
}

export type Action =
  | { type: "DATASTUDIO_SET"; value: Partial<DataStudioState> }
  | { type: "DATASTUDIO_COMMON"; key: string; value: string }
  | { type: "DATASTUDIO_ROW"; index: number; field: "label" | "value" | "unit"; value: string }
  | { type: "DATASTUDIO_ROW_ADD" }
  | { type: "DATASTUDIO_ROW_REMOVE"; index: number }
  | { type: "DATASTUDIO_BLOCK"; index: number; field: "kind" | "title" | "content" | "tags"; value: string }
  | { type: "DATASTUDIO_BLOCK_ADD" }
  | { type: "DATASTUDIO_BLOCK_REMOVE"; index: number }
  | { type: "DATASTUDIO_SUGGEST"; index: number; suggestions: SuggestedFact[]; note?: string }
  | { type: "DATASTUDIO_SUGGESTION_ACCEPT"; index: number }
  | { type: "DATASTUDIO_SUGGESTION_DISMISS"; index: number }
  | { type: "DATASTUDIO_BUSY"; key: string; busy: boolean }
  | { type: "DATASTUDIO_STATUS"; status: string; errors?: string[] };

let blockSeq = 0;

function newBlockId(): string {
  blockSeq += 1;
  return `blk-${Date.now().toString(36)}-${blockSeq}`;
}

function emptyRow(): FactRowIn {
  return { label: "", value: "" };
}

function emptyBlock(): KnowledgeBlockDraft {
  return { id: newBlockId(), kind: "description", title: "", content: "", tags: "" };
}

export function datastudioReducer(state: DataStudioState, action: Action): DataStudioState {
  switch (action.type) {
    case "DATASTUDIO_SET":
      return { ...state, ...action.value };
    case "DATASTUDIO_COMMON":
      return { ...state, common: { ...state.common, [action.key]: action.value } };
    case "DATASTUDIO_ROW":
      return {
        ...state,
        factRows: state.factRows.map((row, i) => (i === action.index ? { ...row, [action.field]: action.value } : row)),
      };
    case "DATASTUDIO_ROW_ADD":
      return { ...state, factRows: [...state.factRows, emptyRow()] };
    case "DATASTUDIO_ROW_REMOVE":
      return { ...state, factRows: state.factRows.filter((_, i) => i !== action.index) };
    case "DATASTUDIO_BLOCK":
      return {
        ...state,
        blocks: state.blocks.map((block, i) => (i === action.index ? { ...block, [action.field]: action.value } : block)),
      };
    case "DATASTUDIO_BLOCK_ADD":
      return { ...state, blocks: [...state.blocks, emptyBlock()] };
    case "DATASTUDIO_BLOCK_REMOVE":
      return { ...state, blocks: state.blocks.filter((_, i) => i !== action.index) };
    case "DATASTUDIO_SUGGEST":
      return {
        ...state,
        suggestions: action.suggestions,
        suggestionsNote: action.note ?? null,
        status: action.suggestions.length
          ? `AI gợi ý ${action.suggestions.length} fact cho block ${action.index + 1}.`
          : "AI không tìm thấy gợi ý nào.",
        errors: [],
      };
    case "DATASTUDIO_SUGGESTION_ACCEPT": {
      const suggestion = state.suggestions[action.index];
      if (!suggestion) return state;
      const rows = [...state.factRows, { label: suggestion.label, value: suggestion.value, ...(suggestion.unit ? { unit: suggestion.unit } : {}) }];
      return {
        ...state,
        factRows: rows,
        suggestions: state.suggestions.filter((_, i) => i !== action.index),
        status: `Đã thêm "${suggestion.label}" vào danh sách — lưu form để ghi nhận.`,
        errors: [],
      };
    }
    case "DATASTUDIO_SUGGESTION_DISMISS":
      return {
        ...state,
        suggestions: state.suggestions.filter((_, i) => i !== action.index),
        errors: [],
      };
    case "DATASTUDIO_BUSY":
      return { ...state, busy: { ...state.busy, [action.key]: action.busy } };
    case "DATASTUDIO_STATUS":
      return { ...state, status: action.status, errors: action.errors ?? state.errors };
    default:
      return state;
  }
}

export function parseTags(text: string): string[] {
  return text.split(",").map((s) => s.trim()).filter(Boolean);
}

export function draftJson(state: DataStudioState): string {
  return JSON.stringify({
    version: LOCAL_DRAFT_VERSION,
    draft: {
      entityType: state.entityType,
      loadedId: state.loadedId,
      name: state.name,
      aliases: state.aliases,
      tags: state.tags,
      common: state.common,
      factRows: state.factRows,
      blocks: state.blocks,
      selectors: state.selectors,
    },
  });
}

export function persistDraft(state: DataStudioState): void {
  try {
    localStorage.setItem(LOCAL_DRAFT_KEY, draftJson(state));
  } catch {
    /* localStorage unavailable (private mode) — draft persistence is best-effort */
  }
}

export function hydrateDraft(): Partial<DataStudioState> | null {
  try {
    const raw = localStorage.getItem(LOCAL_DRAFT_KEY);
    if (!raw) return null;
    const stored = JSON.parse(raw) as { version?: number; draft?: Partial<DataStudioState> };
    if (stored?.version !== LOCAL_DRAFT_VERSION || !stored.draft) return null;
    return stored.draft;
  } catch {
    return null;
  }
}

export function clearDraft(): void {
  try {
    localStorage.removeItem(LOCAL_DRAFT_KEY);
  } catch {
    /* best-effort */
  }
}

// ---------------- Draft -> request conversion (tasks 9.1-9.4) ----------------

export function emptyCommon(): Record<string, string> {
  return {
    "identity.brand": "",
    "identity.sku": "",
    "commerce.price.current": "",
    "commerce.price.original": "",
    "commerce.promotion": "",
    "commerce.shipping": "",
    "commerce.warranty": "",
    "commerce.stock.available": "",
    "commerce.stock.quantity": "",
  };
}

export function commonFromDocument(doc: EntityDocument): Record<string, string> {
  const common = emptyCommon();
  for (const key of Object.keys(common)) {
    const fact = doc.facts.find((f) => f.key === key);
    if (!fact) continue;
    if (fact.type === "bool") common[key] = String(fact.value);
    else if (fact.value !== null && fact.value !== undefined) common[key] = String(fact.value);
  }
  return common;
}

export function rowsFromDocument(doc: EntityDocument): FactRowIn[] {
  const commonKeys = new Set(Object.keys(emptyCommon()));
  return doc.facts
    .filter((f) => !commonKeys.has(f.key))
    .map((f) => ({
      label: f.labels[0] ?? f.key,
      value: String(f.value),
      ...(f.unit ? { unit: f.unit } : {}),
    }));
}

export function blocksFromDocument(doc: EntityDocument): KnowledgeBlockDraft[] {
  return doc.knowledge_blocks.map((b) => ({ id: b.id, kind: b.kind, title: b.title, content: b.content, tags: b.tags.join(", ") }));
}

/** Strip empty values: "" and "false" (unchecked bool) are omitted so the
 * backend sees only keys the operator actually filled. */
export function cleanCommon(common: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(common)) {
    if (value !== "" && value !== "false") out[key] = value;
  }
  return out;
}

export function toUpsertRequest(state: DataStudioState): SimpleEntityUpsertReq {
  return {
    id: state.loadedId ?? "",
    entity_type: state.entityType,
    name: state.name.trim(),
    aliases: parseTags(state.aliases),
    tags: parseTags(state.tags),
    common: cleanCommon(state.common),
    fact_rows: state.factRows.filter((r) => r.label.trim() || r.value.trim()),
    knowledge_blocks: state.blocks
      .filter((b) => b.content.trim())
      .map((b): KnowledgeBlockIn => ({ kind: b.kind, title: b.title.trim(), content: b.content, tags: parseTags(b.tags) })),
  };
}

// ---------------- Flows (testable, no DOM) ----------------

export interface FlowDeps {
  api: Api;
  state: DataStudioState;
  dispatch: (action: Action) => void;
  onEvent?: (message: string, tone?: string) => void;
}

function errorMessage(error: unknown): string {
  if (error instanceof Error) return error.message;
  return "Lỗi không xác định";
}

function setBusy(deps: FlowDeps, key: string, busy: boolean): void {
  deps.dispatch({ type: "DATASTUDIO_BUSY", key, busy });
}

/** Reset the draft to a fresh empty form. */
export function resetForm(deps: FlowDeps): void {
  deps.dispatch({
    type: "DATASTUDIO_SET",
    value: {
      loadedId: null,
      document: null,
      name: "",
      aliases: "",
      tags: "",
      common: emptyCommon(),
      factRows: [],
      blocks: [],
      suggestions: [],
      suggestionsNote: null,
      selectors: "",
      renderText: null,
      status: "Tạo entity mới.",
      errors: [],
    },
  });
  clearDraft();
}

export async function loadEntitiesFlow(deps: FlowDeps): Promise<void> {
  setBusy(deps, "load-entities", true);
  try {
    const body = await deps.api.listEntities(deps.state.entityType);
    const entities = body.entities ?? [];
    deps.dispatch({ type: "DATASTUDIO_SET", value: { entities, status: `Đã tải ${entities.length} entity (${deps.state.entityType}).`, errors: [] } });
  } catch (error) {
    deps.dispatch({ type: "DATASTUDIO_STATUS", status: `Tải danh sách entity thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
    deps.onEvent?.(`Tải danh sách entity thất bại: ${errorMessage(error)}`, "danger");
  } finally {
    setBusy(deps, "load-entities", false);
  }
}

/** Load one entity into the form: common keys from facts, remaining facts as
 * fact_rows by label, knowledge blocks as-is. */
export async function loadEntityFlow(deps: FlowDeps, id: string): Promise<void> {
  setBusy(deps, "load-entity", true);
  try {
    const doc = await deps.api.getEntity(id);
    deps.dispatch({
      type: "DATASTUDIO_SET",
      value: {
        loadedId: doc.id,
        document: doc,
        name: doc.name,
        aliases: doc.aliases.join(", "),
        tags: doc.tags.join(", "),
        common: commonFromDocument(doc),
        factRows: rowsFromDocument(doc),
        blocks: blocksFromDocument(doc),
        suggestions: [],
        suggestionsNote: null,
        selectors: "",
        renderText: null,
        status: `Đã tải ${doc.name} (revision ${doc.revision}).`,
        errors: [],
      },
    });
  } catch (error) {
    deps.dispatch({ type: "DATASTUDIO_STATUS", status: `Tải entity ${id} thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
    deps.onEvent?.(`Tải entity ${id} thất bại: ${errorMessage(error)}`, "danger");
  } finally {
    setBusy(deps, "load-entity", false);
  }
}

/** Save the form. SAVE path only — never calls suggestions. On 409
 * revision_conflict the server state wins: reload the entity and warn. */
export async function saveFlow(deps: FlowDeps): Promise<boolean> {
  const { state, dispatch } = deps;
  const id = state.loadedId;
  if (!id) {
    dispatch({ type: "DATASTUDIO_STATUS", status: "Nhập ID để lưu entity.", errors: ["id: cần ID."] });
    return false;
  }
  if (!state.name.trim()) {
    dispatch({ type: "DATASTUDIO_STATUS", status: "Nhập tên entity.", errors: ["name: cần tên."] });
    return false;
  }
  setBusy(deps, "save", true);
  try {
    const doc = await deps.api.putEntity(id, toUpsertRequest(state));
    dispatch({
      type: "DATASTUDIO_SET",
      value: {
        document: doc,
        name: doc.name,
        aliases: doc.aliases.join(", "),
        tags: doc.tags.join(", "),
        common: commonFromDocument(doc),
        factRows: rowsFromDocument(doc),
        blocks: blocksFromDocument(doc),
        suggestions: [],
        suggestionsNote: null,
        renderText: null,
        status: `Đã lưu ${doc.name} (revision ${doc.revision}).`,
        errors: [],
      },
    });
    deps.onEvent?.(`Đã lưu ${doc.name} (revision ${doc.revision}).`, "success");
    clearDraft();
    await loadEntitiesFlow(deps);
    return true;
  } catch (error) {
    const isConflict = error instanceof Error && error.message.includes("409");
    dispatch({ type: "DATASTUDIO_STATUS", status: `Lưu thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
    deps.onEvent?.(
      isConflict ? `Xung đột revision — đã tải lại phiên bản mới nhất. ${errorMessage(error)}` : `Lưu entity thất bại: ${errorMessage(error)}`,
      isConflict ? "warning" : "danger",
    );
    if (isConflict) await loadEntityFlow(deps, id);
    return false;
  } finally {
    setBusy(deps, "save", false);
  }
}

/** Optional AI extraction for one knowledge block. Always independent from
 * save; failure only warns, never blocks saving. */
export async function suggestFlow(deps: FlowDeps, blockIndex: number): Promise<void> {
  const block = deps.state.blocks[blockIndex];
  if (!block) return;
  if (!block.content.trim()) {
    deps.dispatch({ type: "DATASTUDIO_STATUS", status: "Block chưa có nội dung để gợi ý.", errors: ["content: block trống."] });
    return;
  }
  setBusy(deps, `suggest-${blockIndex}`, true);
  try {
    const result = await deps.api.suggestFacts({
      entity_type: deps.state.entityType,
      text: block.content,
      block_kind: block.kind,
      block_title: block.title,
    });
    deps.dispatch({ type: "DATASTUDIO_SUGGEST", index: blockIndex, suggestions: result.suggestions ?? [], note: result.note });
  } catch (error) {
    deps.dispatch({ type: "DATASTUDIO_STATUS", status: `AI gợi ý thất bại — vẫn có thể lưu block gốc. ${errorMessage(error)}`, errors: [errorMessage(error)] });
    deps.onEvent?.(`AI gợi ý thất bại (việc lưu vẫn hoạt động): ${errorMessage(error)}`, "warning");
  } finally {
    setBusy(deps, `suggest-${blockIndex}`, false);
  }
}

/** Exact evidence/context rendering preview — what the Agent/LLM sees. */
export async function renderPreviewFlow(deps: FlowDeps): Promise<void> {
  const { state, dispatch } = deps;
  const id = state.loadedId;
  if (!id) {
    dispatch({ type: "DATASTUDIO_STATUS", status: "Chọn hoặc tạo entity trước khi xem render.", errors: ["id: thiếu."] });
    return;
  }
  setBusy(deps, "render-preview", true);
  try {
    const result = await deps.api.renderPreview(id, parseTags(state.selectors), 400);
    dispatch({ type: "DATASTUDIO_SET", value: { renderText: result.rendered, status: `Render preview cho ${result.selectors.join(", ") || "toàn bộ"}.`, errors: [] } });
  } catch (error) {
    dispatch({ type: "DATASTUDIO_STATUS", status: `Render preview thất bại: ${errorMessage(error)}`, errors: [errorMessage(error)] });
    deps.onEvent?.(`Render preview thất bại: ${errorMessage(error)}`, "danger");
  } finally {
    setBusy(deps, "render-preview", false);
  }
}
