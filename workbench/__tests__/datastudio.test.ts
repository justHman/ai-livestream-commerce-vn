/** Entity Data Studio — draft → upsert conversion, suggestion acceptance
 * flow, entity load mapping, and render-preview response display (9.1-9.8).
 * Pure logic only: no DOM, no fetch. */

import { describe, expect, it, vi } from "vitest";

import {
  cleanCommon,
  commonFromDocument,
  datastudioReducer,
  initialStateDataStudio,
  loadEntityFlow,
  renderPreviewFlow,
  rowsFromDocument,
  saveFlow,
  suggestFlow,
  toUpsertRequest,
  type DataStudioState,
} from "../src/datastudio";
import type { Api } from "../src/api";
import type { EntityDocument } from "../src/api_types";

function baseState(): DataStudioState {
  return {
    ...initialStateDataStudio(),
    loadedId: "product-abc",
    name: "Kem chống nắng",
    aliases: "Kem chống nắng 50+, Sunscreen",
    tags: "beauty, chống nắng",
    common: {
      "identity.brand": "La Roche-Posay",
      "identity.sku": "LRP-SPF50",
      "commerce.price.current": "329000",
      "commerce.price.original": "399000",
      "commerce.promotion": "Giảm 50k khi mua 2",
      "commerce.shipping": "Freeship toàn quốc",
      "commerce.warranty": "Đổi trả 30 ngày",
      "commerce.stock.available": "",
      "commerce.stock.quantity": "120",
    },
    factRows: [
      { label: "Dùng cho da dầu", value: "Có" },
      { label: "Kết cấu", value: "Gel mỏng", unit: "" },
    ],
    blocks: [
      { id: "blk-1", kind: "usage", title: "Hướng dẫn dùng", content: "Thoa đều trước khi ra nắng 20 phút.", tags: "cách dùng, spf" },
      { id: "blk-2", kind: "description", title: "", content: "Mô tả chung.", tags: "" },
      { id: "blk-3", kind: "custom", title: "", content: "   ", tags: "" },
    ],
    suggestions: [],
    suggestionsNote: null,
    selectors: "",
    renderText: null,
    status: "",
    errors: [],
    busy: {},
  };
}

function sampleDocument(): EntityDocument {
  return {
    id: "product-abc",
    entity_type: "product",
    revision: 3,
    name: "Kem chống nắng",
    aliases: ["Sunscreen"],
    tags: ["beauty"],
    facts: [
      { key: "identity.brand", type: "str", value: "La Roche-Posay", labels: ["Thương hiệu"], revision: 1, freshness: "stable", updated_at: "2026-08-14T00:00:00Z" },
      { key: "commerce.price.current", type: "int", value: 329000, unit: "VND", labels: ["Giá hiện tại"], revision: 2, freshness: "volatile", updated_at: "2026-08-14T00:00:00Z" },
      { key: "custom.cho-da-dau", type: "str", value: "Có", labels: ["Dùng cho da dầu"], revision: 1, freshness: "stable", updated_at: "2026-08-14T00:00:00Z" },
    ],
    knowledge_blocks: [
      { id: "blk-1", kind: "usage", title: "Hướng dẫn dùng", content: "Thoa đều trước khi ra nắng 20 phút.", tags: ["cách dùng"], revision: 1 },
    ],
    relations: [],
  };
}

function mockApi(overrides: Partial<Api> = {}): Api {
  const api = {} as Api;
  return { ...api, ...overrides } as Api;
}

function stateWithLoaded(): DataStudioState {
  return { ...baseState(), document: sampleDocument() };
}

