/** DRAFT — rename to auth_ownership.test.ts when unblocked (RED evidence for D.2).
 * Auth ownership: every /api/v1/script-sets/* route requires the VIEWER token
 * (BACKEND_API_TOKEN) via Authorization: Bearer header (backend scripts.py
 * uses Depends(viewer_auth) on ALL authoring routes incl. SSE /events).
 * The console previously sent the ADMIN token on every authoring call.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createScriptClient, type ApiDeps } from "../src/scriptSets";

const VIEWER = "local-test-token-123456789012345678901234567890";
const ADMIN = "local-admin-token-123456789012345678901234567890";

type CapturedRequest = { url: string; init: RequestInit };

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const WIRE_SET = {
  id: "set_1",
  name: "s",
  transition_policy: "ORDER_AGNOSTIC",
  product_ids: ["p1"],
  revision: 1,
  items: {},
};

describe("script authoring calls carry the VIEWER token", () => {
  let captured: CapturedRequest[];
  const fetchMock = vi.fn(async (url: string | URL | Request, init?: RequestInit) => {
    captured.push({ url: String(url), init: init ?? {} });
    return jsonResponse(WIRE_SET);
  });

  beforeEach(() => {
    captured = [];
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  function clientWithSeparateTokens() {
    const deps: ApiDeps = {
      backendUrl: "http://backend.test",
      viewerToken: () => VIEWER,
      adminToken: () => ADMIN,
    };
    return createScriptClient(deps);
  }

  const cases: Array<[string, (c: ReturnType<typeof createScriptClient>) => Promise<unknown>]> = [
    ["createScriptSet", (c) => c.createScriptSet({ name: "n", product_ids: ["p1"] })],
    ["getScriptSet", (c) => c.getScriptSet("set_1")],
    ["patchScriptSet", (c) => c.patchScriptSet("set_1", { name: "n2" })],
    ["putDraft", (c) => c.putDraft("set_1", "p1", { display_text: "d" })],
    ["submit", (c) => c.submit("set_1", "p1")],
    ["previewProduct", (c) => c.previewProduct("set_1", "p1", 600)],
    ["generateProduct", (c) => c.generateProduct("set_1", "p1", 600, "k1")],
    ["regenerateSegment", (c) => c.regenerateSegment("set_1", "p1", 0, "k2")],
    ["fixProduct", (c) => c.fixProduct("set_1", "p1", "k3")],
    ["approveProduct", (c) => c.approveProduct("set_1", "p1", "ver_1", "op")],
    ["approveBatch", (c) => c.approveBatch("set_1", ["p1"], { p1: "ver_1" }, "op")],
    ["generateBatch", (c) => c.generateBatch("set_1", { product_ids: ["p1"], target_duration_s: 600 }, "k4")],
    ["getBatch", (c) => c.getBatch("set_1", "batch_1")],
    ["cancelBatch", (c) => c.cancelBatch("set_1", "batch_1")],
  ];

  for (const [name, call] of cases) {
    it(`${name} sends Authorization: Bearer <viewer>`, async () => {
      await call(clientWithSeparateTokens());
      const last = captured.at(-1)!;
      const headers = new Headers((last.init.headers ?? undefined) as HeadersInit);
      expect(headers.get("Authorization")).toBe(`Bearer ${VIEWER}`);
    });

    it(`${name} never leaks the admin token`, async () => {
      await call(clientWithSeparateTokens());
      const last = captured.at(-1)!;
      const headers = new Headers((last.init.headers ?? undefined) as HeadersInit);
      expect(headers.get("Authorization")).not.toBe(`Bearer ${ADMIN}`);
    });
  }
});
