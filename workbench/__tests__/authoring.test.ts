/** Script authoring — legal-state guards, mock zero-LLM flow, batch UX tests. */

import { describe, expect, it, vi } from "vitest";

import {
  applyBatchEvent,
  approveBatchFlow,
  authoringReducer,
  canApprove,
  canFix,
  canGenerate,
  canRegenerateSegment,
  canSubmit,
  createMockScriptClient,
  generateAllFlow,
  initialStateAuthoring,
  loadSetFlow,
  previewFlow,
  type AuthoringState,
} from "../src/authoring";
import { createScriptClient, idempotencyKey, mapScriptSetResponse } from "../src/scriptSets";
import type { BackendScriptSetResponse, ScriptSet } from "../src/scriptSets";

describe("legal-state guards (task 13.2)", () => {
  it("fix is allowed only for GATE_FAILED", () => {
    expect(canFix("GATE_FAILED")).toBe(true);
    expect(canFix("DRAFT")).toBe(false);
    expect(canFix("REVIEWABLE")).toBe(false);
    expect(canFix("APPROVED")).toBe(false);
  });

  it("approve is allowed only for REVIEWABLE", () => {
    expect(canApprove("REVIEWABLE")).toBe(true);
    expect(canApprove("APPROVED")).toBe(false);
    expect(canApprove("GATE_FAILED")).toBe(false);
    expect(canApprove("STALE")).toBe(false);
  });

  it("submit is allowed for DRAFT and GATE_FAILED only", () => {
    expect(canSubmit("DRAFT")).toBe(true);
    expect(canSubmit("GATE_FAILED")).toBe(true);
    expect(canSubmit("REVIEWABLE")).toBe(false);
    expect(canSubmit("EMPTY")).toBe(false);
  });

  it("generate is allowed when no fresh approval exists", () => {
    for (const state of ["EMPTY", "DRAFT", "GATE_FAILED", "STALE"]) {
      expect(canGenerate(state)).toBe(true);
    }
    expect(canGenerate("REVIEWABLE")).toBe(false);
    expect(canGenerate("APPROVED")).toBe(false);
  });

  it("regenerate is allowed only for gate-failed segments of a GATE_FAILED item", () => {
    expect(canRegenerateSegment({ status: "gate_failed", versions: [] }, "GATE_FAILED")).toBe(true);
    expect(canRegenerateSegment({ status: "gate_passed", versions: [] }, "GATE_FAILED")).toBe(false);
    expect(canRegenerateSegment({ status: "gate_failed", versions: [] }, "REVIEWABLE")).toBe(false);
  });
});

describe("zero-LLM manual PASS flow (task 13.8)", () => {
  it("manual draft -> submit -> reviewable -> approve reaches APPROVED without generation", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001"],
    });
    const draft = await client.putDraft(set.id, "P001", { display_text: "Kem chống nắng chỉ 329 nghìn đồng." });
    expect(draft.state).toBe("DRAFT");
    const submitted = await client.submit(set.id, "P001");
    expect(submitted.state).toBe("reviewable");
    const beforeApprove = await client.getScriptSet(set.id);
    const approved = await client.approveProduct(set.id, "P001", beforeApprove.products[0]!.current_version!.version_id, "operator");
    expect(approved.state).toBe("APPROVED");
    const reloaded = await client.getScriptSet(set.id);
    expect(reloaded.products[0]?.state).toBe("APPROVED");
    expect(reloaded.products[0]?.approved_revision).toBe(1);
  });

  it("approve is rejected for non-REVIEWABLE versions", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001"],
    });
    await client.putDraft(set.id, "P001", { display_text: "abc" });
    await expect(client.approveProduct(set.id, "P001", "x", "operator")).rejects.toThrow(/409/);
  });

  it("manual gate FAIL -> fix with AI -> resubmit -> reviewable", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001"],
    });
    await client.putDraft(set.id, "P001", { display_text: "Không dùng--dấu gạch dài." });
    const failed = await client.submit(set.id, "P001");
    expect(failed.state).toBe("gate_failed");
    expect(failed.violations?.some((v) => v.rule_id === "STYLE_DISALLOWED_PUNCTUATION")).toBe(true);
    // AI fix on gate-failed version is legal and creates a new draft.
    await client.fixProduct(set.id, "P001", idempotencyKey({ op: "fix" }));
    const resubmitted = await client.submit(set.id, "P001");
    expect(resubmitted.state).toBe("reviewable");
  });

  it("fix is rejected with 409 when the version is not gate-failed", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001"],
    });
    await client.putDraft(set.id, "P001", { display_text: "ok" });
    await expect(client.fixProduct(set.id, "P001", "k")).rejects.toThrow(/409/);
  });

  it("segment regenerate creates a new segment version, not a sibling rewrite", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001"],
    });
    // Put the item into GATE_FAILED first (segment pause), then regenerate.
    await client.putDraft(set.id, "P001", { display_text: "x--y" });
    await client.submit(set.id, "P001");
    await client.regenerateSegment(set.id, "P001", 0, "k");
    const reloaded = await client.getScriptSet(set.id);
    const item = reloaded.products[0]!;
    expect(item.state).toBe("DRAFT");
    expect(item.segments[0]?.versions).toHaveLength(1);
    expect(item.segments[0]?.status).toBe("gate_passed");
  });
});

