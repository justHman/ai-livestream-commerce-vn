"""Structured product catalog — production schema + O(1) attribute routing.

Two-tier retrieval (the design decision for challenge 4 + 5):

  TIER 1 — "which PRODUCT?"   : semantic cosine (cluster centroid vs product
            embedding). Needed because "áo đen này" carries no id — only
            meaning + the current-product phase target disambiguate A vs B.

  TIER 2 — "which ATTRIBUTE?" : O(1) field lookup (intent -> field name).
            A "giá bao nhiêu?" cluster maps to the `price` field and is answered
            DIRECTLY from structured data — no LLM, no cosine over fields. Only
            open-ended questions fall through to the LLM.

So structured fields don't replace cosine; they remove LLM/cosine work for the
common factual questions (price/ship/size/color/stock), which is the real speed win.

The schema below is intentionally rich to avoid missing live-commerce edge cases
(variants, stock, shipping, warranty, media, limits). Extend `attributes` for
anything domain-specific without code changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# Vietnamese intent phrases -> canonical attribute field. Drives O(1) routing.
# (Lexical trigger only; the semantic product match stays cosine-based.)
INTENT_TO_FIELD: dict[str, list[str]] = {
    "price": ["giá", "bao nhiêu", "nhiêu tiền", "mấy đồng", "giá sale", "giá gốc"],
    "promotion": ["khuyến mãi", "sale", "giảm", "deal", "voucher", "mã giảm", "ưu đãi"],
    "shipping": ["ship", "giao hàng", "vận chuyển", "freeship", "phí ship", "bao lâu nhận"],
    "stock": ["còn hàng", "còn không", "hết chưa", "số lượng", "available"],
    "size": ["size", "kích cỡ", "size nào", "cao bao nhiêu", "nặng bao nhiêu", "vừa không"],
    "color": ["màu", "color", "màu nào", "mấy màu"],
    "material": ["chất liệu", "vải gì", "làm bằng gì", "thành phần", "chất"],
    "warranty": ["bảo hành", "đổi trả", "hoàn tiền", "guarantee"],
    "usage": ["cách dùng", "sử dụng", "công dụng", "dùng sao", "hướng dẫn"],
    "origin": ["xuất xứ", "nước nào", "hàng nội", "hàng nhập", "made in"],
    "how_to_buy": ["mua sao", "đặt hàng", "chốt đơn", "cách mua", "order"],
}


@dataclass
class ProductVariant:
    """A concrete buyable variant (size/color combination)."""

    sku: str
    color: Optional[str] = None
    size: Optional[str] = None
    price: Optional[int] = None        # VND; overrides product price if set
    stock: int = 0


@dataclass
class Product:
    """Structured product record (production schema)."""

    # identity
    id: str
    name: str
    description: str = ""

    # commercial
    price: Optional[int] = None            # VND, base/display price
    original_price: Optional[int] = None
    currency: str = "VND"
    promotion: Optional[str] = None        # human-readable promo text
    discount_percent: Optional[float] = None

    # physical / catalog attributes
    brand: Optional[str] = None
    category: Optional[str] = None
    colors: list[str] = field(default_factory=list)
    sizes: list[str] = field(default_factory=list)
    material: Optional[str] = None
    origin: Optional[str] = None
    features: list[str] = field(default_factory=list)

    # commerce ops
    variants: list[ProductVariant] = field(default_factory=list)
    in_stock: bool = True
    stock_total: Optional[int] = None
    shipping: Optional[str] = None         # e.g. "Freeship đơn từ 200k, 2-4 ngày"
    warranty: Optional[str] = None
    how_to_buy: Optional[str] = None
    usage: Optional[str] = None
    purchase_limit: Optional[int] = None   # max per buyer (flash-sale edge case)

    # media / presentation
    ref_image: Optional[str] = None        # avatar asset keyed by product id
    images: list[str] = field(default_factory=list)

    # free-form extension — add domain fields without schema changes
    attributes: dict[str, Any] = field(default_factory=dict)

    # filled by the embedder (TIER 1 semantic retrieval)
    embedding: Optional[list[float]] = None

    def embedding_text(self) -> str:
        """Text used to compute the product embedding (name + desc + key attrs)."""
        parts = [self.name, self.description]
        if self.brand:
            parts.append(self.brand)
        if self.category:
            parts.append(self.category)
        if self.colors:
            parts.append("màu " + ", ".join(self.colors))
        if self.material:
            parts.append("chất liệu " + self.material)
        parts.extend(self.features)
        return " . ".join(p for p in parts if p)

    def answer_field(self, field_name: str) -> Optional[str]:
        """O(1) factual answer for a known attribute (no LLM needed)."""
        k = field_name
        if k == "price":
            if self.price is None:
                return None
            base = f"{self.price:,}đ".replace(",", ".")
            if self.original_price and self.original_price > self.price:
                og = f"{self.original_price:,}đ".replace(",", ".")
                return f"Giá {self.name}: {base} (giá gốc {og})."
            return f"Giá {self.name}: {base}."
        if k == "promotion":
            return self.promotion or (f"Đang giảm {self.discount_percent:.0f}%."
                                      if self.discount_percent else None)
        if k == "shipping":
            return self.shipping
        if k == "stock":
            if self.stock_total is not None:
                return f"{self.name} còn {self.stock_total} sản phẩm."
            return "Còn hàng nha!" if self.in_stock else "Sản phẩm tạm hết hàng ạ."
        if k == "size":
            return ("Size có: " + ", ".join(self.sizes)) if self.sizes else None
        if k == "color":
            return ("Màu có: " + ", ".join(self.colors)) if self.colors else None
        if k == "material":
            return (f"Chất liệu: {self.material}") if self.material else None
        if k == "warranty":
            return self.warranty
        if k == "usage":
            return self.usage
        if k == "origin":
            return (f"Xuất xứ: {self.origin}") if self.origin else None
        if k == "how_to_buy":
            return self.how_to_buy
        return self.attributes.get(k)


def route_intent_to_field(text: str) -> Optional[str]:
    """O(1)-ish lexical map from a comment to a canonical attribute field.

    Returns the field name (e.g. 'price') or None if it's not a simple factual
    attribute question (then the Director routes to the LLM).
    """
    t = text.lower()
    for field_name, phrases in INTENT_TO_FIELD.items():
        if any(p in t for p in phrases):
            return field_name
    return None
