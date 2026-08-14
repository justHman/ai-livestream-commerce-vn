/** Entity Data Studio — DOM mount: renders Data Studio state into the panel
 * and binds controls to flows (tasks 9.1-9.8). No business logic here — all
 * flows live in datastudio.ts. Follows main.ts conventions: $ lookup, escaped
 * text via textContent (never innerHTML with user data), addEvent status feed,
 * busy states, localStorage draft persistence. */

import type { Api } from "./api";
import { COMMON_FIELDS } from "./datastudio";
import {
  datastudioReducer,
  emptyCommon,
  hydrateDraft,
  initialStateDataStudio,
  loadEntitiesFlow,
  loadEntityFlow,
  persistDraft,
  renderPreviewFlow,
  resetForm,
  saveFlow,
  suggestFlow,
  type Action,
  type DataStudioState,
  type KnowledgeBlockDraft,
} from "./datastudio";
import type { FactRowIn } from "./api_types";

export interface DataStudioMountDeps {
  backendUrl: () => string;
  viewerToken: () => string;
  api: Api;
  onEvent: (message: string, tone?: string) => void;
}

export function mountDataStudio(deps: DataStudioMountDeps): {
  state: DataStudioState;
  dispatch: (action: Action) => void;
} {
  const $ = (id: string): HTMLElement => document.getElementById(id) ?? document.body;

  let state: DataStudioState = {
    ...initialStateDataStudio(),
    common: emptyCommon(),
  };

  const stored = hydrateDraft();
  if (stored) {
    state = {
      ...state,
      ...stored,
      common: { ...emptyCommon(), ...(stored.common ?? {}) },
      factRows: Array.isArray(stored.factRows) ? stored.factRows : [],
      blocks: Array.isArray(stored.blocks) ? stored.blocks : [],
      entities: [],
      document: null,
      suggestions: [],
      suggestionsNote: null,
      renderText: null,
      status: "Đã khôi phục bản nháp từ trình duyệt.",
      errors: [],
      busy: {},
    };
  }

  function dispatch(action: Action): void {
    state = datastudioReducer(state, action);
    if (isDraftAction(action)) persistDraft(state);
    render(action);
  }

  function addEvent(message: string, tone?: string): void {
    deps.onEvent(message, tone);
  }

  function flowDeps() {
    return { api: deps.api, state, dispatch, onEvent: addEvent };
  }

  // ---------------- Flows bound to controls ----------------

  async function refreshEntities(): Promise<void> {
    await loadEntitiesFlow(flowDeps());
  }

  async function loadSelected(): Promise<void> {
    const select = $("dsEntitySelect") as HTMLSelectElement;
    const id = select.value;
    if (!id) return;
    await loadEntityFlow(flowDeps(), id);
  }

  function newEntity(): void {
    resetForm(flowDeps());
    ($("dsEntitySelect") as HTMLSelectElement).value = "";
  }

  async function save(): Promise<void> {
    await saveFlow(flowDeps());
  }

  async function suggest(blockIndex: number): Promise<void> {
    await suggestFlow(flowDeps(), blockIndex);
  }

  async function renderPreview(): Promise<void> {
    await renderPreviewFlow(flowDeps());
  }

  function changeEntityType(): void {
    const select = $("dsEntityType") as HTMLSelectElement;
    dispatch({ type: "DATASTUDIO_SET", value: { entityType: select.value as DataStudioState["entityType"] } });
    void refreshEntities();
  }

  // ---------------- Render ----------------

  function render(_action: Action): void {
    renderHeader();
    renderRows();
    renderBlocks();
    renderSuggestions();
    renderDocument();
    renderStatus();
  }

  function renderHeader(): void {
    ($("dsEntityType") as HTMLSelectElement).value = state.entityType;
    ($("dsEntitySelect") as HTMLSelectElement).value = state.loadedId ?? "";
    if (document.activeElement !== $("dsName")) ($("dsName") as HTMLInputElement).value = state.name;
    if (document.activeElement !== $("dsAliases")) ($("dsAliases") as HTMLInputElement).value = state.aliases;
    if (document.activeElement !== $("dsTags")) ($("dsTags") as HTMLInputElement).value = state.tags;
    if (document.activeElement !== $("dsId")) ($("dsId") as HTMLInputElement).value = state.loadedId ?? "";
    for (const field of COMMON_FIELDS) {
      const el = $(field.id) as HTMLInputElement;
      if (document.activeElement === el) continue;
      const value = state.common[field.key] ?? "";
      if (field.kind === "bool") el.checked = value === "true";
      else el.value = value;
    }
    const hasId = Boolean(state.loadedId);
    ($("dsSaveBtn") as HTMLButtonElement).disabled = !hasId || Boolean(state.busy["save"]);
    ($("dsRenderBtn") as HTMLButtonElement).disabled = !hasId || Boolean(state.busy["render-preview"]);
    if (document.activeElement !== $("dsSelectors")) ($("dsSelectors") as HTMLInputElement).value = state.selectors;
  }

  function renderRows(): void {
    const list = $("dsRowList");
    list.replaceChildren(...state.factRows.map((row, i) => rowElement(row, i)));
    ($("dsAddRowBtn") as HTMLButtonElement).disabled = Boolean(state.busy["save"]);
  }

  function rowElement(row: FactRowIn, index: number): HTMLElement {
    const div = document.createElement("div");
    div.className = "ds-row";
    div.innerHTML = `
      <input data-row-field="label" placeholder="Nhãn (vd: Dùng cho da dầu)" maxlength="200" aria-label="Nhãn dòng ${index + 1}">
      <input data-row-field="value" placeholder="Giá trị" maxlength="500" aria-label="Giá trị dòng ${index + 1}">
      <input data-row-field="unit" placeholder="Đơn vị (tùy chọn)" maxlength="64" aria-label="Đơn vị dòng ${index + 1}">
      <button type="button" class="secondary danger" data-row-remove aria-label="Xóa dòng ${index + 1}">Xóa</button>
    `;
    const inputs = div.querySelectorAll<HTMLInputElement>("[data-row-field]");
    inputs[0]!.value = row.label;
    inputs[1]!.value = row.value;
    inputs[2]!.value = row.unit ?? "";
    return div;
  }

  function renderBlocks(): void {
    const list = $("dsBlockList");
    list.replaceChildren(...state.blocks.map((block, i) => blockElement(block, i)));
    ($("dsAddBlockBtn") as HTMLButtonElement).disabled = Boolean(state.busy["save"]);
  }

  function blockElement(block: KnowledgeBlockDraft, index: number): HTMLElement {
    const div = document.createElement("div");
    div.className = "ds-block";
    div.innerHTML = `
      <div class="ds-block-head">
        <select data-block-field="kind" aria-label="Loại block ${index + 1}">
          <option value="description">Mô tả</option>
          <option value="usage">Hướng dẫn sử dụng</option>
          <option value="story">Câu chuyện</option>
          <option value="campaign">Chiến dịch</option>
          <option value="custom">Tùy chỉnh</option>
        </select>
        <input data-block-field="title" placeholder="Tiêu đề block" maxlength="200" aria-label="Tiêu đề block ${index + 1}">
        <button type="button" class="secondary danger" data-block-remove aria-label="Xóa block ${index + 1}">Xóa</button>
      </div>
      <textarea data-block-field="content" rows="4" placeholder="Dán nội dung thô vào đây..." aria-label="Nội dung block ${index + 1}"></textarea>
      <div class="ds-block-foot">
        <input data-block-field="tags" placeholder="Tags, phân cách bằng dấu phẩy" maxlength="500" aria-label="Tags block ${index + 1}">
        <button type="button" class="secondary" data-block-suggest aria-label="Gợi ý từ AI cho block ${index + 1}">Gợi ý từ AI</button>
      </div>
    `;
    const kind = div.querySelector<HTMLSelectElement>('[data-block-field="kind"]')!;
    const title = div.querySelector<HTMLInputElement>('[data-block-field="title"]')!;
    const content = div.querySelector<HTMLTextAreaElement>('[data-block-field="content"]')!;
    const tags = div.querySelector<HTMLInputElement>('[data-block-field="tags"]')!;
    kind.value = block.kind;
    title.value = block.title;
    content.value = block.content;
    tags.value = block.tags;
    const suggestBtn = div.querySelector<HTMLButtonElement>("[data-block-suggest]")!;
    suggestBtn.disabled = Boolean(state.busy[`suggest-${index}`]);
    return div;
  }

  function renderSuggestions(): void {
    const container = $("dsSuggestionList");
    if (!state.suggestions.length) {
      container.textContent = "Chưa có gợi ý — bấm 'Gợi ý từ AI' trên một block nội dung.";
      return;
    }
    container.replaceChildren(...state.suggestions.map((s, i) => suggestionElement(s, i)));
    ($("dsSuggestionNote") as HTMLElement).textContent = state.suggestionsNote ?? "";
  }

  function suggestionElement(suggestion: { key: string; label: string; value: string; unit?: string | null }, index: number): HTMLElement {
    const card = document.createElement("div");
    card.className = "ds-suggestion";
    card.innerHTML = `
      <div class="ds-suggestion-body">
        <strong></strong>
        <span class="ds-suggestion-value"></span>
        <span class="ds-suggestion-meta"></span>
      </div>
      <div class="actions">
        <button type="button" class="secondary" data-suggestion-accept aria-label="Chấp nhận gợi ý ${index + 1}">Chấp nhận</button>
        <button type="button" class="secondary danger" data-suggestion-dismiss aria-label="Bỏ qua gợi ý ${index + 1}">Bỏ qua</button>
      </div>
    `;
    card.querySelector("strong")!.textContent = suggestion.label;
    card.querySelector(".ds-suggestion-value")!.textContent = suggestion.value;
    card.querySelector(".ds-suggestion-meta")!.textContent = `${suggestion.unit ?? ""} · ${suggestion.key}`.trim();
    return card;
  }

  function renderDocument(): void {
    const container = $("dsDocumentView");
    const doc = state.document;
    if (!doc) {
      container.textContent = "Chưa có entity đã tải — bấm Lưu hoặc chọn entity để xem dạng chuẩn hóa.";
      return;
    }
    container.replaceChildren();
    const factsHeading = document.createElement("h3");
    factsHeading.textContent = `Facts (${doc.facts.length})`;
    container.appendChild(factsHeading);
    const facts = document.createElement("ul");
    facts.className = "ds-doc-list";
    for (const fact of doc.facts) {
      const li = document.createElement("li");
      li.textContent = `${fact.key} · ${fact.type} · ${String(fact.value)}${fact.unit ? ` ${fact.unit}` : ""} · ${fact.labels.join(", ")} · fresh=${fact.freshness} · rev=${fact.revision} · ${fact.updated_at}`;
      facts.appendChild(li);
    }
    container.appendChild(facts);
    const blocksHeading = document.createElement("h3");
    blocksHeading.textContent = `Knowledge blocks (${doc.knowledge_blocks.length})`;
    container.appendChild(blocksHeading);
    for (const block of doc.knowledge_blocks) {
      const details = document.createElement("details");
      details.className = "ds-block-doc";
      const summary = document.createElement("summary");
      summary.textContent = `${block.kind} · ${block.title} · rev=${block.revision} · ${block.tags.join(", ")}`;
      const content = document.createElement("pre");
      content.textContent = block.content;
      details.append(summary, content);
      container.appendChild(details);
    }
    const rawHeading = document.createElement("h3");
    rawHeading.textContent = "Raw JSON";
    container.appendChild(rawHeading);
    const raw = document.createElement("details");
    const rawSummary = document.createElement("summary");
    rawSummary.textContent = "Mở rộng/xem JSON";
    const rawPre = document.createElement("pre");
    rawPre.textContent = JSON.stringify(doc, null, 2);
    raw.append(rawSummary, rawPre);
    container.appendChild(raw);
  }

  function renderStatus(): void {
    ($("dsStatus") as HTMLElement).textContent = state.status;
    ($("dsErrors") as HTMLElement).textContent = state.errors.join("\n");
    const renderNote = $("dsRenderNote") as HTMLElement;
    const renderText = $("dsRenderText") as HTMLElement;
    if (state.renderText) {
      renderText.textContent = state.renderText;
      renderNote.textContent = "Đây chính xác là những gì Agent/LLM nhìn thấy.";
    } else {
      renderText.textContent = "";
      renderNote.textContent = "";
    }
  }

  // ---------------- Binding ----------------

  function bind(): void {
    ($("dsEntityType") as HTMLSelectElement).addEventListener("change", changeEntityType);
    ($("dsEntitySelect") as HTMLSelectElement).addEventListener("change", () => void loadSelected());
    ($("dsNewEntityBtn") as HTMLButtonElement).addEventListener("click", newEntity);
    ($("dsSaveBtn") as HTMLButtonElement).addEventListener("click", () => void save());
    ($("dsRenderBtn") as HTMLButtonElement).addEventListener("click", () => void renderPreview());
    ($("dsAddRowBtn") as HTMLButtonElement).addEventListener("click", () => dispatch({ type: "DATASTUDIO_ROW_ADD" }));
    ($("dsAddBlockBtn") as HTMLButtonElement).addEventListener("click", () => dispatch({ type: "DATASTUDIO_BLOCK_ADD" }));
    ($("dsRowList") as HTMLElement).addEventListener("change", (event) => {
      const input = event.target as HTMLInputElement;
      const rowEl = input.closest<HTMLElement>(".ds-row");
      const field = input.dataset.rowField;
      if (!rowEl || !field) return;
      const index = [...rowEl.parentElement!.children].indexOf(rowEl);
      dispatch({ type: "DATASTUDIO_ROW", index, field: field as "label" | "value" | "unit", value: input.value });
    });
    ($("dsRowList") as HTMLElement).addEventListener("click", (event) => {
      const button = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-row-remove]");
      if (!button) return;
      const rowEl = button.closest<HTMLElement>(".ds-row");
      if (!rowEl) return;
      const index = [...rowEl.parentElement!.children].indexOf(rowEl);
      dispatch({ type: "DATASTUDIO_ROW_REMOVE", index });
    });
    ($("dsBlockList") as HTMLElement).addEventListener("change", (event) => {
      const input = event.target as HTMLInputElement;
      const blockEl = input.closest<HTMLElement>(".ds-block");
      const field = input.dataset.blockField;
      if (!blockEl || !field) return;
      const index = [...blockEl.parentElement!.children].indexOf(blockEl);
      dispatch({ type: "DATASTUDIO_BLOCK", index, field: field as "kind" | "title" | "content" | "tags", value: input.value });
    });
    ($("dsBlockList") as HTMLElement).addEventListener("click", (event) => {
      const remove = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-block-remove]");
      if (remove) {
        const blockEl = remove.closest<HTMLElement>(".ds-block");
        if (!blockEl) return;
        const index = [...blockEl.parentElement!.children].indexOf(blockEl);
        dispatch({ type: "DATASTUDIO_BLOCK_REMOVE", index });
        return;
      }
      const suggestBtn = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-block-suggest]");
      if (suggestBtn) {
        const blockEl = suggestBtn.closest<HTMLElement>(".ds-block");
        if (!blockEl) return;
        const index = [...blockEl.parentElement!.children].indexOf(blockEl);
        void suggest(index);
      }
    });
    ($("dsSuggestionList") as HTMLElement).addEventListener("click", (event) => {
      const accept = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-suggestion-accept]");
      if (accept) {
        const card = accept.closest<HTMLElement>(".ds-suggestion");
        if (!card) return;
        const index = [...card.parentElement!.children].indexOf(card);
        dispatch({ type: "DATASTUDIO_SUGGESTION_ACCEPT", index });
        return;
      }
      const dismiss = (event.target as HTMLElement).closest<HTMLButtonElement>("[data-suggestion-dismiss]");
      if (dismiss) {
        const card = dismiss.closest<HTMLElement>(".ds-suggestion");
        if (!card) return;
        const index = [...card.parentElement!.children].indexOf(card);
        dispatch({ type: "DATASTUDIO_SUGGESTION_DISMISS", index });
      }
    });
    ($("dsSelectors") as HTMLInputElement).addEventListener("input", (event) => {
      dispatch({ type: "DATASTUDIO_SET", value: { selectors: (event.target as HTMLInputElement).value } });
    });
    ($("dsName") as HTMLInputElement).addEventListener("input", (event) => {
      dispatch({ type: "DATASTUDIO_SET", value: { name: (event.target as HTMLInputElement).value } });
    });
    ($("dsAliases") as HTMLInputElement).addEventListener("input", (event) => {
      dispatch({ type: "DATASTUDIO_SET", value: { aliases: (event.target as HTMLInputElement).value } });
    });
    ($("dsTags") as HTMLInputElement).addEventListener("input", (event) => {
      dispatch({ type: "DATASTUDIO_SET", value: { tags: (event.target as HTMLInputElement).value } });
    });
    ($("dsId") as HTMLInputElement).addEventListener("input", (event) => {
      const id = (event.target as HTMLInputElement).value.trim();
      dispatch({ type: "DATASTUDIO_SET", value: { loadedId: id, status: id ? "Entity mới — sẵn sàng lưu." : state.status } });
    });
    for (const field of COMMON_FIELDS) {
      const el = $(field.id) as HTMLInputElement;
      el.addEventListener(field.kind === "bool" ? "change" : "input", () => {
        const value = field.kind === "bool" ? String(el.checked) : el.value;
        dispatch({ type: "DATASTUDIO_COMMON", key: field.key, value });
      });
    }
    void refreshEntities();
  }

  bind();
  return { state, dispatch };
}

function isDraftAction(action: Action): boolean {
  return [
    "DATASTUDIO_COMMON",
    "DATASTUDIO_ROW",
    "DATASTUDIO_ROW_ADD",
    "DATASTUDIO_ROW_REMOVE",
    "DATASTUDIO_BLOCK",
    "DATASTUDIO_BLOCK_ADD",
    "DATASTUDIO_BLOCK_REMOVE",
    "DATASTUDIO_SUGGESTION_ACCEPT",
    "DATASTUDIO_SUGGESTION_DISMISS",
    "DATASTUDIO_SET",
  ].includes(action.type);
}
