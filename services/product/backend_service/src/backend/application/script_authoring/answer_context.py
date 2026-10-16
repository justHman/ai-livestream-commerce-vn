"""Conversational wrappers around complete, approved facts; no model or new claims."""

from __future__ import annotations

import re
import zlib

from .generation.ordered_units import claim_covered


# A closed grammar, not a keyword hunt: the shortcut only answers a question that IS, word for
# word, one of these shapes once greetings and the viewer's city are dropped. Anything else
# (warranty, washing, stock, refunds, "shop nhận đơn"...) takes the validated semantic path.
_FILLER = frozenset(
    "ạ nhé nha nhỉ vậy ơi shop ad admin cho hỏi xin em mình bạn anh chị à hả thế".split()
)
_GOODS = r"(?:áo|sản phẩm|mẫu|món|cái|chiếc)"
_PRICE_Q = re.compile(
    rf"(?:{_GOODS}\s+)?(?:này\s+|đó\s+|kia\s+)?"
    r"(?:(?:có\s+)?giá(?:\s+cả)?(?:\s+là)?(?:\s+(?:bao nhiêu|nhiêu|sao|thế nào))?"
    r"|(?:bao nhiêu|nhiêu) tiền|mấy đồng)"
)
_HOW_LONG = r"(?:bao lâu|mấy ngày|bao nhiêu ngày)"
_SHIP_Q = re.compile(
    rf"(?:thời gian\s+)?(?:giao hàng|giao|ship|vận chuyển)(?:\s+mất)?\s+{_HOW_LONG}"
    rf"|(?:đặt hàng\s+|đặt\s+|mua\s+)?(?:thì\s+|rồi\s+)?(?:bao lâu|mấy ngày|khi nào|bao giờ)\s+"
    r"(?:thì\s+|sẽ\s+)?(?:nhận|giao|ship)(?:\s+được)?(?:\s+hàng)?(?:\s+(?:tới|đến))?"
    rf"|(?:nhận|giao)(?:\s+được)?\s+hàng(?:\s+mất)?\s+{_HOW_LONG}"
)


_CITY_NAMES = (
    "hồ chí minh|tp hcm|tphcm|hcm|sài gòn|hà nội|hn|đà nẵng|hải phòng|cần thơ|huế|nha trang"
    "|đà lạt|vũng tàu|biên hòa|bình dương"
)


def _topic_of(question: str) -> str | None:
    """ "price" / "shipping" when every comment of the cluster is exactly such a question."""
    topics = set()
    for part in question.lower().split("|"):
        # The viewer's own city only ("em ở Hồ Chí Minh"): a closed list, so no question that
        # merely contains "ở" ("em ở xa có ship không", "shop ở đâu") is ever cut away.
        part = re.sub(rf"[,.\s]*(?:em|mình|tôi|nhà em)\s+ở\s+(?:{_CITY_NAMES})\s*[?.!]*$", "", part)
        part = re.sub(
            rf"^\s*(?:em|mình|tôi|nhà em)\s+ở\s+(?:{_CITY_NAMES})\s*(?:thì\s+)?[,.]?\s*", "", part
        )
        words = [w for w in re.findall(r"\w+", part) if w not in _FILLER]
        text = " ".join(words)
        if _PRICE_Q.fullmatch(text):
            topics.add("price")
        elif _SHIP_Q.fullmatch(text):
            topics.add("shipping")
        else:
            return None
    return topics.pop() if len(topics) == 1 else None


def factual_reply(product, question: str, completed_text: str = "") -> str | None:
    """Only exact price / delivery-time questions use this zero-model path.

    Other questions retain semantic retrieval and validation. Never infer a delivery
    promise for an individual address from a general policy, or omit its conditions.
    """
    topic = _topic_of(question)
    if topic is None:
        return None
    answers = product.answers()
    if topic == "price":
        selected = [a for a in answers if "giá " in a.lower() and "đồng" in a.lower()]
    else:
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
    remarks = (_PRICE_REMARK, _PRICE_MORE) if topic == "price" else (_SHIP_REMARK, _SHIP_MORE)
    chat = f"{remarks[0][(pick + 1) % 3]} {remarks[1][(pick + 2) % 3]}"
    return intro.format(label=label) + fact + " " + chat + " " + _INVITE[pick]


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
# Friendly talk around the fact, never a claim of its own (about the viewer or the situation only), so the
# answer is a little conversation and not a bare line.
_PRICE_REMARK = (
    "Hỏi giá trước rồi mới chọn thì đúng là cách mua sắm khôn ngoan luôn đó, mình thấy bạn rất biết cách cân nhắc.",
    "Mình hiểu ai mua đồ online cũng muốn nắm rõ chi phí trước, nên mình nói rõ để mọi người thoải mái cân nhắc nha.",
    "Giá cả rõ ràng thì mình mới thoải mái chọn lựa đúng không nè, nên mình nói thẳng luôn cho cả nhà cùng nghe.",
)
_SHIP_REMARK = (
    "Mua đồ online mà biết rõ bao lâu hàng tới thì mới sắp xếp được lịch diện đồ đúng không nè, nên mình nói kỹ để bạn dễ tính nha.",
    "Mình hiểu cảm giác chờ hàng nó sốt ruột lắm, nên biết trước thời gian giao thì lên kế hoạch diện đồ dễ hơn nhiều.",
    "Câu hỏi này rất thực tế luôn, vì ai cũng muốn biết khi nào mình được mặc món đồ mình đã chọn.",
)
_PRICE_MORE = (
    "Món nào mình chọn cũng nên vừa túi tiền và vừa ý, nên bạn cứ thong thả xem thêm phần giới thiệu của mình nha.",
    "Nếu bạn đang phân vân thì cứ nghe hết phần giới thiệu, rồi quyết định thật thoải mái nhé.",
    "Mình thích cùng các bạn tính toán kỹ trước khi chốt, như vậy mua về mới ưng cái bụng.",
)
_SHIP_MORE = (
    "Có thời gian dự kiến trong tay thì bạn dễ sắp xếp lịch diện đồ cho những buổi hẹn sắp tới lắm.",
    "Mình nói vậy để các bạn tính trước chuyện mặc đi chơi hay đi làm cho kịp lịch của mình nha.",
    "Hàng về tới tay mà đúng dịp mình cần thì mặc mới thích, đúng không nè.",
)
_NATURAL_LEAD_IN = (
    "Cảm ơn bạn đã hỏi nha, mình trả lời liền đây.",
    "À câu này mình xin trả lời luôn cho mọi người cùng nghe nhé.",
    "Mình thấy có bạn hỏi nè, để mình nói rõ cho mọi người cùng nghe nha.",
)
_NATURAL_REMARK = (
    "Câu này nhiều bạn cũng quan tâm lắm, nên mình nói kỹ một chút cho mọi người dễ hình dung.",
    "Hỏi như vậy là rất hợp lý luôn, vì mua đồ online ai cũng muốn nắm rõ trước khi quyết định.",
    "Mình thích những câu hỏi thực tế như vầy, vì nó giúp mọi người chọn đúng món mình cần.",
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


def wrap_natural(question: str, answer: str) -> str:
    """A checked natural answer, said as a short conversation: lead-in, the answer, a remark, an invitation.

    The model's text is untouched; the sentences around it are fixed and carry no claim."""
    pick = zlib.crc32(question.encode("utf-8")) % 3
    return f"{_NATURAL_LEAD_IN[pick]} {answer.strip()} {_NATURAL_REMARK[(pick + 1) % 3]} {_INVITE[pick]}"
