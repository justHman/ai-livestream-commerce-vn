"""Conversational wrappers around complete, approved facts; no model or new claims."""

from __future__ import annotations

import re
import unicodedata
import zlib
from functools import lru_cache

from ..safety_gate.text_matching import fold_text

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
# Personal host questions need positive evidence too; unrelated text is silent.
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

_SHOP_CONTEXT = re.compile(
    r"\b(?:gia|bao nhieu tien|size|kich co|mau|chat lieu|vai|ship|giao hang|van chuyen|nhan hang|doi tra|"
    r"bao hanh|chinh sach|hoan tien|cod|thanh toan|khuyen mai|"
    r"giam gia|uu dai|con hang|het hang|con size|con mau|co san|san hang|kho hang|"
    r"mua|dat hang|san pham|giat|can nang|chieu cao|chinh hang|ao|quan|giay|tui|"
    r"vay|mu|dong ho|my pham|serum|son|hoa don|hang|freeship|tra gop|order|tu van|cua hang|showroom|"
    r"voucher|ma giam|chiet khau|chong nuoc|chong nang|cai nay|cai do|cai kia|"
    r"mon nay|mon do|mau nay|do nay|loai nay|chiec nay|bo nay|hang nay|shop)\b"
)
_SHOP_REQUEST = re.compile(
    r"\b(?:gia|bao nhieu|size|kich co|mau|ship|mua|dat hang|chot|tu van|xin link|"
    r"con hang|chat lieu|chinh sach|doi tra|bao hanh|khuyen mai|giam gia|uu dai)\b"
)
_PRODUCT_NOUNS = re.compile(r"\b(?:ao|quan|giay|tui|vay|mu|dong ho|my pham|serum|son)\b")
_FOLDED_ASKING = re.compile(
    fold_text(_ASKING.pattern) + r"|\b(?:may|bao nhieu|bao lau|khi nao|bao gio)\b", re.IGNORECASE
)


def _commerce_view(text: str) -> str:
    # Keep explicit non-commerce meanings before accent folding (mưa != mua,
    # máu != màu, gia đình/quốc gia != giá). Other shopping words survive.
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"\b(?:mưa|máu|mù|vậy|gia đình|quốc gia|đóng vai)\b", "unrelated", text)
    return fold_text(text)


@lru_cache(maxsize=1)
def _abuse_patterns():
    from ..safety_gate.resources import load_all_curated_patterns

    return load_all_curated_patterns()


def question_kind(question: str | None, facts: list[str]) -> str:
    """Positive evidence only; reducer topics and model output are never evidence."""
    from ..platform_events.comment_intent import is_non_question
    from ..safety_gate.resources import match_curated

    text = question or ""
    if match_curated(text, _abuse_patterns()):
        return "safety_filtered"
    if is_non_question(text):
        return "non_question"
    # Evaluate every representative, not just the first text in a cluster.
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if not parts:
        return "not_answerable_non_commerce"
    kinds = []
    known = fold_text(" ".join(facts))
    known_nouns = set(_PRODUCT_NOUNS.findall(known))
    known_phrases = [" ".join(re.findall(r"\w+", _commerce_view(fact))) for fact in facts]
    for part in parts:
        normalized = _commerce_view(part)
        asking = bool(_FOLDED_ASKING.search(normalized))
        commerce = bool(_SHOP_CONTEXT.search(normalized)) and (
            asking or bool(_SHOP_REQUEST.search(normalized))
        )
        # Incidental shared words (water/France in a geography question) are
        # not product intent. Require an intact approved phrase when the
        # explicit commerce vocabulary above does not establish relevance.
        words = " " + " ".join(re.findall(r"\w+", normalized)) + " "
        commerce = commerce or (
            asking
            and any(len(phrase.split()) >= 2 and f" {phrase} " in words for phrase in known_phrases)
        )
        if commerce:
            nouns = set(_PRODUCT_NOUNS.findall(normalized))
            if known_nouns and nouns - known_nouns:
                kinds.append("other_product")
            elif nouns and nouns <= known_nouns:
                kinds.append("current_product")
            else:
                kinds.append("commerce")
        elif asking and is_off_topic(part, facts):
            kinds.append("off_topic")
        else:
            return "not_answerable_non_commerce"
    if "other_product" in kinds:
        return "other_product"
    if len(set(kinds)) == 1:
        return kinds[0]
    return "commerce" if "off_topic" not in kinds else "not_answerable_non_commerce"


def is_off_topic(question: str, facts: list[str]) -> bool:
    """An explicit personal question to the host with no commerce/fact connection."""
    lowered = question.split("|")[0].strip().lower()
    folded = _commerce_view(lowered)
    if _SHOP_CONTEXT.search(folded) or not _HOST.search(lowered):
        return False
    if not (_PERSONAL.search(lowered) and _FOLDED_ASKING.search(folded)):
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
    "Câu hỏi này mình chưa có thông tin chính xác, bạn nhắn tin cho shop để được hỗ trợ kỹ hơn nha. "
    "Cảm ơn bạn đã quan tâm. Giờ mình tiếp tục giới thiệu cho mọi người nhé."
)
_CURRENT_PRODUCT_DEFLECTION = (
    "Với sản phẩm đang giới thiệu, câu hỏi này mình chưa có thông tin chính xác để trả lời. "
    "Bạn nhắn tin cho shop để được hỗ trợ kỹ hơn nha. Giờ mình tiếp tục giới thiệu cho mọi người nhé."
)
_OTHER_PRODUCT_DEFLECTION = (
    "Mình chưa có thông tin về mặt hàng này trong phần giới thiệu hôm nay. "
    "Bạn nhắn tin cho shop để được hỗ trợ kỹ hơn nha. Giờ mình tiếp tục giới thiệu cho mọi người nhé."
)


def deflection_for(question: str | None, facts: list[str]) -> str:
    """Fixed, non-echoing fallback for positively identified shop/host questions only."""
    from .approved_speech import SpeechRejected

    kind = question_kind(question, facts)
    choices = {
        "off_topic": _OFF_TOPIC_DEFLECTION,
        "commerce": _UNKNOWN_DEFLECTION,
        "current_product": _CURRENT_PRODUCT_DEFLECTION,
        "other_product": _OTHER_PRODUCT_DEFLECTION,
    }
    if kind not in choices:
        raise SpeechRejected(kind)
    return choices[kind]


def is_deflection(text: str | None) -> bool:
    """Whether a spoken answer is one of the fixed "cannot answer" texts (not an answer)."""
    from .approved_speech import DEFLECTION

    return text in (
        DEFLECTION,
        _OFF_TOPIC_DEFLECTION,
        _UNKNOWN_DEFLECTION,
        _CURRENT_PRODUCT_DEFLECTION,
        _OTHER_PRODUCT_DEFLECTION,
    )
