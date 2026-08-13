/** Fixture loading + fail-loud field-level validation.
 *
 * Loads versioned JSON fixtures and validates every required field before
 * returning/sending. Rejects partial datasets, unknown malformed fields,
 * duplicates, and reference violations. No silent defaults for required fields.
 */

import type { ProductEntity, ShopProfile } from "./api_types";

import productsJson from "./fixtures/products.json";
import shopProfilesJson from "./fixtures/shop_profiles.json";
import viewerMessagesJson from "./fixtures/viewer_messages.json";

export interface ShopProfileFixture extends ShopProfile {
  id: string;
  persona: string;
}

export interface ViewerMessageFixture {
  id: string;
  category: string;
  text: string;
}

export const SHOP_PROFILE_CATEGORIES = new Set([
  "normal_commerce",
  "product_question",
  "purchase_intent",
  "chitchat",
  "complaint",
  "spam",
  "off_topic",
  "safety",
  "comparison",
  "haggling",
  "repeat",
]);

export class FixtureError extends Error {
  constructor(message: string, public issues: string[]) {
    super(message);
    this.name = "FixtureError";
  }
}

export interface FixtureData {
  shop_profiles: ShopProfileFixture[];
  viewer_messages: ViewerMessageFixture[];
  products: ProductEntity[];
}

export function validateShopProfiles(items: unknown): ShopProfileFixture[] {
  if (!Array.isArray(items)) throw new FixtureError("shop_profiles must be an array", ["shop_profiles: not an array"]);
  const ids = new Set<string>();
  const out: ShopProfileFixture[] = [];
  const requiredStringFields = ["id", "shop_name", "host_name", "persona", "address", "phone", "selling_style"];
  for (const [index, item] of items.entries()) {
    const path = `shop_profiles[${index}]`;
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      throw new FixtureError(`${path}: must be an object`, [`${path}: not an object`]);
    }
    const record = item as Record<string, unknown>;
    for (const field of requiredStringFields) {
      if (typeof record[field] !== "string" || !(record[field] as string).trim()) {
        throw new FixtureError(`${path}.${field}: required`, [`${path}.${field}: missing`]);
      }
    }
    if (ids.has(record.id as string)) {
      throw new FixtureError(`${path}.id: duplicate ${record.id}`, [`${path}.id: duplicate`]);
    }
    ids.add(record.id as string);
    for (const [key, value] of Object.entries(record)) {
      if (!requiredStringFields.includes(key) && typeof value !== "string") {
        throw new FixtureError(`${path}.${key}: malformed field`, [`${path}.${key}: malformed`]);
      }
    }
    out.push(record as unknown as ShopProfileFixture);
  }
  if (!out.length) throw new FixtureError("shop_profiles: empty dataset", ["shop_profiles: empty"]);
  return out;
}

export function validateViewerMessages(value: unknown): ViewerMessageFixture[] {
  if (!Array.isArray(value)) throw new FixtureError("viewer_messages must be an array", ["viewer_messages: not an array"]);
  const ids = new Set<string>();
  const out: ViewerMessageFixture[] = [];
  for (const [index, item] of value.entries()) {
    const path = `viewer_messages[${index}]`;
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      throw new FixtureError(`${path}: must be an object`, [`${path}: not an object`]);
    }
    const record = item as Record<string, unknown>;
    for (const field of ["id", "category", "text"]) {
      if (typeof record[field] !== "string" || !(record[field] as string).trim()) {
        throw new FixtureError(`${path}.${field}: required`, [`${path}.${field}: missing`]);
      }
    }
    if (ids.has(record.id as string)) {
      throw new FixtureError(`${path}.id: duplicate ${record.id}`, [`${path}.id: duplicate`]);
    }
    ids.add(record.id as string);
    if (!SHOP_PROFILE_CATEGORIES.has(record.category as string)) {
      throw new FixtureError(`${path}.category: unknown ${record.category}`, [`${path}.category: unknown`]);
    }
    if (typeof (record.text as string) === "string" && (record.text as string).length > 500) {
      throw new FixtureError(`${path}.text: too long`, [`${path}.text: too long`]);
    }
    out.push({ id: record.id as string, category: record.category as string, text: record.text as string });
  }
  if (out.length < 1) throw new FixtureError("viewer_messages: empty dataset", ["viewer_messages: empty"]);
  return out;
}

