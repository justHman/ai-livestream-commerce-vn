/** API client behavior tests — safe response/error parsing, canonical paths. */

import { describe, expect, it, vi } from "vitest";

import { createApi } from "../src/api";

function makeApi(fetchImpl: () => Promise<Response>) {
  vi.stubGlobal("fetch", fetchImpl);
  return createApi({
    backendUrl: "http://127.0.0.1:8800",
    viewerToken: () => "viewer-token",
    adminToken: () => "admin-token",
  });
}

function mockFetch(status: number, body: unknown) {
  return vi.fn().mockResolvedValue(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

describe("api requestJson error parsing", () => {
  it("throws ApiError with field-location detail from pydantic validationErrors", async () => {
    const api = makeApi(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            detail: [
              { loc: ["body", "products", 0, "name"], msg: "Field required" },
              { loc: ["body", "products", 0, "price"], msg: "Input should be a valid integer" },
            ],
          }),
          { status: 422, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    await expect(api.attach("s1", { shop_profile: {}, products: [] })).rejects.toMatchObject({
      message: "body.products.0.name: Field required; body.products.0.price: Input should be a valid integer",
      status: 422,
    });
  });

  it("throws on non-JSON response", async () => {
    const api = makeApi(() =>
      Promise.resolve(new Response("<html>oops</html>", { status: 500 })),
    );
    await expect(api.healthReady()).rejects.toMatchObject({
      message: "HTTP 500: phản hồi không phải JSON",
      status: 500,
    });
  });

  it("throws ApiError on string detail", async () => {
    const api = makeApi(() =>
      Promise.resolve(new Response(JSON.stringify({ detail: "nope" }), { status: 400, headers: { "Content-Type": "application/json" } })),
    );
    await expect(api.healthReady()).rejects.toMatchObject({ message: "nope", status: 400 });
  });

  it("parses 200 JSON body", async () => {
    const api = makeApi(() =>
      Promise.resolve(new Response(JSON.stringify({ ok: true, render_backend: "mock" }), { status: 200, headers: { "Content-Type": "application/json" } })),
    );
    await expect(api.healthReady()).resolves.toMatchObject({ ok: true, render_backend: "mock" });
  });

  it("returns empty body as null", async () => {
    const api = makeApi(() => Promise.resolve(new Response(null, { status: 200 })));
    await expect(api.interrupt("s1")).resolves.toBeNull();
  });
});

describe("api canonical paths", () => {
  it("startSession posts to /api/v1/sessions", async () => {
    const fetchMock = mockFetch(200, { session_id: "sid-1", mode: "LITE" });
    const api = createApi({
      backendUrl: "http://127.0.0.1:8800/",
      viewerToken: () => "v",
      adminToken: () => "a",
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.startSession({ avatar_id: null, is_sandbox: true });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/sessions");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ avatar_id: null, is_sandbox: true });
  });

  it("speak uses path-style contract", async () => {
    const fetchMock = mockFetch(200, { ok: true });
    const api = createApi({
      backendUrl: "http://127.0.0.1:8800",
      viewerToken: () => "v",
      adminToken: () => "a",
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.say("sid-1", "hello", false);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/sessions/sid-1/say");
    expect(JSON.parse(init.body as string)).toEqual({ text: "hello", generate: false });
  });

  it("attach uses canonical path with body sans session_id", async () => {
    const fetchMock = mockFetch(200, { ok: true, products: 3, will_speak: false });
    const api = createApi({
      backendUrl: "http://127.0.0.1:8800",
      viewerToken: () => "v",
      adminToken: () => "a",
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.attach("sid-1", { shop_profile: { shop_name: "S" }, products: [] });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/sessions/sid-1/attach");
    expect(JSON.parse(init.body as string)).toEqual({ shop_profile: { shop_name: "S" }, products: [] });
  });

  it("applies runtime config via PATCH on canonical session path", async () => {
    const fetchMock = mockFetch(200, { ok: true, config_revision: 3 });
    const api = createApi({
      backendUrl: "http://127.0.0.1:8800",
      viewerToken: () => "v",
      adminToken: () => "a",
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.applyRuntimeConfig("sid-1", { comment_rate: 0.67 });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8800/api/v1/sessions/sid-1/config");
    expect(init.method).toBe("PATCH");
  });

  it("never uses /lite/* aliases", async () => {
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(JSON.stringify({ ok: true }), { status: 200, headers: { "Content-Type": "application/json" } }),
      ),
    );
    const api = createApi({
      backendUrl: "http://127.0.0.1:8800",
      viewerToken: () => "v",
      adminToken: () => "a",
    });
    vi.stubGlobal("fetch", fetchMock);
    await api.stopSession("s1");
    const urls = fetchMock.mock.calls.map(([url]) => url as string);
    expect(urls.some((u) => u.includes("/lite/"))).toBe(false);
    expect(urls).toContain("http://127.0.0.1:8800/api/v1/sessions/s1/stop");
  });
});