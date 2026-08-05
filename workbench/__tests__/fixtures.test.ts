/** Fixture validation tests — fail-loud field-level checks. */

import { describe, expect, it } from "vitest";

import {
  loadFixtures,
  validateProducts,
  validateShopProfiles,
  validateViewerMessages,
  FixtureError,
  shopProfileById,
  productById,
} from "../src/fixtures";

describe("fixtures valid full-session load", () => {
  it("loads all fixture categories without throwing", () => {
    expect(() => loadFixtures()).not.toThrow();
  });

  it("products satisfy canonical schema with pricing/inventory/features", () => {
    const { products } = loadFixtures();
    expect(products.length).toBeGreaterThanOrEqual(3);
    for (const product of products) {
      expect(product.id).toBeTruthy();
      expect(product.name).toBeTruthy();
      expect(typeof product.price).toBe("number");
      expect(typeof product.in_stock).toBe("boolean");
      expect(Array.isArray(product.features)).toBe(true);
      if (product.original_price != null && product.price != null) {
        expect(product.original_price).toBeGreaterThanOrEqual(product.price);
      }
    }
  });

  it("viewer messages cover all required categories", () => {
    const { viewer_messages } = loadFixtures();
    const required = ["normal_commerce", "product_question", "purchase_intent", "complaint", "spam", "off_topic", "safety"];
    const seen = new Set(viewer_messages.map((m) => m.category));
    for (const category of required) expect(seen.has(category)).toBe(true);
  });

  it("shop profiles include shop/host/persona fields", () => {
    const { shop_profiles } = loadFixtures();
    for (const profile of shop_profiles) {
      expect(profile.shop_name).toBeTruthy();
      expect(profile.host_name).toBeTruthy();
      expect(profile.persona).toBeTruthy();
      expect(profile.selling_style).toBeTruthy();
    }
  });

  it("shop profile presets support preset and custom values", () => {
    const { shop_profiles } = loadFixtures();
    const byId = new Map(shop_profiles.map((p) => [p.id, p]));
    expect(byId.has("beauty")).toBe(true);
    expect(byId.has("fashion")).toBe(true);
    expect(byId.has("gadget")).toBe(true);
    for (const id of ["beauty", "fashion", "gadget"]) {
      const profile = byId.get(id)!;
      expect(profile.shop_name).toBeTruthy();
      expect(profile.host_name).toBeTruthy();
      expect(profile.address).toBeTruthy();
      expect(profile.phone).toBeTruthy();
      expect(profile.selling_style).toBeTruthy();
      // Each preset maps to all five shop-draft fields consumed by attach.
      for (const field of ["shop_name", "host_name", "address", "phone", "selling_style"]) {
        expect(typeof (profile as Record<string, unknown>)[field]).toBe("string");
      }
    }
    // Custom values remain expressible: a draft shop is not restricted to preset ids.
    expect(byId.has("custom")).toBe(false);
  });
});

describe("fixtures deterministic ordering", () => {
  it("message ids are unique and in order", () => {
    const { viewer_messages } = loadFixtures();
    const ids = new Set(viewer_messages.map((m) => m.id));
    expect(ids.size).toBe(viewer_messages.length);
  });
});

describe("fail-loud corruption rejection", () => {
  it("rejects a product missing required id", () => {
    const { products } = loadFixtures();
    const bad = structuredClone(products);
    delete (bad[0] as Record<string, unknown>).id;
    expect(() => validateProducts(bad)).toThrow(FixtureError);
  });

  it("rejects negative price", () => {
    const { products } = loadFixtures();
    const bad = structuredClone(products);
    (bad[0] as Record<string, unknown>).price = -5;
    expect(() => validateProducts(bad)).toThrow(/non-negative integer/);
  });

  it("rejects original_price < price", () => {
    const { products } = loadFixtures();
    const bad = structuredClone(products);
    (bad[0] as Record<string, unknown>).original_price = 10;
    (bad[0] as Record<string, unknown>).price = 100;
    expect(() => validateProducts(bad)).toThrow(/original_price/);
  });

  it("rejects duplicate product ids", () => {
    const { products } = loadFixtures();
    const bad = structuredClone(products);
    (bad[1] as Record<string, unknown>).id = (bad[0] as Record<string, unknown>).id;
    expect(() => validateProducts(bad)).toThrow(/duplicate/);
  });

  it("rejects unknown viewer message category", () => {
    const { viewer_messages } = loadFixtures();
    const bad = structuredClone(viewer_messages);
    (bad[0] as Record<string, unknown>).category = "alien_category";
    expect(() => validateViewerMessages(bad)).toThrow(/unknown/);
  });

  it("rejects partial dataset (empty arrays)", () => {
    expect(() => validateShopProfiles([])).toThrow(/empty/);
    expect(() => validateViewerMessages([])).toThrow(/empty/);
    expect(() => validateProducts([])).toThrow(/empty/);
  });

  it("rejects non-object items", () => {
    expect(() => validateProducts([1, 2])).toThrow(FixtureError);
  });
});

describe("lookups", () => {
  it("shopProfileById resolves existing", () => {
    const profile = shopProfileById("beauty");
    expect(profile.shop_name).toBeTruthy();
  });

  it("shopProfileById throws on unknown", () => {
    expect(() => shopProfileById("nope")).toThrow(FixtureError);
  });

  it("productById resolves P001", () => {
    const product = productById("P001");
    expect(product.name).toContain("Kem chống nắng");
  });
});