"""Conversational wrappers around complete, approved facts; no model or new claims."""

from __future__ import annotations

import re
import zlib

from .generation.ordered_units import claim_covered


def factual_reply(product, question: str, completed_text: str = "") -> str | None:
    """Only unambiguous price/delivery questions use this zero-model path.

    Other questions retain semantic retrieval and validation. Never infer a delivery
    promise for an individual address from a general policy, or omit its conditions.
    """
    from backend.application.director.routing import route_hints

    intent = route_hints(question, []).intent_candidates
    if len(intent) != 1 or intent[0] not in ("price", "shipping"):
        return None
    topic = intent[0]
    # Mixed questions and personalised guarantees need the existing semantic path.
    if re.search(r"bảo đảm|chắc chắn|kịp|địa chỉ|ngày mai|hôm nay", question, re.I):
        return None
    answers = product.answers()
    if topic == "price":
        if re.search(r"giá gốc|giảm|sale|ship|giao hàng|mua .*giá", question, re.I):
            return None
        selected = [a for a in answers if "giá " in a.lower() and "đồng" in a.lower()]
    else:
        if re.search(
            r"phí|freeship|miễn phí|giá|bao nhiêu tiền|trả tiền|thanh toán|cod|hàng lại|còn hàng",
            question,
            re.I,
        ):
            return None
        selected = [a for a in answers if re.search(r"giao hàng|vận chuyển|ship", a, re.I)]
    if not selected or len(selected) > (1 if topic == "price" else 3):
        return None  # ambiguous/missing policy: don't guess or widen the approved answer
    fact = " ".join(a.strip().rstrip(".!?") + "." for a in selected)
    if len(fact) > 600:
        return None
    label = "giá" if topic == "price" else "thời gian giao hàng"
    said = all(
        any(claim_covered(a, part) for part in completed_text.split("\n\n") if part)
        for a in selected
    )
    # A fixed set of wordings (picked by the question, so the same question repeats itself but a
    # live is not one template over and over). They only greet, invite and echo the viewer's
    # city; every claim is the approved fact above, and nothing refers to the script.
    pick = zlib.crc32(question.encode("utf-8")) % len(_LEAD_IN)
    city = _city(question) if topic == "shipping" else None
    if city:
        intro = f"Bạn ở {city} đúng không nè, mình nói rõ cho bạn luôn nha. "
    else:
        intro = _LEAD_IN_SAID[pick] if said else _LEAD_IN[pick]
    return intro.format(label=label) + fact + " " + _INVITE[pick]


_LEAD_IN = (
    "Mình thấy có bạn hỏi về {label}, mình trả lời luôn cho mọi người tiện nha. ",
    "Cảm ơn bạn đã hỏi nha, về {label} thì mình nói ngay đây. ",
    "À câu hỏi về {label} nè, mình trả lời liền cho mọi người nhé. ",
)
_LEAD_IN_SAID = (
    "Mình nói lại về {label} cho các bạn mới vào tiện theo dõi nha. ",
    "Bạn nào mới vào thì mình nhắc lại {label} một chút nhé. ",
    "Về {label} thì mình nói lại cho mọi người cùng nghe nha. ",
)
_INVITE = (
    "Bạn nào còn băn khoăn điều gì về sản phẩm thì cứ bình luận, mình trả lời liền nhé.",
    "Mọi người cần hỏi thêm gì cứ nhắn mình, mình tư vấn kỹ cho từng bạn nha.",
    "Còn thắc mắc gì thì mọi người cứ thoải mái hỏi tiếp nhé.",
)


def _city(question: str) -> str | None:
    """Only a city the viewer says they live in; "giao tới Hà Nội" names a destination, not them."""
    named = [
        n for n, pattern in _CITIES if re.search(rf"(?<!\w)(?:{pattern})(?!\w)", question, re.I)
    ]
    lives = [
        n for n, pattern in _CITIES if re.search(rf"(?<!\w)ở\s+(?:{pattern})(?!\w)", question, re.I)
    ]
    return lives[0] if len(named) == 1 and lives else None


_CITIES = (
    ("Hồ Chí Minh", r"hồ chí minh|hcm|tphcm|sài gòn"),
    ("Hà Nội", r"hà nội|hn"),
)
