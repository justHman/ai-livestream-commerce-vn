/** Product/draft validation — mirrors backend ProductIn constraints. */

import type { Product } from "./api_types";

const stringLimits: Record<string, number> = {
  id: 128, name: 256, description: 2000, promotion: 500, material: 256, shipping: 500, warranty: 500, ref_image: 2048,
};

export function validateProductCatalog(products: Product[]): string[] {
  const errors: string[] = [];
  if (!Array.isArray(products)) return ["products: phải là JSON array."];
  if (products.length > 100) errors.push("products: tối đa 100 sản phẩm.");
  const ids = new Set<string>();
  products.forEach((product, index) => {
    if (!product || typeof product !== "object" || Array.isArray(product)) {
      errors.push(`products[${index}]: phải là object.`);
      return;
    }
    const record = product as unknown as Record<string, unknown>;
    for (const [field, limit] of Object.entries(stringLimits)) {
      const value = record[field];
      if ((field === "id" || field === "name") && (typeof value !== "string" || !value.trim())) {
        errors.push(`products[${index}].${field}: bắt buộc.`);
      } else if (value != null && typeof value !== "string") {
        errors.push(`products[${index}].${field}: phải là chuỗi.`);
      } else if (typeof value === "string" && value.length > limit) {
        errors.push(`products[${index}].${field}: tối đa ${limit} ký tự.`);
      }
    }
    if (typeof product.id === "string") {
      if (ids.has(product.id)) errors.push(`products[${index}].id: trùng ID ${product.id}.`);
      ids.add(product.id);
    }
    for (const field of ["price", "original_price", "stock_total"]) {
      const value = record[field];
      if (value != null && (typeof value !== "number" || !Number.isInteger(value) || value < 0)) {
        errors.push(`products[${index}].${field}: phải là số nguyên không âm.`);
      }
    }
    if (product.price != null && product.original_price != null && product.original_price < product.price) {
      errors.push(`products[${index}].original_price: phải lớn hơn hoặc bằng price.`);
    }
    for (const field of ["colors", "sizes", "features"]) {
      const values = record[field] ?? [];
      if (!Array.isArray(values)) {
        errors.push(`products[${index}].${field}: phải là array.`);
      } else {
        if (values.length > 32) errors.push(`products[${index}].${field}: tối đa 32 phần tử.`);
        values.forEach((value, itemIndex) => {
          if (typeof value !== "string" || !value.length || value.length > 500) {
            errors.push(`products[${index}].${field}[${itemIndex}]: chuỗi 1-500 ký tự.`);
          }
        });
      }
    }
  });
  return errors;
}

export function productJson(products: Product[]): string {
  return JSON.stringify(products, null, 2);
}

export function validateShopLimits(shop: Record<string, unknown>): string[] {
  const errors: string[] = [];
  const shopLimits: Record<string, number> = { shop_name: 256, host_name: 128, address: 500, phone: 64, selling_style: 1000 };
  for (const [field, limit] of Object.entries(shopLimits)) {
    if (typeof shop[field] !== "string" || (shop[field] as string).length > limit) {
      errors.push(`shop_profile.${field}: tối đa ${limit} ký tự.`);
    }
  }
  return errors;
}