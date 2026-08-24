/**
 * REAL-backend contract smoke — drives the ACTUAL Workbench client code
 * (createApi, createScriptClient, BatchEventStream) against a booted mock-mode
 * backend. Gated: the whole suite is a no-op unless LIVE_BACKEND_URL is set,
 * so normal `npm test` in CI runs nothing here and stays green.
 *
 * Fixture tokens come from src/dev_tokens; the backend must be booted with
 * BACKEND_API_TOKEN=DEV_TOKENS.viewerToken and ADMIN_API_TOKEN=
 * DEV_TOKENS.adminToken so the two auth planes are actually enforced.
 */

import { describe, expect, it } from "vitest";

import { ApiError, createApi } from "../src/api";
import { BatchEventStream, createScriptClient } from "../src/scriptSets";
import { DEV_TOKENS } from "../src/dev_tokens";

const LIVE_URL = process.env.LIVE_BACKEND_URL;

function liveUrl(): string {
  if (!LIVE_URL) throw new Error("LIVE_BACKEND_URL is not set");
  return LIVE_URL;
}

const scriptSetInput = {
  name: "live-smoke",
  transition_policy: "ORDER_AGNOSTIC" as const,
  product_ids: ["P001"],
  brief: { title: "Phiên smoke live", host_name: "MC", shop_name: "Shop", note: "" },
};

describe.skipIf(!LIVE_URL)("live backend contract smoke", () => {
  it("healthReady(viewer) resolves 200", async () => {
    const api = createApi({
      backendUrl: liveUrl(),
      viewerToken: () => DEV_TOKENS.viewerToken,
      adminToken: () => DEV_TOKENS.adminToken,
    });
    const ready = await api.healthReady();
    expect(ready.ok).not.toBe(false);
  });

  it("adminConfig(admin) returns app_env and render_backend", async () => {
    const api = createApi({
      backendUrl: liveUrl(),
      viewerToken: () => DEV_TOKENS.viewerToken,
      adminToken: () => DEV_TOKENS.adminToken,
    });
    const cfg = await api.adminConfig();
    expect(typeof cfg.app_env).toBe("string");
    expect(typeof cfg.render_backend).toBe("string");
  });

  it("adminConfig(viewer) rejects with ApiError 403 (plane separation)", async () => {
    const api = createApi({
      backendUrl: liveUrl(),
      viewerToken: () => DEV_TOKENS.viewerToken,
      adminToken: () => DEV_TOKENS.viewerToken, // viewer token presented on the admin plane
    });
    await expect(api.adminConfig()).rejects.toMatchObject({ status: 403 });
  });

  it("createScriptSet(viewer) passes the auth gate (never 401)", async () => {
    const client = createScriptClient({
      backendUrl: liveUrl(),
      viewerToken: () => DEV_TOKENS.viewerToken,
      adminToken: () => DEV_TOKENS.adminToken,
    });
    let status = 0;
    try {
      await client.createScriptSet(scriptSetInput);
      status = 201;
    } catch (err) {
      status = err instanceof ApiError ? err.status : 0;
    }
    // Mock mode without DATABASE_URL returns 501 (authoring disabled) — that
    // 501 PROVES the viewer token cleared the auth gate. 401 is the only failure.
    expect(status, `expected a non-401 status, got ${status}`).not.toBe(401);
  });

  it("createScriptSet(admin token on viewer route) rejects with 401", async () => {
    // Pre-fix ownership sent the ADMIN token on the viewer-protected authoring
    // routes; the real backend rejects it with 401 (wrong viewer credential).
    const client = createScriptClient({
      backendUrl: liveUrl(),
      viewerToken: () => DEV_TOKENS.adminToken,
      adminToken: () => DEV_TOKENS.adminToken,
    });
    await expect(client.createScriptSet(scriptSetInput)).rejects.toMatchObject({ status: 401 });
  });

  it("BatchEventStream(viewer) connects without 401 and builds a query-less events URL", async () => {
    const scriptSetId = "live-set";
    const batchId = "live-batch-unknown"; // unknown -> backend 404/501, never 401
    const statusMessages: string[] = [];
    const stream = new BatchEventStream({
      backendUrl: liveUrl(),
      viewerToken: () => DEV_TOKENS.viewerToken,
      scriptSetId,
      batchId,
      retryDelayMs: 60_000, // far longer than the test, so no reconnect fires
      onEvent: () => {},
      onStatus: (message) => statusMessages.push(message),
    });
    await stream.connect();
    stream.disconnect(); // cancel any scheduled reconnect before teardown
    // A 401 surfaces here as onStatus("SSE lỗi HTTP 401 — dừng kết nối.").
    expect(statusMessages.join("\n")).not.toMatch(/HTTP 401/);
    // The transport builds the events URL exactly this way (scriptSets.ts
    // BatchEventStream.connect) and authenticates via header — never ?token=.
    const eventsUrl = `${liveUrl().replace(/\/$/, "")}/api/v1/script-sets/${encodeURIComponent(
      scriptSetId,
    )}/generation-batches/${encodeURIComponent(batchId)}/events`;
    expect(eventsUrl).not.toMatch(/[?&]token=/);
  });
});
