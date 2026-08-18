/** Script authoring — transport, idempotency key, SSE dedup tests. */

import { describe, expect, it, vi } from "vitest";

import { createScriptClient, mapScriptSetResponse } from "../src/scriptSets";
import { idempotencyKey, parseSseFrame, SseFeed } from "../src/scriptSets";
import type { BackendScriptSetResponse, ScriptEvent } from "../src/scriptSets";

function makeClient(fetchImpl: () => Promise<Response>) {
  vi.stubGlobal("fetch", fetchImpl);
  return createScriptClient({
    backendUrl: "http://127.0.0.1:8800",
    viewerToken: () => "",
    adminToken: () => "admin-token",
  });
}

function mockFetch(status: number, body: unknown) {
  // Fresh Response per call so multi-fetch flows (e.g. per-product previews)
  // never read an already-consumed body.
  return vi.fn().mockImplementation(() =>
    Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } })),
  );
}

describe("script client canonical paths", () => {
  it("creates ScriptSet via POST /api/v1/script-sets with name + brief.title + product_ids", async () => {
    const fetchMock = mockFetch(200, {
      id: "sets-1",
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      product_ids: ["P001"],
      revision: 1,
      items: { P001: { state: "EMPTY" } },
    });
    const client = makeClient(fetchMock);
    await client.createScriptSet({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      product_ids: ["P001"],
      brief: { title: "Phiên bán kem", host_name: "MC", shop_name: "Shop", note: "" },
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/script-sets");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      name: "Set A",
      transition_policy: "ORDER_AGNOSTIC",
      product_ids: ["P001"],
      brief: { title: "Phiên bán kem", host_name: "MC", shop_name: "Shop", note: "" },
    });
  });

  it("submit posts to canonical product submit path", async () => {
    const fetchMock = mockFetch(200, { state: "gate_failed", violations: [{ rule_id: "STYLE_DISALLOWED_PUNCTUATION", severity: "ERROR", message: "em-dash" }] });
    const client = makeClient(fetchMock);
    const result = await client.submit("sets-1", "P001");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/script-sets/sets-1/products/P001/submit");
    expect(init.method).toBe("POST");
    expect(result.state).toBe("gate_failed");
  });

  it("generate sends 202 job semantics with idempotency header", async () => {
    const fetchMock = mockFetch(202, { status: "accepted", workflow_id: "wf-1" });
    const client = makeClient(fetchMock);
    const job = await client.generateProduct("sets-1", "P001", 600, "key-123");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe("key-123");
    expect(job.status).toBe("accepted");
  });

  it("approve-batch posts product_ids + version_ids + actor to the canonical path", async () => {
    const fetchMock = mockFetch(200, { approvals: [] });
    const client = makeClient(fetchMock);
    await client.approveBatch("sets-1", ["P001", "P002"], { P001: "v1", P002: "v2" }, "operator");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/script-sets/sets-1/approve-batch");
    expect(JSON.parse(init.body as string)).toEqual({
      product_ids: ["P001", "P002"],
      version_ids: { P001: "v1", P002: "v2" },
      actor: "operator",
    });
  });

  it("approve posts version_id + actor to the per-product approve path", async () => {
    const fetchMock = mockFetch(200, { approval: {}, state: "APPROVED" });
    const client = makeClient(fetchMock);
    await client.approveProduct("sets-1", "P001", "v5", "operator");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/script-sets/sets-1/products/P001/approve");
    expect(JSON.parse(init.body as string)).toEqual({ version_id: "v5", actor: "operator" });
  });

  it("preview posts product_id + target_duration_s to the canonical generation-preview path", async () => {
    const fetchMock = mockFetch(200, { product_id: "P001", target_duration_s: 600, planned_segment_count: 1, estimated_semantic_calls: 2 });
    const client = makeClient(fetchMock);
    await client.previewProduct("sets-1", "P001", 600);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/script-sets/sets-1/products/P001/generation-preview");
    expect(JSON.parse(init.body as string)).toEqual({ product_id: "P001", target_duration_s: 600 });
  });

  it("batch preview aggregates per-product previews from the canonical per-product path", async () => {
    const fetchMock = mockFetch(200, { product_id: "P001", target_duration_s: 600, planned_segment_count: 1, estimated_semantic_calls: 2 });
    const client = makeClient(fetchMock);
    const result = await client.previewBatch("sets-1", {
      products: [
        { product_id: "P001", target_duration_s: 600 },
        { product_id: "P002", target_duration_s: 1200 },
      ],
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(fetchMock.mock.calls[0][0]).toBe("http://127.0.0.1:8800/api/v1/script-sets/sets-1/products/P001/generation-preview");
    expect(fetchMock.mock.calls[1][0]).toBe("http://127.0.0.1:8800/api/v1/script-sets/sets-1/products/P002/generation-preview");
    expect(result.estimated_semantic_calls_total).toBe(4);
  });

  it("generate-batch posts product_ids + target_duration_s (not a products list)", async () => {
    const fetchMock = mockFetch(202, { status: "accepted", batch_id: "batch-1" });
    const client = makeClient(fetchMock);
    await client.generateBatch("sets-1", { product_ids: ["P001", "P002"], target_duration_s: 600 }, "key-1");
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/script-sets/sets-1/generate-batch");
    expect(JSON.parse(init.body as string)).toEqual({ product_ids: ["P001", "P002"], target_duration_s: 600 });
  });

  it("gate-fail returns 200 with domain payload, not transport error", async () => {
    const fetchMock = mockFetch(200, { state: "gate_failed", violations: [{ rule_id: "FORMAT_X", severity: "ERROR", message: "x" }] });
    const client = makeClient(fetchMock);
    await expect(client.submit("sets-1", "P001")).resolves.toMatchObject({ state: "gate_failed" });
  });

  it("409 invalid transition surfaces ApiError with status", async () => {
    const fetchMock = mockFetch(409, { detail: "AI fix chỉ hợp lệ khi gate_failed" });
    const client = makeClient(fetchMock);
    await expect(client.fixProduct("sets-1", "P001", "k")).rejects.toMatchObject({ status: 409 });
  });
});

describe("idempotencyKey", () => {
  it("is deterministic for equal payloads", () => {
    expect(idempotencyKey({ a: 1, b: [2] })).toBe(idempotencyKey({ a: 1, b: [2] }));
  });

  it("differs when payload changes", () => {
    expect(idempotencyKey({ a: 1 })).not.toBe(idempotencyKey({ a: 2 }));
  });
});

describe("SSE frame parsing", () => {
  it("parses named event with data and id", () => {
    const frame = parseSseFrame("event: segment.gate_passed\nid: ev-7\ndata: {\"type\":\"segment.gate_passed\"}\n\n");
    expect(frame?.event).toBe("segment.gate_passed");
    expect(frame?.id).toBe("ev-7");
    expect(frame?.data).toContain("segment.gate_passed");
  });

  it("ignores comment-only frames", () => {
    expect(parseSseFrame(": keepalive\n\n")).toBeNull();
  });

  it("joins multi-line data with newline per SSE spec", () => {
    const frame = parseSseFrame("data: {\"a\":1,\ndata: \"b\"}\n\n");
    expect(frame?.data).toBe('{"a":1,\n"b"}');
  });
});

function makeEvent(overrides: Partial<ScriptEvent>): ScriptEvent {
  return {
    event_id: "ev-1",
    revision: 1,
    type: "batch.progress",
    script_set_id: "sets-1",
    batch_id: "batch-1",
    ...overrides,
  };
}

function sseFrame(event: ScriptEvent): string {
  return `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`;
}

describe("SseFeed snapshot + revision dedup (task 13.5)", () => {
  it("applies snapshot then live events with monotonic revision", () => {
    const seen: string[] = [];
    const feed = new SseFeed({ onEvent: (e) => seen.push(e.type) });
    feed.push(sseFrame(makeEvent({ event_id: "snap-1", revision: 10, type: "batch.snapshot" })));
    feed.push(sseFrame(makeEvent({ event_id: "ev-11", revision: 11, type: "segment.started" })));
    expect(seen).toEqual(["batch.snapshot", "segment.started"]);
  });

  it("drops live events before the first snapshot", () => {
    const seen: string[] = [];
    const feed = new SseFeed({ onEvent: (e) => seen.push(e.type) });
    feed.push(sseFrame(makeEvent({ event_id: "ev-1", revision: 1, type: "segment.started" })));
    expect(seen).toEqual([]);
  });

  it("does not re-apply events with revision <= snapshot revision (reconnect replay)", () => {
    const seen: string[] = [];
    const feed = new SseFeed({ onEvent: (e) => seen.push(e.type) });
    feed.push(sseFrame(makeEvent({ event_id: "snap-1", revision: 5, type: "batch.snapshot" })));
    feed.push(sseFrame(makeEvent({ event_id: "ev-6", revision: 6, type: "segment.gate_passed" })));
    // Reconnect: server replays snapshot + events up to revision 6.
    feed.push(sseFrame(makeEvent({ event_id: "snap-1", revision: 6, type: "batch.snapshot" })));
    feed.push(sseFrame(makeEvent({ event_id: "ev-6", revision: 6, type: "segment.gate_passed" })));
    feed.push(sseFrame(makeEvent({ event_id: "ev-7", revision: 7, type: "product.reviewable" })));
    expect(seen).toEqual(["batch.snapshot", "segment.gate_passed", "batch.snapshot", "product.reviewable"]);
    // segment.gate_passed applied exactly once across the reconnect.
    expect(seen.filter((t) => t === "segment.gate_passed")).toHaveLength(1);
  });

  it("dedups duplicate event ids within one session", () => {
    const seen: string[] = [];
    const feed = new SseFeed({ onEvent: (e) => seen.push(e.type) });
    feed.push(sseFrame(makeEvent({ event_id: "snap-1", revision: 1, type: "batch.snapshot" })));
    feed.push(sseFrame(makeEvent({ event_id: "ev-2", revision: 2, type: "batch.progress" })));
    feed.push(sseFrame(makeEvent({ event_id: "ev-2", revision: 2, type: "batch.progress" })));
    expect(seen.filter((t) => t === "batch.progress")).toHaveLength(1);
  });
});

describe("real-backend ScriptSet wire adapter (handoff §11.3/§11.4)", () => {
  // Exact shape returned by ScriptAuthoringServiceImpl._set_wire — an `items`
  // map keyed by product_id, NOT a `products[]` array.
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

  it("mapScriptSetResponse expands the items map into full products with parsed states", () => {
    const set = mapScriptSetResponse(realWire);
    expect(set.id).toBe("set-9");
    expect(set.name).toBe("Phiên live 19/08");
    expect(set.revision).toBe(3);
    expect(set.transition_policy).toBe("ORDER_AGNOSTIC");
    expect(set.products).toHaveLength(2);
    expect(set.products.map((p) => p.product_id)).toEqual(["P001", "P002"]);
    expect(set.products[0]?.state).toBe("DRAFT");
    expect(set.products[1]?.state).toBe("REVIEWABLE");
    // Every product is a structurally complete ScriptItem the UI already renders.
    expect(set.products[0]?.segments).toEqual([]);
    expect(set.products[0]?.versions).toEqual([]);
    expect(set.products[0]?.current_version).toBeNull();
    expect(set.products[0]?.approvals).toEqual([]);
  });

  it("GET /script-sets/{set_id} parses the real wire shape through the client", async () => {
    const fetchMock = mockFetch(200, realWire);
    const client = makeClient(fetchMock);
    const set = await client.getScriptSet("set-9");
    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/script-sets/set-9");
    expect(set.products).toHaveLength(2);
    expect(set.products.map((p) => p.product_id)).toEqual(["P001", "P002"]);
    expect(set.products[0]?.state).toBe("DRAFT");
  });

  it("POST /script-sets parses the real wire shape through the client", async () => {
    const fetchMock = mockFetch(201, realWire);
    const client = makeClient(fetchMock);
    const set = await client.createScriptSet({
      name: "Phiên live 19/08",
      transition_policy: "ORDER_AGNOSTIC",
      product_ids: ["P001", "P002"],
      brief: { title: "t" },
    });
    expect(set.products).toHaveLength(2);
    expect(set.products[1]?.state).toBe("REVIEWABLE");
  });
});