describe("Generate All UX (tasks 13.4, 13.8)", () => {
  async function readyState(): Promise<{ state: AuthoringState; client: ReturnType<typeof createMockScriptClient> }> {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001", "P002"],
    });
    const state: AuthoringState = {
      ...initialStateAuthoring(),
      setId: set.id,
      scriptSet: set,
      productIds: ["P001", "P002"],
      targets: { P001: 600, P002: 1200 },
    };
    return { state, client };
  }

  it("preview estimates 1+K calls per product without any LLM call", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001", "P002"],
    });
    const state: AuthoringState = { ...initialStateAuthoring(), setId: set.id, productIds: ["P001", "P002"], targets: { P001: 600, P002: 3600 } };
    const dispatch = vi.fn();
    await previewFlow({ client, state, dispatch });
    const previewAction = dispatch.mock.calls.find(([a]) => a.type === "AUTHORING_PREVIEW");
    expect(previewAction).toBeDefined();
    const preview = (previewAction![0] as { value: GenerationPreview }).value;
    expect(preview.products[0]?.estimated_semantic_calls).toBe(2); // 1 + K, K=1 for 600s
    expect(preview.products[1]?.estimated_semantic_calls).toBe(7); // 1 + K, K=6 for 3600s
    expect(preview.estimated_semantic_calls_total).toBe(9);
  });

  it("double-click idempotency: second Generate All while running is a no-op", async () => {
    const { state, client } = await readyState();
    state.preview = {
      products: [
        { product_id: "P001", target_duration_s: 600, planned_segment_count: 1, estimated_semantic_calls: 2 },
        { product_id: "P002", target_duration_s: 1200, planned_segment_count: 2, estimated_semantic_calls: 3 },
      ],
      estimated_semantic_calls_total: 5,
    };
    const dispatch = vi.fn();
    const flow = { client, state, dispatch };
    // First click succeeds.
    const first = await generateAllFlow(flow);
    expect(first?.status).toBe("accepted");
    // Simulate the running state the first click set.
    state.batch.status = "running";
    // Second click must not dispatch another generate call.
    const second = await generateAllFlow(flow);
    expect(second).toBeNull();
    const generateActions = dispatch.mock.calls.filter(([a]) => a.type === "AUTHORING_BATCH" && (a.value as { batchId?: string }).batchId);
    expect(generateActions).toHaveLength(1);
  });

  it("Generate All requires a preview estimate first", async () => {
    const { state, client } = await readyState();
    const dispatch = vi.fn();
    const result = await generateAllFlow({ client, state, dispatch });
    expect(result).toBeNull();
    expect(dispatch.mock.calls.some(([a]) => a.type === "AUTHORING_STATUS" && String(a.status).includes("preview"))).toBe(true);
  });

  it("segment failure pauses the product (mock gate-fail stops later spend)", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001"],
    });
    await client.putDraft(set.id, "P001", { display_text: "x--y" });
    const failed = await client.submit(set.id, "P001");
    expect(failed.state).toBe("gate_failed");
    // No automatic fix/regenerate: state stays gate_failed until human action.
    const reloaded = await client.getScriptSet(set.id);
    expect(reloaded.products[0]?.state).toBe("GATE_FAILED");
    expect(reloaded.products[0]?.approvals).toHaveLength(0);
  });
});

describe("approval invalidation (task 13.7)", () => {
  it("new draft after approval leaves the prior approval untouched (immutable history)", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T", host_name: "H", shop_name: "S", note: "" },
      product_ids: ["P001"],
    });
    await client.putDraft(set.id, "P001", { display_text: "v1" });
    await client.submit(set.id, "P001");
    const beforeApprove = await client.getScriptSet(set.id);
    await client.approveProduct(set.id, "P001", beforeApprove.products[0]!.current_version!.version_id, "operator");
    // Edit after approval creates a new draft version.
    const next = await client.putDraft(set.id, "P001", { display_text: "v2" });
    expect(next.state).toBe("DRAFT");
    const reloaded = await client.getScriptSet(set.id);
    const item = reloaded.products[0]!;
    expect(item.state).toBe("DRAFT");
    expect(item.approvals).toHaveLength(1);
    expect(item.approved_version_id).toBe(item.approvals[0]?.version_id);
    // The approval record stays bound to v1 while the current version is v2.
    expect(item.versions[item.versions.length - 1]?.version).toBe(2);
  });
});

