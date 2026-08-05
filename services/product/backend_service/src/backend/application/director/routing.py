"""Deterministic commerce routing before semantic clustering."""

from __future__ import annotations

from dataclasses import replace

from .catalog import Product
from .clustering import Comment


_INTENT_PHRASES: list[tuple[str, tuple[str, ...]]] = [
    (
        "complaint",
        ("giao sai", "bị lỗi", "bị rát", "giao chậm", "móp", "đổi size", "không phản hồi"),
    ),
    ("buy_intent", ("chốt", "đặt hàng", "đặt ", "mua ngay", "lấy ", "thanh toán")),
    ("promotion", ("khuyến mãi", "giảm", "sale", "voucher", "deal", "ưu đãi")),
    ("shipping", ("ship", "giao hàng", "vận chuyển", "freeship", "bao lâu nhận")),
    ("stock", ("còn hàng", "còn không", "hết chưa", "về hàng")),
    ("size_color", ("size", "cỡ", "kích cỡ", "màu", "vừa không")),
    ("price", ("giá", "bao nhiêu", "nhiêu tiền", "mấy đồng")),
    ("comparison", ("so với", "khác gì", "cái nào")),
    ("usage", ("cách dùng", "dùng sao", "sử dụng", "công dụng", "phù hợp", "dùng được")),
]
_SOCIAL = ("chào", "hello", "hi ", "like", "follow", "thả tim", "tym", "đẹp quá")
_SPAM = ("bit.ly", "t.me/", "inbox em", "bên shop em", "link mua", "follow em")
_PRODUCT_STOPWORDS = {
    "cho",
    "của",
    "hàng",
    "màu",
    "này",
    "shop",
    "sản",
    "phẩm",
    "trắng",
}
_OFF_TOPIC = (
    "bóng đá",
    "bầu cử",
    "bitcoin",
    "crypto",
    "biển đông",
    "world cup",
    "thời tiết",
    "nhà hàng",
)


def _route_product(
    text: str, products: list[Product], current_product_id: str | None
) -> str | None:
    normalized = text.lower()
    matches: list[tuple[int, str]] = []
    for product in products:
        terms = [product.id.lower(), product.name.lower()]
        terms.extend(
            part.lower()
            for part in product.name.split()
            if len(part) >= 3 and part.lower() not in _PRODUCT_STOPWORDS
        )
        terms.extend(feature.lower() for feature in product.features)
        score = sum(term in normalized for term in set(terms) if term)
        if score:
            matches.append((score, product.id))
    if matches:
        matches.sort(reverse=True)
        return matches[0][1]
    return current_product_id


def route_comment(
    comment: Comment,
    products: list[Product],
    current_product_id: str | None,
) -> Comment:
    """Classify one comment and bind its commerce partition."""
    text = comment.text.lower().strip()
    if any(phrase in text for phrase in _SPAM):
        return replace(comment, category="spam", intent="spam", actionable=False)
    if any(phrase in text for phrase in _OFF_TOPIC):
        return replace(comment, category="off_topic", intent="off_topic", actionable=False)
    if any(phrase in text for phrase in _SOCIAL):
        return replace(comment, category="social", intent="social", actionable=False)

    intent = "unknown"
    for candidate, phrases in _INTENT_PHRASES:
        if any(phrase in text for phrase in phrases):
            intent = candidate
            break
    return replace(
        comment,
        category="commerce",
        intent=intent,
        product_id=(
            _route_product(text, products, current_product_id)
            if intent != "unknown"
            else _route_product(text, products, None)
        ),
        actionable=True,
    )
