/** DRAFT — rename to sse.test.ts when D.2 lands (RED evidence for D.3).
 * SSE transport contract against the real backend:
 * - fetch-stream with Authorization: Bearer <viewer> header (native EventSource
 *   cannot send headers; backend viewer_auth reads ONLY the header);
 * - no token in the URL query;
 * - each network frame parsed ONCE (raw frame -> payload JSON); a payload is
 *   never re-parsed as an SSE frame;
 * - snapshot event name maps to type="batch.snapshot" even though the backend
 *   payload carries no "type" field; reconnect replays snapshot idempotently;
 * - batch.error is terminal (danger status, no endless reconnect);
 * - set/batch/product identity preserved end-to-end.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// GREEN target: scriptSets exports a fetch-based SSE transport class.
import { BatchEventStream } from "../src/scriptSets";

const VIEWER = "local-test-token-123456789012345678901234567890";
const BASE = "http://backend.test";

function sseResponse(frames: string[], opts: { failFirst?: boolean } = {}): Response {
  const encoder = new TextEncoder();
  let sent = false;
  return new Response(
    new ReadableStream<Uint8Array>({
      start(controller) {
        if (opts.failFirst && !sent) {
          sent = true;
          controller.error(new Error("boom"));
          return;
        }
        for (const frame of frames) controller.enqueue(encoder.encode(frame));
        controller.close();
      },
    }),
    { status: 200, headers: { "Content-Type": "text/event-stream" } },
  );
}

function frame(event: string, data: unknown): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

function makeStream(fetchMock: ReturnType<typeof vi.fn>, handlers: Partial<Record<string, unknown>> = {}) {
  return new BatchEventStream({
    backendUrl: BASE,
    viewerToken: () => VIEWER,
    scriptSetId: "set_1",
    batchId: "batch_1",
    retryDelayMs: 0,
    onEvent: (handlers.onEvent as (e: unknown) => void) ?? (() => {}),
    onStatus: handlers.onStatus as ((m: string, t?: string) => void) | undefined,
  });
}

describe("BatchEventStream transport", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("connects via fetch with Bearer viewer header and clean URL", async () => {
    fetchMock.mockResolvedValue(sseResponse([frame("batch.snapshot", { batch_id: "batch_1", set_id: "set_1", status: "queued", revision: 1 })]));
    const stream = makeStream(fetchMock, { onEvent: () => {} });
    await stream.connect();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(String(url)).toBe(`${BASE}/api/v1/script-sets/set_1/generation-batches/batch_1/events`);
    expect(String(url)).not.toContain("token=");
    expect(new Headers(init.headers).get("Authorization")).toBe(`Bearer ${VIEWER}`);
    expect(new Headers(init.headers).get("Accept")).toBe("text/event-stream");
    stream.disconnect();
  });

  it("parses each frame once and maps event name to type", async () => {
    const seen: any[] = [];
    fetchMock.mockResolvedValue(sseResponse([
      frame("batch.snapshot", { batch_id: "batch_1", set_id: "set_1", status: "running", revision: 4 }),
      frame("product.failed", { batch_id: "batch_1", product_id: "p9", reason: "llm down" }),
    ]));
    await new Promise<void>((done) => {
      const stream = makeStream(fetchMock, { onEvent: (e: unknown) => { seen.push(e); if (seen.length === 2) done(); } });
      void stream.connect();
    });
    expect(seen[0].type).toBe("batch.snapshot");
    expect(seen[0].revision).toBe(4);
    expect(seen[0].snapshot?.status).toBe("running");
    expect(seen[1].type).toBe("product.failed");
    expect(seen[1].product_id).toBe("p9");
  });

  it("does not double-parse payload JSON as an SSE frame", async () => {
    const tricky = { batch_id: "b", set_id: "s", status: "x", revision: 2, note: "data: not-a-frame\nevent: trick" };
    const seen: any[] = [];
    fetchMock.mockResolvedValue(sseResponse([frame("batch.snapshot", tricky)]));
    await new Promise<void>((done) => {
      const stream = makeStream(fetchMock, { onEvent: (e: unknown) => { seen.push(e); done(); } });
      void stream.connect();
    });
    expect(seen[0].snapshot?.note ?? seen[0].note).toContain("data: not-a-frame");
  });

  it("reconnects after a transport error and reapplies snapshot idempotently", async () => {
    fetchMock
      .mockImplementationOnce(() => Promise.reject(new Error("conn reset")))
      .mockResolvedValueOnce(sseResponse([
        frame("batch.snapshot", { batch_id: "batch_1", set_id: "set_1", status: "running", revision: 5 }),
        frame("batch.progress", { batch_id: "batch_1" }),
      ]));
    const seen: any[] = [];
    const stream = makeStream(fetchMock, { onEvent: (e: unknown) => seen.push(e) });
    await stream.connect();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(seen.filter((e) => e.type === "batch.snapshot")).toHaveLength(1);
    stream.disconnect();
  });

  it("treats batch.error as terminal without endless reconnect", async () => {
    fetchMock.mockResolvedValue(sseResponse([
      frame("batch.snapshot", { batch_id: "batch_1", set_id: "set_1", status: "failed", revision: 6 }),
      frame("batch.error", { batch_id: "batch_1", code: "not_found" }),
    ]));
    const statuses: string[] = [];
    await new Promise<void>((done) => {
      const stream = makeStream(fetchMock, { onEvent: () => {}, onStatus: (_m: string, t?: string) => { statuses.push(t ?? ""); if (t === "danger") done(); } });
      void stream.connect();
    });
    await new Promise((r) => setTimeout(r, 20));
    expect(statuses).toContain("danger");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("stops reconnecting after disconnect()", async () => {
    fetchMock.mockRejectedValue(new Error("down"));
    const stream = makeStream(fetchMock, { onEvent: () => {} });
    void stream.connect();
    stream.disconnect();
    await new Promise((r) => setTimeout(r, 20));
    const callsAfterDisconnect = fetchMock.mock.calls.length;
    await new Promise((r) => setTimeout(r, 30));
    expect(fetchMock.mock.calls.length).toBe(callsAfterDisconnect);
  });
});