const stringLimits: Record<string, number> = {
  id: 128, name: 256, description: 2000, promotion: 500, material: 256, shipping: 500, warranty: 500, ref_image: 2048,
};

export function validateProducts(value: unknown): ProductEntity[] {
  if (!Array.isArray(value)) throw new FixtureError("products must be an array", ["products: not an array"]);
  if (value.length > 100) throw new FixtureError("products: too many", ["products: too many"]);
  const ids = new Set<string>();
  const out: ProductEntity[] = [];
  for (const [index, item] of value.entries()) {
    const path = `products[${index}]`;
    if (typeof item !== "object" || item === null || Array.isArray(item)) {
      throw new FixtureError(`${path}: must be an object`, [`${path}: not an object`]);
    }
    const record = item as Record<string, unknown>;
    for (const field of ["id", "name"]) {
      if (typeof record[field] !== "string" || !(record[field] as string).trim()) {
        throw new FixtureError(`${path}.${field}: required`, [`${path}.${field}: missing`]);
      }
    }
    for (const [field, limit] of Object.entries(stringLimits)) {
      const val = record[field];
      if (val != null && typeof val !== "string") {
        throw new FixtureError(`${path}.${field}: must be string`, [`${path}.${field}: type`]);
      }
      if (typeof val === "string" && val.length > limit) {
        throw new FixtureError(`${path}.${field}: too long`, [`${path}.${field}: too long`]);
      }
    }
    if (ids.has(record.id as string)) {
      throw new FixtureError(`${path}.id: duplicate ${record.id}`, [`${path}.id: duplicate`]);
    }
    ids.add(record.id as string);
    for (const field of ["price", "original_price", "stock_total"]) {
      const val = record[field];
      if (val != null && (typeof val !== "number" || !Number.isInteger(val) || val < 0)) {
        throw new FixtureError(`${path}.${field}: non-negative integer required`, [`${path}.${field}: invalid`]);
      }
    }
    for (const field of ["colors", "sizes", "features"]) {
      const values = record[field] ?? [];
      if (!Array.isArray(values)) {
        throw new FixtureError(`${path}.${field}: must be array`, [`${path}.${field}: not array`]);
      }
      for (const [itemIndex, value] of values.entries()) {
        if (typeof value !== "string" || !value.length || value.length > 500) {
          throw new FixtureError(`${path}.${field}[${itemIndex}]: invalid item`, [`${path}.${field}[${itemIndex}]: invalid`]);
        }
      }
    }
    const price = record.price as number | null;
    const original = record.original_price as number | null;
    if (price != null && original != null && original < price) {
      throw new FixtureError(`${path}.original_price: must be >= price`, [`${path}.original_price: < price`]);
    }
    out.push(record as unknown as ProductEntity);
  }
  if (!out.length) throw new FixtureError("products: empty dataset", ["products: empty"]);
  return out;
}

export function loadFixtures(): FixtureData {
  const shop_profiles = validateShopProfiles(shopProfilesJson);
  const viewer_messages = validateViewerMessages(viewerMessagesJson);
  const products = validateProducts(productsJson);
  return { shop_profiles, viewer_messages, products };
}

export function shopProfileById(id: string): ShopProfileFixture {
  const profiles = loadFixtures().shop_profiles;
  const found = profiles.find((p) => p.id === id);
  if (!found) throw new FixtureError(`shop_profile ${id} not found`, [`shop_profiles: unknown id ${id}`]);
  return found;
}

export function productById(id: string): ProductEntity {
  const products = loadFixtures().products;
  const found = products.find((p) => p.id === id);
  if (!found) throw new FixtureError(`product ${id} not found`, [`products: unknown id ${id}`]);
  return found;
}

export { productsJson, shopProfilesJson, viewerMessagesJson };