describe("SSE event application (task 13.5)", () => {
  it("snapshot sets batch id; transport failure marks retryable", () => {
    let state = initialStateAuthoring();
    const snapshot: ScriptEvent = {
      event_id: "snap-1",
      revision: 3,
      type: "batch.snapshot",
      script_set_id: "sets-1",
      batch_id: "batch-9",
      snapshot: { status: "running" } as never,
    };
    state = applyBatchEvent(state, snapshot);
    expect(state.batch.batchId).toBe("batch-9");
    expect(state.batch.status).toBe("running");
    const transportFail: ScriptEvent = {
      event_id: "ev-4",
      revision: 4,
      type: "product.failed",
      script_set_id: "sets-1",
      batch_id: "batch-9",
      failure: { reason: "transport", message: "provider timeout" },
    };
    state = applyBatchEvent(state, transportFail);
    expect(state.batch.transportRetrying).toBe(true);
    expect(state.batch.lastError).toContain("provider timeout");
    const completed: ScriptEvent = {
      event_id: "ev-5",
      revision: 5,
      type: "batch.completed",
      script_set_id: "sets-1",
      batch_id: "batch-9",
    };
    state = applyBatchEvent(state, completed);
    expect(state.batch.status).toBe("completed");
    expect(state.batch.transportRetrying).toBe(false);
  });

  it("authoringReducer dispatches batch events", () => {
    let state = initialStateAuthoring();
    state = authoringReducer(state, {
      type: "AUTHORING_SSE_EVENT",
      value: { event_id: "e1", revision: 1, type: "batch.snapshot", script_set_id: "s", batch_id: "b" },
    });
    expect(state.batch.batchId).toBe("b");
  });
});

describe("real-backend wire interop (handoff §11.4)", () => {
  // Exact wire shape from ScriptAuthoringServiceImpl._set_wire.
  const realWire: BackendScriptSetResponse = {
    id: "set-9",
    name: "Phiên live 19/08",
    transition_policy: "ORDER_AGNOSTIC",
    product_ids: ["P001", "P002"],
    revision: 3,
    items: {
      P001: { state: "DRAFT" },
      P002: { state: "REVIEWABLE" },
    },
  };

  function wireClient() {
    const client = createScriptClient({
      backendUrl: "http://127.0.0.1:8800",
      viewerToken: () => "",
      adminToken: () => "t",
    });
    return { client };
  }

  it("loadSetFlow consumes a mapped real-wire ScriptSet (refresh path does not crash)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(realWire), { status: 200, headers: { "Content-Type": "application/json" } }),
      ),
    );
    const { client } = wireClient();
    const dispatch = vi.fn();
    const state = { ...initialStateAuthoring(), setId: "set-9" };
    await loadSetFlow({ client, state, dispatch }, "set-9");
    const setAction = dispatch.mock.calls.find(([a]) => a.type === "AUTHORING_SET");
    expect(setAction).toBeDefined();
    const value = setAction![0] as { value: { scriptSet: ScriptSet; productIds: string[] } };
    // The render path reads set.products.length / set.products.map(...).
    expect(value.value.scriptSet.products.length).toBe(2);
    expect(value.value.scriptSet.products.map((p) => p.product_id)).toEqual(["P001", "P002"]);
    expect(value.value.productIds).toEqual(["P001", "P002"]);
    vi.unstubAllGlobals();
  });

  it("approve path guards missing version data from the real wire (no crash)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(realWire), { status: 200, headers: { "Content-Type": "application/json" } }),
      ),
    );
    const { client } = wireClient();
    const dispatch = vi.fn();
    const mapped = mapScriptSetResponse(realWire);
    const state: AuthoringState = {
      ...initialStateAuthoring(),
      setId: "set-9",
      scriptSet: mapped,
      selectedApproveIds: ["P002"], // REVIEWABLE, but the wire carries no version data
    };
    await approveBatchFlow({ client, state, dispatch });
    const status = dispatch.mock.calls.find(([a]) => a.type === "AUTHORING_STATUS");
    expect(String(status?.[0]?.errors?.join(" "))).toContain("thiếu current_version");
    vi.unstubAllGlobals();
  });

  it("approve path resolves the current version when the mapped model carries it", async () => {
    const client = createMockScriptClient();
    const set = await client.createScriptSet({
      name: "S",
      transition_policy: "ORDER_AGNOSTIC",
      brief: { title: "T" },
      product_ids: ["P001"],
    });
    await client.putDraft(set.id, "P001", { display_text: "abc" });
    await client.submit(set.id, "P001");
    const mapped = await client.getScriptSet(set.id);
    const dispatch = vi.fn();
    const state: AuthoringState = {
      ...initialStateAuthoring(),
      setId: set.id,
      scriptSet: mapped,
      selectedApproveIds: ["P001"],
    };
    await approveBatchFlow({ client, state, dispatch });
    const status = dispatch.mock.calls.find(([a]) => a.type === "AUTHORING_STATUS");
    expect(String(status?.[0]?.status)).toContain("Đã duyệt 1 phiên bản");
  });
});
