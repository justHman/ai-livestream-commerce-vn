"""Structured product catalog — entity-document facts + O(1) intent routing.

Two-tier retrieval (the design decision for challenge 4 + 5):

  TIER 1 — "which PRODUCT?"   : semantic cosine (cluster centroid vs product
            embedding). Needed because "áo đen này" carries no id — only
            meaning + the current-product phase target disambiguate A vs B.

  TIER 2 — "which ATTRIBUTE?" : O(1) field lookup (intent -> fact key).
            A "giá bao nhiêu?" cluster maps to a canonical fact key and is
            answered DIRECTLY from the entity's structured facts — no LLM,
            no cosine over fields. Only open-ended questions fall through
            to the LLM.

So structured facts don't replace cosine; they remove LLM/cosine work for
the common factual questions (price/ship/size/color/stock), which is the
real speed win. The catalog is a dict of ``EntityDocument`` (task 8.7); the
API layer constructs entities directly.
"""

from __future__ import annotations

from typing import Optional

from backend.application.entity.models import EntityDocument
from backend.application.entity.registry import (
    COMMERCE_PRICE_CURRENT,
    COMMERCE_PRICE_ORIGINAL,
    COMMERCE_PROMOTION,
    COMMERCE_SHIPPING,
    COMMERCE_STOCK_AVAILABLE,
    COMMERCE_STOCK_QUANTITY,
    COMMERCE_WARRANTY,
    IDENTITY_BRAND,
    IDENTITY_SKU,
)

__all__ = [
    "FACT_KEY_BY_INTENT",
    "INTENT_TO_FIELD",
    "answer_field",
    "embedding_text",
    "route_intent_to_field",
]


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

# Intent -> canonical fact key (task 8.7). "stock" reads the quantity fact
# first and falls back to the availability boolean; size/color have NO fact
# key (they are answered from knowledge blocks tagged size/color, and
# material/origin/usage/how_to_buy use the custom.* namespace).
FACT_KEY_BY_INTENT: dict[str, str] = {
    "price": COMMERCE_PRICE_CURRENT,
    "promotion": COMMERCE_PROMOTION,
    "shipping": COMMERCE_SHIPPING,
    "stock": COMMERCE_STOCK_QUANTITY,
    "material": "custom.material",
    "origin": "custom.origin",
    "usage": "custom.usage",
    "how_to_buy": "custom.how_to_buy",
    "warranty": COMMERCE_WARRANTY,
}


def _format_vnd(value: int) -> str:
    """Legacy VND display format: 350.000đ (dot thousand separators)."""
    return f"{value:,}đ".replace(",", ".")


def _block_value(entity: EntityDocument, tag: str) -> Optional[str]:
    """Join knowledge-block contents tagged ``tag`` (size/color answers)."""
    contents = [block.content for block in entity.knowledge_blocks if tag in block.tags]
    return ", ".join(contents) if contents else None


def answer_field(entity: EntityDocument, field_name: str) -> Optional[str]:
    """O(1) factual answer for a known attribute (no LLM needed).

    Reads the mapped fact(s) from the entity document; stock answers from
    the quantity fact, falling back to the availability boolean; size/color
    are answered from knowledge blocks tagged ``size``/``color``.
    """
    if field_name == "price":
        price = entity.get_fact(COMMERCE_PRICE_CURRENT)
        if price is None:
            return None
        base = _format_vnd(int(price.value))
        original = entity.get_fact(COMMERCE_PRICE_ORIGINAL)
        if original is not None and int(original.value) > int(price.value):
            og = _format_vnd(int(original.value))
            return f"Giá {entity.name}: {base} (giá gốc {og})."
        return f"Giá {entity.name}: {base}."
    if field_name == "promotion":
        promotion = entity.get_fact(COMMERCE_PROMOTION)
        return str(promotion.value) if promotion is not None else None
    if field_name == "shipping":
        shipping = entity.get_fact(COMMERCE_SHIPPING)
        return str(shipping.value) if shipping is not None else None
    if field_name == "stock":
        quantity = entity.get_fact(COMMERCE_STOCK_QUANTITY)
        if quantity is not None and int(quantity.value) > 0:
            return f"{entity.name} còn {quantity.value} sản phẩm."
        available = entity.get_fact(COMMERCE_STOCK_AVAILABLE)
        in_stock = bool(available.value) if available is not None else False
        return "Còn hàng nha!" if in_stock else "Sản phẩm tạm hết hàng ạ."
    if field_name in ("size", "color"):
        value = _block_value(entity, field_name)
        return f"{field_name.capitalize()} có: {value}" if value else None
    if field_name in ("material", "origin", "usage", "how_to_buy", "warranty"):
        key = FACT_KEY_BY_INTENT[field_name]
        fact = entity.get_fact(key)
        if fact is None:
            return None
        prefix = {"material": "Chất liệu", "origin": "Xuất xứ"}.get(field_name)
        value = str(fact.value)
        return f"{prefix}: {value}" if prefix else value
    return None


def embedding_text(entity: EntityDocument) -> str:
    """Text used to compute the entity embedding (name + tags + brand/sku + blocks).

    Mirrors the legacy ``Product.embedding_text`` surface for the Director
    runtime; ``render_entity_context(entity)`` renders the same content in a
    slightly different layout, so this compact form is kept for embeddings.
    """
    parts = [entity.name]
    if entity.tags:
        parts.append(" ".join(entity.tags))
    for key in (IDENTITY_BRAND, IDENTITY_SKU):
        fact = entity.get_fact(key)
        if fact is not None:
            parts.append(str(fact.value))
    parts.extend(block.content for block in entity.knowledge_blocks)
    return " . ".join(p for p in parts if p)


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
