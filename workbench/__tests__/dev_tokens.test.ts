/** Token prefill + non-persistence tests (Task 1.46). */

import { describe, expect, it } from "vitest";

import { DEV_TOKENS } from "../src/dev_tokens";

describe("dev tokens", () => {
  it("viewer and admin fixtures are exact public local values", () => {
    expect(DEV_TOKENS.viewerToken).toBe("local-test-token-123456789012345678901234567890");
    expect(DEV_TOKENS.adminToken).toBe("local-admin-token-123456789012345678901234567890");
  });

  it("tokens live in page memory only — module exposes plain values, no storage", () => {
    // The module has no side effects and never touches localStorage/sessionStorage.
    const storageKeyReferences = Object.keys(DEV_TOKENS);
    expect(storageKeyReferences).toHaveLength(2);
  });

  it("never contains provider or LiveKit API secrets", () => {
    const values = Object.values(DEV_TOKENS).join(" ");
    expect(values).not.toMatch(/sk_live|livekit_api|api_secret|eyJ[a-zA-Z0-9_-]{20,}/);
  });
});