describe("draft → SimpleEntityUpsertReq conversion (9.1-9.4)", () => {
  it("maps form common keys and keeps rows/blocks verbatim", () => {
    const req = toUpsertRequest(baseState());
    expect(req.id).toBe("product-abc");
    expect(req.entity_type).toBe("product");
    expect(req.name).toBe("Kem chống nắng");
    expect(req.aliases).toEqual(["Kem chống nắng 50+", "Sunscreen"]);
    expect(req.tags).toEqual(["beauty", "chống nắng"]);
    expect(req.common["commerce.price.current"]).toBe("329000");
    expect(req.common["identity.brand"]).toBe("La Roche-Posay");
    expect(req.fact_rows).toEqual([
      { label: "Dùng cho da dầu", value: "Có" },
      { label: "Kết cấu", value: "Gel mỏng", unit: "" },
    ]);
    expect(req.knowledge_blocks).toEqual([
      { kind: "usage", title: "Hướng dẫn dùng", content: "Thoa đều trước khi ra nắng 20 phút.", tags: ["cách dùng", "spf"] },
      { kind: "description", title: "", content: "Mô tả chung.", tags: [] },
    ]);
  });

  it("omits empty and unchecked-bool common values so only filled keys are sent", () => {
    const req = toUpsertRequest(baseState());
    expect(req.common["commerce.stock.available"]).toBeUndefined();
    expect(req.common["commerce.shipping"]).toBe("Freeship toàn quốc");
    expect(cleanCommon({ "commerce.stock.available": "false", "commerce.promotion": "" })).toEqual({});
  });

  it("drops empty rows and blank knowledge blocks", () => {
    const state: DataStudioState = {
      ...baseState(),
      factRows: [{ label: "", value: "" }, { label: "A", value: "B" }],
      blocks: [{ id: "b1", kind: "custom", title: "", content: "", tags: "" }],
    };
    const req = toUpsertRequest(state);
    expect(req.fact_rows).toEqual([{ label: "A", value: "B" }]);
    expect(req.knowledge_blocks).toEqual([]);
  });

  it("passes unknown labels through verbatim without key knowledge", () => {
    const state: DataStudioState = {
      ...baseState(),
      factRows: [{ label: "Dùng cho da dầu", value: "Có" }],
    };
    const req = toUpsertRequest(state);
    expect(req.fact_rows[0]).toEqual({ label: "Dùng cho da dầu", value: "Có" });
  });
});

describe("entity load mapping (task 9.7)", () => {
  it("fills common fields from facts by key and other facts as rows by label", () => {
    const common = commonFromDocument(sampleDocument());
    expect(common["identity.brand"]).toBe("La Roche-Posay");
    expect(common["commerce.price.current"]).toBe("329000");
    expect(common["identity.sku"]).toBe("");
    const rows = rowsFromDocument(sampleDocument());
    expect(rows).toEqual([{ label: "Dùng cho da dầu", value: "Có" }]);
  });

  it("loadEntityFlow populates the form from the fetched document", async () => {
    let dispatched: unknown[] = [];
    const flow = {
      api: mockApi({ getEntity: vi.fn().mockResolvedValue(sampleDocument()) }),
      state: baseState(),
      dispatch: (a: unknown) => {
        dispatched.push(a);
      },
    };
    await loadEntityFlow(flow as never, "product-abc");
    const set = dispatched.find((a) => (a as { type: string }).type === "DATASTUDIO_SET") as { value: Partial<DataStudioState> };
    expect(set.value.loadedId).toBe("product-abc");
    expect(set.value.name).toBe("Kem chống nắng");
    expect(set.value.document?.revision).toBe(3);
    expect(set.value.common?.["commerce.price.current"]).toBe("329000");
    expect(set.value.factRows).toEqual([{ label: "Dùng cho da dầu", value: "Có" }]);
  });
});

