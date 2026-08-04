/** State reducer transition tests. */

import { describe, expect, it } from "vitest";

import { initialState, reducer } from "../src/state";

describe("reducer", () => {
  it("SESSION_SET merges partial session state", () => {
    let state = initialState();
    state = reducer(state, { type: "SESSION_SET", value: { id: "sid-1" } });
    expect(state.session.id).toBe("sid-1");
    expect(state.session.livekit).toBeNull();
  });

  it("SESSION_CLEAR resets session and clears diagnostics", () => {
    let state = initialState();
    state = reducer(state, { type: "SESSION_SET", value: { id: "sid-1" } });
    state = reducer(state, { type: "DIAGNOSTICS_SET", value: { foo: 1 } });
    state = reducer(state, { type: "SESSION_CLEAR" });
    expect(state.session.id).toBeNull();
    expect(state.diagnostics).toBeNull();
  });

  it("SHOP_FIELD patches only that field", () => {
    let state = initialState();
    state = reducer(state, { type: "SHOP_FIELD", field: "host_name", value: "Chị Hoa" });
    expect(state.draft.shop.host_name).toBe("Chị Hoa");
    expect(state.draft.shop.shop_name).toBe("Shop Nam Beauty");
  });

  it("PRODUCTS_SET replaces product subset", () => {
    let state = initialState();
    state = reducer(state, {
      type: "PRODUCTS_SET",
      value: { products: [{ id: "P1", name: "x", price: 1 }] as never[], selectedProductIds: ["P1"], productOrder: ["P1"] },
    });
    expect(state.draft.products).toHaveLength(1);
    expect(state.draft.selectedProductIds).toEqual(["P1"]);
  });

  it("EVENT_ADD prepends and caps at 200", () => {
    let state = initialState();
    for (let i = 0; i < 205; i++) {
      state = reducer(state, { type: "EVENT_ADD", value: { at: new Date().toISOString(), message: `e${i}`, tone: "info" } });
    }
    expect(state.events).toHaveLength(200);
    expect(state.events[0].message).toBe("e204");
  });

  it("unknown action returns current state", () => {
    const state = initialState();
    expect(reducer(state, { type: "UNKNOWN" as never })).toBe(state);
  });

  it("AUTO_PHASE updates phase and active", () => {
    let state = initialState();
    state = reducer(state, { type: "AUTO_PHASE", phase: "verifying", active: true });
    expect(state.autoDemo.phase).toBe("verifying");
    expect(state.autoDemo.active).toBe(true);
  });

  it("LIFECYCLE_EVENT stores on session", () => {
    let state = initialState();
    state = reducer(state, { type: "LIFECYCLE_EVENT", value: { type: "avatar.speak_started", state: "generating" } });
    expect(state.session.lifecycle?.type).toBe("avatar.speak_started");
  });
});