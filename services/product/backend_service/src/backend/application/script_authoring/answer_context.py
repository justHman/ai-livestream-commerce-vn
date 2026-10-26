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


# These rules authorize missing-information fallback only, never fact answering.
_PRODUCT_NOUNS = re.compile(r"\b(?:ao|quan|giay|tui|vay|mu|dong ho|my pham|serum|son)\b")
_HARD_COMMERCE = re.compile(
    r"\b(?:gia|size|kich co|kich thuoc|mau|chat lieu|nguyen lieu|co gian|vai|"
    r"ship|giao|giao hang|van chuyen|nhan hang|lay hang|con hang|het hang|"
    r"con size|con mau|con khong|con chua|co san|kho hang|ma|giam|doi tra|bao hanh|"
    r"mua|dat|chot|order|cod|thanh toan|tien|phi|mien phi|khuyen mai|uu dai|"
    r"voucher|hoa don|tra gop|chinh hang|chinh sach|giat|chong nuoc|chong nang|"
    r"so dien thoai|sdt|dia chi|lien he|inbox|ib|shop|cua hang|showroom|"
    r"san pham|ao|quan|giay|tui|dong ho|my pham|serum|son|nuoc hoa)\b|"
    r"\b(?:lay|giu)\s+(?:\d+|mot|hai|ba|bon|nam|cai|chiec|bo|hop|chai|ma)\b|"
    r"\b[a-z]+\d+[a-z0-9]*\b|\b(?:co\s+)?ban\s+(?:do|dao|hang)\b|"
    r"\b\d+\s+(?:cai|chiec|bo|hop|chai|goi|lo|doi)\b"
)
_HOST = r"(?:chi chu|chu shop|cua hang|shop|admin|chi|em|anh|co|ban)"
_SOFT_GOODS = (
    r"(?:vay|ao|quan|giay|dao|vo goi|vo chan|bo do ngu|do ngu|do an|con bup be|con gau bong)"
)
_SOFT_COMMERCE = re.compile(
    rf"\bban\s+{_SOFT_GOODS}\b|\b(?:cho (?:em |minh )?xem|xem hang)\b|"
    rf"\bco\s+(?:con\s+)?{_SOFT_GOODS}\b"
)
# "chi co may xay khong?", "shop co vo dien thoai khong?": asking whether the shop has an item. Private-life
# questions are vetoed earlier by is_off_topic; greetings about the host's state are not goods.
_AVAILABILITY = re.compile(r"\b(?:co|ban|con)\s+(?:\w+\s+){1,6}?(?:khong|chua|hong|ko)\b")
# Match a state predicate, not a word anywhere in a product name (ngôn ngữ,
# máy hâm nóng, sách hay...). Privacy/state vetoes run before commerce keywords.
_NOT_GOODS = re.compile(
    r"\bco\s+(?:khoe|vui|on|ngon|met|buon|doi|khat|biet|nho|hieu|thay|quen|ranh|ban gi|buc|gio)"
    r"\s+(?:khong|chua|hong|ko)\b|\bco\s+(?:biet|nho|hieu|thay|quen)\b"
)
_HOST_STATE = re.compile(
    rf"\b{_HOST}\s+co\s+(?:khoe|vui|on|met|buon|doi|khat|ranh)\s+(?:khong|chua|hong|ko)\b"
)
_HOST_PRIVATE = re.compile(
    rf"\b{_HOST}\s+co\s+(?:bao nhieu\s+)?"
    r"(?:chong|spouse|nguoi yeu|ban trai|ban gai|con cai|tuoi|sinh nam|que|thich|tung|dinh|"
    r"doc than|o dau|lam nghe|hay)\b"
)
_CHILD_PRIVATE = re.compile(
    rf"\b{_HOST}\s+co\s+(?:(?:may|bao nhieu)\s+)?con"
    r"(?=$|\s+(?:khong|chua|hong|ko|roi|a|ha|nhi|vay|nhe)\b)|"
    rf"\b{_HOST}\s+co\s+con\s+(?:cai|ban|dat hang)\b"
)
_ASCII_SPOUSE = re.compile(
    rf"\b{_HOST}\s+co\s+(?:bao nhieu\s+)?vo"
    r"(?:\s+chong\b|(?=$|\s+(?:khong|chua|hong|ko|roi|a|ha|nhi|vay)\b))"
)
# Existing direct questions without 'co'; anchored to the host, never a free
# keyword scan through a buyer's food/product/family context.
_DIRECT_PRIVATE = re.compile(
    rf"\b{_HOST}\s+(?:co\s+)?"
    r"(?:(?:con|remaining) doc than|gia dinh|song o|nha o dau|yeu ai|uong gi|ngu chua|"
    r"biet nhay|biet hat|hay ngu|di dau choi|khoe|bao nhieu tuoi|sinh nam|que|thich)\b"
)


def _commerce_view(text: str) -> str:
    # Keep explicit non-commerce meanings before accent folding (mưa != mua,
    # máu != màu, gia đình/quốc gia != giá). Other shopping words survive.
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"\b(?:mưa|máu|mù|vậy|gia đình|quốc gia|đóng vai)\b", "unrelated", text)
    folded = fold_text(text)
    folded = re.sub(r"\b(?:gia dinh|quoc gia)\b", "unrelated", folded)
    return re.sub(r"\b(gi|sao|nao) vay\b", r"\1 unrelated", folded)


