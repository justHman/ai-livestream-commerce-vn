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
    "Mua đồ online thì giá là điều ai cũng muốn nắm rõ trước tiên, nên mình nói luôn để cả nhà tiện theo dõi.",
    "Câu hỏi về giá nhiều bạn cũng đang chờ nghe, nên mình trả lời thẳng luôn cho mọi người cùng biết.",
    "Có giá rõ ràng rồi thì mọi người dễ so sánh và cân nhắc hơn nhiều.",
)
_PRICE_MORE = (
    "Mình nói giá trước, còn các thông tin khác về sản phẩm thì mình sẽ giới thiệu tiếp ngay phía sau nha.",
    "Bạn nào còn muốn xem thêm chi tiết thì cứ nghe tiếp phần giới thiệu của mình nhé.",
    "Giá chỉ là một phần thôi, mình sẽ cùng mọi người xem tiếp các điểm khác của sản phẩm nữa.",
)
_SHIP_REMARK = (
    "Mua đồ online mà biết rõ bao lâu hàng tới thì mới sắp xếp được lịch nhận đúng không nè, nên mình nói kỹ để bạn dễ tính nha.",
    "Mình hiểu cảm giác chờ hàng nó sốt ruột lắm, nên biết trước thời gian giao thì chủ động hơn nhiều.",
    "Câu hỏi này rất thực tế luôn, vì ai cũng muốn biết khi nào mình nhận được món đồ mình đã chọn.",
)
_SHIP_MORE = (
    "Có thời gian dự kiến trong tay thì bạn dễ sắp xếp lịch ở nhà để nhận hàng hơn nhiều.",
    "Mình nói vậy để các bạn tính trước chuyện nhận hàng cho kịp lịch của mình nha.",
    "Hàng về tới tay đúng lúc mình cần thì dùng mới thích, đúng không nè.",
)
_NATURAL_LEAD_IN = (
    "Cảm ơn bạn đã hỏi nha, mình trả lời liền đây.",
    "À câu này mình xin trả lời luôn cho mọi người cùng nghe nhé.",
    "Mình thấy có bạn hỏi nè, để mình nói rõ cho mọi người cùng nghe nha.",
)
_NATURAL_REMARK = (
    "Câu này nhiều bạn cũng quan tâm, nên mình nói kỹ một chút cho mọi người dễ hình dung.",
    "Mua đồ online thì ai cũng muốn nắm rõ chuyện này trước khi quyết định.",
    "Mình trả lời công khai để các bạn khác đang xem cũng nắm được luôn nha.",
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


# Words that never tie a question to the product: "không" or "biết" appear in any fact and in any chit-chat.
_STOP = frozenset(
    "không có biết được là và của thì mà nào gì sao đâu cho với này kia đó vậy nhé nha ạ à hả thế "
    "anh chị em bạn mình shop ad admin ơi cái những các một hai ba bao nhiêu".split()
)
# A viewer's words are NEVER spoken back in the shop's voice: no list of words can prove that arbitrary text
# holds no price, fee, promise, insult or personal detail. The fallback only says WHAT KIND of question it is.
#
# "Off topic" needs a positive sign: a question put to the host as a person (anh/chị/em/bạn/MC...) about
# something personal, with no shopping word and no content word of the approved facts. Anything else that the
# facts cannot answer is a shop question and goes to the shop, so "có chống nước không" is never "ngoài lề".
_SHOP_WORDS = (
    "giá|bao nhiêu|size|kích cỡ|màu|chất liệu|vải|giao|ship|vận chuyển|nhận hàng|đổi|trả|bảo hành"
    "|hoàn tiền|cod|thanh toán|khuyến mãi|giảm giá|giảm|ưu đãi|còn hàng|hết hàng|còn size|còn màu"
    "|còn không|còn áo|còn mẫu|còn bao nhiêu|có sẵn|sẵn hàng|trong kho|kho hàng|mua|đặt|sản phẩm|mẫu"
    "|giặt|cân nặng|chiều cao|chính hãng|áo|quần|giày|túi|hóa đơn|hàng|freeship|miễn phí|phí|tiền"
    "|trả góp|order|inbox|tư vấn|liên hệ|địa chỉ|ở đâu|cửa hàng|showroom|bao lâu|mấy ngày|tặng|quà"
    "|voucher|mã giảm|chiết khấu|cam kết|đảm bảo|bảo đảm|hứa|khuyến|lừa|uy tín|thật|giả"
    "|cái này|cái đó|cái kia|món này|món đó|mẫu này|đồ này|loại này|chiếc này|bộ này|hàng này"
)
_COMMERCE = re.compile(rf"(?<!\w)(?:{_SHOP_WORDS})(?!\w)", re.IGNORECASE)
_HOST = re.compile(r"(?<!\w)(?:anh|chị|em|bạn|mc|kol|ad|admin)(?!\w)", re.IGNORECASE)
_PERSONAL = re.compile(
    r"(?<!\w)(?:biết|thích|ăn|uống|ngủ|hát|nhảy|múa|đàn|tuổi|độc thân|người yêu|bạn trai|bạn gái|vợ"
    r"|chồng|gia đình|quê|sống|học|làm nghề|nghề|khoẻ|khỏe|vui|buồn|mệt|đẹp trai|xinh|tên|chơi|đi đâu"
    r"|rảnh|bận|làm gì|ở nhà|cười|yêu|ghét|đói|phim|nhạc|bài hát|game|bóng đá|du lịch|ngày mai|cuối tuần)"
    r"(?!\w)",
    re.IGNORECASE,
)
_ASKING = re.compile(
    r"\?|(?<!\w)(?:không|chưa|hả|à|nhỉ|sao|gì|nào|đâu|chứ|ko|k)(?!\w)", re.IGNORECASE
)


def is_off_topic(question: str, facts: list[str]) -> bool:
    """A question put to the host as a person, with no shopping word and no content word of the facts.

    Real viewers rarely type a '?', so a question is any interrogative word. A wrong guess only changes
    the wording: both fallbacks send a shop question to the shop and repeat nothing the viewer wrote."""
    lowered = question.split("|")[0].strip().lower()
    if _COMMERCE.search(lowered) or not _HOST.search(lowered):
        return False
    if not (_PERSONAL.search(lowered) or _ASKING.search(lowered)):
        return False
    words = {w for w in re.findall(r"\w+", lowered) if len(w) > 2 and w not in _STOP}
    known = set(re.findall(r"\w+", " ".join(facts).lower()))
    return not (words & known)


_OFF_TOPIC_DEFLECTION = (
    "Mình thấy có bạn hỏi một chuyện hơi ngoài lề so với phần giới thiệu hôm nay. "
    "Nếu bạn cần hỏi về sản phẩm thì nhắn tin cho shop để được hỗ trợ kỹ hơn nha, còn chuyện này "
    "thì mình xin phép không trả lời sâu, cảm ơn bạn đã ghé tương tác với mình. "
    "Giờ mình quay lại giới thiệu tiếp cho mọi người nhé."
)
_UNKNOWN_DEFLECTION = (
    "Mình thấy có bạn hỏi một câu rất cụ thể về sản phẩm. Câu này mình chưa có thông tin chính xác "
    "trong phần giới thiệu để trả lời, bạn nhắn tin cho shop để được hỗ trợ kỹ hơn nha. "
    "Cảm ơn bạn đã quan tâm, còn bây giờ mình tiếp tục giới thiệu cho mọi người nhé."
)


def deflection_for(question: str | None, facts: list[str]) -> str:
    """Said when the approved facts cannot answer. It says what kind of question this is (a chat question
    outside the shop talk, or a shop question the introduction does not cover) and goes back to the
    product. It repeats nothing the viewer wrote."""
    from ..platform_events.comment_intent import is_non_question
    from .approved_speech import SpeechRejected

    if question and is_non_question(question):
        raise SpeechRejected("non_question")
    if (question or "").strip() and is_off_topic(question or "", facts):
        # Even a wrongly classified shop question is still sent to the shop by this text.
        return _OFF_TOPIC_DEFLECTION
    return _UNKNOWN_DEFLECTION


def is_deflection(text: str | None) -> bool:
    """Whether a spoken answer is one of the fixed "cannot answer" texts (not an answer)."""
    from .approved_speech import DEFLECTION

    return text in (DEFLECTION, _OFF_TOPIC_DEFLECTION, _UNKNOWN_DEFLECTION)
