"""Deterministic commerce routing before semantic clustering."""

from __future__ import annotations

import re
from dataclasses import replace, dataclass

from backend.application.entity.models import EntityDocument

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
    # Gender/quantity/name tokens so "pin" is never an explicit alias.
    "pin",
    "cái",
    "chiếc",
    "em",
    "chị",
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
    text: str, products: list[EntityDocument], current_product_id: str | None
) -> str | None:
    normalized = text.lower()
    matches: list[tuple[int, str]] = []
    for product in products:
        terms = [product.id.lower(), product.name.lower(), *product.aliases]
        terms.extend(
            part.lower()
            for part in product.name.split()
            if len(part) >= 3 and part.lower() not in _PRODUCT_STOPWORDS
        )
        # Legacy product.features now live in knowledge-block content.
        terms.extend(
            part.lower()
            for block in product.knowledge_blocks
            for part in block.content.split()
            if len(part) >= 3 and part.lower() not in _PRODUCT_STOPWORDS
        )
        score = sum(term in normalized for term in set(terms) if term)
        if score:
            matches.append((score, product.id))
    if matches:
        matches.sort(reverse=True)
        return matches[0][1]
    return current_product_id


def _product_terms(product: EntityDocument) -> set[str]:
    """Distinct matchable terms for one product (ids/names/aliases + words).

    Aliases are strong-reference material and are deliberately NOT filtered
    by stopwords (a user-chosen alias like "pin" is a legitimate explicit
    reference); only auto-derived name/block words are.
    """
    terms = {product.id.lower(), product.name.lower(), *(a.lower() for a in product.aliases)}
    terms.update(
        part.lower()
        for part in product.name.split()
        if len(part) >= 3 and part.lower() not in _PRODUCT_STOPWORDS
    )
    # Legacy product.features now live in knowledge-block content.
    terms.update(
        part.lower()
        for block in product.knowledge_blocks
        for part in block.content.split()
        if len(part) >= 3 and part.lower() not in _PRODUCT_STOPWORDS
    )
    return {t for t in terms if t}


def route_hints(
    text: str,
    products: list[EntityDocument],
    current_product_id: str | None = None,
) -> RoutingHints:
    """Soft pre-cluster routing hints: intent + confidence-bearing candidates.

    Classification mirrors ``route_comment`` (same phrase lists, same
    precedence: spam -> off_topic -> social). Candidates are term-count
    matches over id/name/aliases/knowledge-block words; a verbatim whole-token
    match of id/name/alias adds 2.0 (strong explicit evidence, Decision 6).
    Weak references with no explicit match stay ambiguous (empty candidates);
    ``current_product_id`` is never an implicit candidate.
    """
    lowered = text.lower().strip()
    if any(phrase in lowered for phrase in _SPAM):
        return RoutingHints(intent_candidates=["spam"], product_candidates=[])
    if any(phrase in lowered for phrase in _OFF_TOPIC):
        return RoutingHints(intent_candidates=["off_topic"], product_candidates=[])
    if any(phrase in lowered for phrase in _SOCIAL):
        return RoutingHints(intent_candidates=["social"], product_candidates=[])

    intent = "unknown"
    for candidate, phrases in _INTENT_PHRASES:
        if any(phrase in lowered for phrase in phrases):
            intent = candidate
            break

    candidates: list[tuple[str, float, str]] = []
    for product in products:
        terms = _product_terms(product)
        strong = [
            t
            for t in sorted(terms)
            if t
            in {product.id.lower(), product.name.lower(), *(a.lower() for a in product.aliases)}
            and re.search(rf"\b{re.escape(t)}\b", lowered)
        ]
        # Weak terms are name/block words ONLY: ids/names/aliases never count
        # as weak substring hits (a bare "p001" inside "P0010" is not evidence).
        weak = sorted(
            t
            for t in terms
            - {product.id.lower(), product.name.lower(), *(a.lower() for a in product.aliases)}
            if t in lowered
        )
        if not strong and not weak:
            continue
        score = float(len(strong) + len(weak))
        evidence = (
            f"explicit id/name/alias match ({', '.join(sorted(strong))})"
            if strong
            else ", ".join(weak)
        )
        if strong:
            score += 2.0
        candidates.append((product.id, score, evidence))
    return RoutingHints(
        intent_candidates=[intent] if intent != "unknown" else [],
        product_candidates=candidates,
    )


def route_comment(
    comment: Comment,
    products: list[EntityDocument],
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


@dataclass(frozen=True)
class RoutingHints:
    """Soft pre-cluster routing evidence (OpenSpec 6.1-6.2).

    ``intent_candidates`` holds the first-matching intent (empty when
    unknown); ``product_candidates`` is (product_id, score, evidence) triples
    with score = term-count + 2.0 for verbatim id/name/alias matches. Empty
    product_candidates preserves product ambiguity — the hard partition is
    gone, cluster-level resolution decides later.
    """

    intent_candidates: list[str]
    product_candidates: list[tuple[str, float, str]]
    category: str = "commerce"
    actionable: bool = True