@lru_cache(maxsize=1)
def _abuse_patterns():
    from ..safety_gate.resources import load_all_curated_patterns

    return load_all_curated_patterns()


def question_kind(question: str | None, facts: list[str]) -> str:
    """Classify fallback eligibility only; every admitted question may answer from facts."""
    from ..platform_events.comment_intent import is_non_question
    from ..safety_gate.resources import match_curated

    text = question or ""
    if match_curated(text, _abuse_patterns()):
        return "safety_filtered"
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if not parts or all(is_non_question(part) for part in parts):
        return "non_question"
    if any(is_off_topic(part, facts) for part in parts):
        return "not_answerable_non_commerce"
    kinds = []
    known_nouns = set(_PRODUCT_NOUNS.findall(fold_text(" ".join(facts))))
    known_phrases = [" ".join(re.findall(r"\w+", _commerce_view(fact))) for fact in facts]
    for part in parts:
        if is_non_question(part):
            continue
        normalized = _commerce_view(part)
        words = " " + " ".join(re.findall(r"\w+", normalized)) + " "
        asks_goods = bool(_AVAILABILITY.search(words.strip()) and not _NOT_GOODS.search(words))
        commerce = bool(
            _HARD_COMMERCE.search(normalized) or _SOFT_COMMERCE.search(normalized) or asks_goods
        ) or any(len(phrase.split()) >= 2 and f" {phrase} " in words for phrase in known_phrases)
        if not commerce:
            return "unclassified"
        nouns = set(_PRODUCT_NOUNS.findall(normalized))
        if known_nouns and nouns - known_nouns:
            kinds.append("other_product")
        elif nouns and nouns <= known_nouns:
            kinds.append("current_product")
        else:
            kinds.append("commerce")
    if "other_product" in kinds:
        return "other_product"
    return kinds[0] if len(set(kinds)) == 1 else "commerce"


def answer_scope(question: str | None, facts: list[str], names: list[str]) -> tuple[str, str]:
    """Decide BEFORE any model call whether the viewer's words may be answered from the approved facts.

    Returns (action, question): "skip" when every comment is abusive (nothing is said, no model call);
    "other_product" when every remaining comment names an item that is not the item being presented (the
    session may hold several items; this turn treats such a comment as off-topic, never answers it with a
    fact of the current product); otherwise "answer" with the abusive comments of a mixed cluster removed
    so a valid question is still answered.

    "The item being sold" is read from the product NAMES only: a fact such as "dễ phối cùng quần jeans"
    must not make a question about trousers look like one about the jacket, and a detail question
    ("áo có túi không") names the sold item itself, so it is never another item.
    """
    from ..safety_gate.resources import match_curated

    text = question or ""
    parts = [part.strip() for part in text.split("|") if part.strip()]
    if not parts:
        return "answer", text
    kept = [part for part in parts if not match_curated(part, _abuse_patterns())]
    if not kept:
        return "skip", ""
    sold = set(_PRODUCT_NOUNS.findall(fold_text(" ".join(names))))

    def about_another_item(part: str) -> bool:
        if not sold or question_kind(part, facts) not in (
            "other_product",
            "current_product",
            "commerce",
        ):
            return False
        nouns = set(_PRODUCT_NOUNS.findall(_commerce_view(part)))
        return bool(nouns) and not (nouns & sold)

    if all(about_another_item(part) for part in kept):
        return "other_product", " | ".join(kept)
    return "answer", " | ".join(kept)


def is_off_topic(question: str, facts: list[str]) -> bool:
    """Narrow private-life fallback veto, preserving accented product homographs."""
    original = unicodedata.normalize("NFC", question).lower()
    original = re.sub(r"\bcòn\b", "remaining", original)
    original = re.sub(r"\bchống\b", "protective", original)
    original = re.sub(r"\bvợ\b", "spouse", original)
    original = re.sub(r"\bvỏ\b", "productcover", original)
    normalized = " ".join(re.findall(r"\w+", fold_text(original)))
    normalized = re.sub(r"\bchong\s+(?:nuoc|nang)\b", "protective", normalized)
    stock = r"\bcon(?=\s+(?:hang|size|mau|san)\b)"
    normalized = re.sub(stock, "remaining", normalized)
    if original == fold_text(original):
        normalized = re.sub(r"\bcon(?=\s+ban\b)", "remaining", normalized)
    return bool(
        _HOST_PRIVATE.search(normalized)
        or _HOST_STATE.search(normalized)
        or _DIRECT_PRIVATE.search(normalized)
        or _CHILD_PRIVATE.search(normalized)
        or _ASCII_SPOUSE.search(normalized)
    )


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
    """Fixed, non-echoing fallback for eligible commerce questions only."""
    from .approved_speech import SpeechRejected

    kind = question_kind(question, facts)
    choices = {
        "commerce": _UNKNOWN_DEFLECTION,
        "current_product": _CURRENT_PRODUCT_DEFLECTION,
        "other_product": _OTHER_PRODUCT_DEFLECTION,
    }
    if kind not in choices:
        # This refusal concerns speech only; unclassified input is still admitted.
        raise SpeechRejected("not_answerable_non_commerce" if kind == "unclassified" else kind)
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
