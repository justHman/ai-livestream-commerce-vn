"""Conversational wrappers around complete, approved facts; no model or new claims."""

from __future__ import annotations

import re

from .generation.ordered_units import claim_covered


def factual_reply(
    product, question: str, completed_text: str = "", future_text: str = ""
) -> str | None:
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
        if re.search(r"phí|freeship|miễn phí|giá|bao nhiêu tiền", question, re.I):
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
    if said:
        intro = f"Mình nhắc lại phần {label} để các bạn mới vào cũng tiện theo dõi nhé. "
        tail = " Các bạn cứ hỏi thêm nếu còn điều gì muốn mình giải thích nha."
    else:
        intro = f"Mình trả lời câu hỏi về {label} của các bạn trước nhé. "
        # No promise about a future script passage whose cursor could change.
        tail = " Mọi người cứ theo dõi cùng mình và hỏi thêm nếu cần nhé."
        if topic == "price" and any(
            claim_covered(selected[0], part) for part in future_text.split("\n\n") if part
        ):
            tail = " Lát nữa tới phần giá trong kịch bản, mình sẽ nhắc lại để mọi người dễ theo dõi nhé."
    return intro + fact + tail