describe("suggestion acceptance flow (9.5-9.6)", () => {
  it("accept moves the suggestion into fact_rows and removes the card; save writes it", () => {
    let state: DataStudioState = {
      ...baseState(),
      suggestions: [
        { key: "custom.xuat-xu", label: "Xuất xứ", value: "Pháp", unit: null, type: "str" },
      ],
    };
    state = datastudioReducer(state, { type: "DATASTUDIO_SUGGESTION_ACCEPT", index: 0 });
    expect(state.suggestions).toEqual([]);
    expect(state.factRows).toContainEqual({ label: "Xuất xứ", value: "Pháp" });
    expect(toUpsertRequest(state).fact_rows).toContainEqual({ label: "Xuất xứ", value: "Pháp" });
  });

  it("dismiss removes the card without touching fact_rows", () => {
    let state: DataStudioState = {
      ...baseState(),
      suggestions: [
        { key: "custom.x", label: "X", value: "1", unit: null, type: "str" },
        { key: "custom.y", label: "Y", value: "2", unit: null, type: "str" },
      ],
    };
    state = datastudioReducer(state, { type: "DATASTUDIO_SUGGESTION_DISMISS", index: 0 });
    expect(state.suggestions).toHaveLength(1);
    expect(state.suggestions[0]?.label).toBe("Y");
    expect(state.factRows).toEqual(baseState().factRows);
  });

  it("suggestFlow stores returned suggestions without writing anything", async () => {
    let dispatched: unknown[] = [];
    const flow = {
      api: mockApi({
        suggestFacts: vi.fn().mockResolvedValue({
          suggestions: [{ key: "custom.xuat-xu", label: "Xuất xứ", value: "Pháp", unit: null, type: "str" }],
          note: "Tách từ block 1",
        }),
      }),
      state: baseState(),
      dispatch: (a: unknown) => {
        dispatched.push(a);
      },
    };
    await suggestFlow(flow as never, 0);
    const suggest = dispatched.find((a) => (a as { type: string }).type === "DATASTUDIO_SUGGEST") as {
      suggestions: unknown[];
      note?: string;
    };
    expect(suggest.suggestions).toHaveLength(1);
    expect(suggest.note).toBe("Tách từ block 1");
    expect(dispatched.some((a) => (a as { type: string }).type === "DATASTUDIO_SET")).toBe(false);
  });
});

describe("save flow (9.5, 9.6, 9.7)", () => {
  it("save puts the draft and never calls suggestions; save works with AI disabled", async () => {
    const putEntity = vi.fn().mockResolvedValue(sampleDocument());
    const suggestFacts = vi.fn();
    const listEntities = vi.fn().mockResolvedValue({ entities: [sampleDocument()] });
    let dispatched: unknown[] = [];
    const flow = {
      api: mockApi({ putEntity, suggestFacts, listEntities }),
      state: stateWithLoaded(),
      dispatch: (a: unknown) => {
        dispatched.push(a);
      },
      onEvent: vi.fn(),
    };
    const ok = await saveFlow(flow as never);
    expect(ok).toBe(true);
    expect(putEntity).toHaveBeenCalledWith("product-abc", expect.objectContaining({ id: "product-abc" }));
    expect(suggestFacts).not.toHaveBeenCalled();
    const set = dispatched.find((a) => (a as { type: string }).type === "DATASTUDIO_SET") as { value: Partial<DataStudioState> };
    expect(set.value.document?.revision).toBe(3);
    expect(set.value.suggestions).toEqual([]);
  });

  it("rejects save without a name", async () => {
    const putEntity = vi.fn();
    const flow = {
      api: mockApi({ putEntity }),
      state: { ...stateWithLoaded(), name: "  " },
      dispatch: vi.fn(),
    };
    const ok = await saveFlow(flow as never);
    expect(ok).toBe(false);
    expect(putEntity).not.toHaveBeenCalled();
  });
});

describe("render preview (9.8)", () => {
  it("displays the exact rendered string from the API", async () => {
    let dispatched: unknown[] = [];
    const flow = {
      api: mockApi({
        renderPreview: vi.fn().mockResolvedValue({
          entity_id: "product-abc",
          selectors: ["Giá hiện tại"],
          rendered: "- Giá hiện tại: 329.000 VND",
        }),
      }),
      state: { ...stateWithLoaded(), selectors: "Giá hiện tại" },
      dispatch: (a: unknown) => {
        dispatched.push(a);
      },
    };
    await renderPreviewFlow(flow as never);
    const set = dispatched.find((a) => (a as { type: string }).type === "DATASTUDIO_SET") as { value: Partial<DataStudioState> };
    expect(set.value.renderText).toBe("- Giá hiện tại: 329.000 VND");
  });

  it("warns instead of rendering when no entity is loaded", async () => {
    const renderPreview = vi.fn();
    const flow = {
      api: mockApi({ renderPreview }),
      state: { ...baseState(), loadedId: null },
      dispatch: vi.fn(),
    };
    await renderPreviewFlow(flow as never);
    expect(renderPreview).not.toHaveBeenCalled();
  });
});
