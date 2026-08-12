/** Script authoring — transport, idempotency key, SSE dedup tests. */

import { describe, expect, it, vi } from "vitest";

import { createScriptClient } from "../src/scriptSets";
import { idempotencyKey, parseSseFrame, SseFeed } from "../src/scriptSets";
import type { ScriptEvent } from "../src/scriptSets";

function makeClient(fetchImpl: () => Promise<Response>) {
  vi.stubGlobal("fetch", fetchImpl);
  return createScriptClient({
    backendUrl: "http://127.0.0.1:8800",
    viewerToken: () => "",
    adminToken: () => "admin-token",
  });
}

function mockFetch(status: number, body: unknown) {
  return vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

describe("script client canonical paths", () => {
  it("creates ScriptSet via POST /api/v1/script-sets", async () => {
    const fetchMock = mockFetch(200, { id: "sets-1", revision: 1, products: [] });
    const client = makeClient(fetchMock);
    await client.createScriptSet({
      brief: { shop_name: "S", host_name: "H", persona: "", selling_style: "", transition_policy: "ORDER_AGNOSTIC" },
      product_ids: ["P001"],
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/script-sets");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string).product_ids).toEqual(["P001"]);
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

  it("approve-batch posts selected product ids", async () => {
    const fetchMock = mockFetch(200, { approvals: [] });
    const client = makeClient(fetchMock);
    await client.approveBatch("sets-1", ["P001", "P002"]);
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(init.body as string)).toEqual({ product_ids: ["P001", "P002"] });
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
