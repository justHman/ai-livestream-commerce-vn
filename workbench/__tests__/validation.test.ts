/** Product/draft validation tests — mirrors backend ProductIn constraints. */

import { describe, expect, it } from "vitest";

import { validateProductCatalog, validateShopLimits, productJson } from "../src/validation";
import { loadFixtures } from "../src/fixtures";

describe("validateProductCatalog", () => {
  it("accepts canonical fixture products", () => {
    const { products } = loadFixtures();
    expect(validateProductCatalog(products)).toEqual([]);
  });

  it("rejects duplicate ids", () => {
    const { products } = loadFixtures();
    const bad = structuredClone(products);
    (bad[1] as Record<string, unknown>).id = (bad[0] as Record<string, unknown>).id;
    expect(validateProductCatalog(bad as never)).toContain("products[1].id: trùng ID P004.");
  });

  it("rejects missing name", () => {
    const { products } = loadFixtures();
    const bad = structuredClone(products);
    delete (bad[0] as Record<string, unknown>).name;
    expect(validateProductCatalog(bad as never)).toContain("products[0].name: bắt buộc.");
  });

  it("rejects original_price < price", () => {
    const { products } = loadFixtures();
    const bad = structuredClone(products);
    (bad[0] as Record<string, unknown>).price = 500;
    (bad[0] as Record<string, unknown>).original_price = 100;
    expect(validateProductCatalog(bad as never)).toContain("products[0].original_price: phải lớn hơn hoặc bằng price.");
  });

  it("rejects >100 products", () => {
    const products = Array.from({ length: 101 }, (_, i) => ({
      id: `P${i}`, name: "x", description: "", price: 1, original_price: null, promotion: "",
      colors: [], sizes: [], material: "", shipping: "", warranty: "", in_stock: true,
      stock_total: null, ref_image: "", features: [],
    }));
    expect(validateProductCatalog(products)).toContain("products: tối đa 100 sản phẩm.");
  });
});

describe("validateShopLimits", () => {
  it("passes valid shop", () => {
    expect(validateShopLimits({ shop_name: "A", host_name: "B", address: "C", phone: "D", selling_style: "E" })).toEqual([]);
  });

  it("rejects over-long phone", () => {
    const issues = validateShopLimits({ shop_name: "A", host_name: "B", address: "C", phone: "1".repeat(65), selling_style: "E" });
    expect(issues.some((i) => i.includes("shop_profile.phone"))).toBe(true);
  });
});

describe("productJson", () => {
  it("serializes deterministically", () => {
    const { products } = loadFixtures();
    expect(productJson(products)).toBe(productJson(products));
  });
});