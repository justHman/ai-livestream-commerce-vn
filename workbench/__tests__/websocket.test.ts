/** WebSocket canonical URL construction + lifecycle tests. */

import { describe, expect, it } from "vitest";

import { controlSocketUrl } from "../src/websocket";

describe("websocket canonical URLs", () => {
  it("control WS path is /api/v1/ws/control/{session}", () => {
    const url = controlSocketUrl("http://127.0.0.1:8800", "sid-1", "tok");
    expect(url).toBe("ws://127.0.0.1:8800/api/v1/ws/control/sid-1?token=tok");
  });

  it("https backend maps to wss", () => {
    const url = controlSocketUrl("https://api.example.com", "sid", "");
    expect(url).toBe("wss://api.example.com/api/v1/ws/control/sid");
  });

  it("URL-encodes session id", () => {
    const url = controlSocketUrl("http://x:1", "a/b c", "t");
    expect(url).toContain("/api/v1/ws/control/a%2Fb%20c");
  });
});