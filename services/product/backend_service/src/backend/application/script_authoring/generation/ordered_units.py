"""Ordered session script: adaptive unit plan, unit prompts and a bounded LLM call.

One generated unit is one short spoken part (1-2 sentences). The backend fixes the
plan from the AVAILABLE facts, so no filler parts exist and no model decides how many
parts a product has: session opening (first product only, a warm-up), intro, one unit
per cluster of approved claims (every claim exactly once), a price/offer unit that
carries the single soft call to action, and the session closing (last product only).
Prompts carry authoritative facts only; the deterministic unit gate and the output
guards below decide whether a text may be kept.
"""

from __future__ import annotations

import re
import math
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace

from ..compile import compile_spoken_text, number_to_vietnamese_words
from ..duration import spoken_duration_ms
from ..gate.rules.commerce_claims import _vnd_amount

__all__ = [
    "BoundedLLM",
    "LLMDeadlineError",
    "ProductBrief",
    "SessionBrief",
    "UnitSpec",
    "build_unit_prompt",
    "build_unit_repair_prompt",
    "clean_unit_text",
    "check_unit_text",
    "claim_covered",
    "claims_not_spoken",
    "uncovered_claims",
    "fallback_unit_text",
    "plan_units",
    "role_bounds_s",
    "role_title",
    "spoken_price",
]

# Spoken-duration bounds (seconds, canonical estimator) per role. Wide on purpose:
# the owner edits the text; these only reject empty/one-word and runaway output.
# Measured in the 2026-10-15 live (VieNeu, TTS tempo 0.92): 3672 characters were spoken in 226 s,
# i.e. 16 characters/second. The same script had been planned at 9 and ran at 63% of its target.
_VOICE_CHARS_PER_S = 16
# The 2026-10-15 live lasted 5:15 for 3:46 of script (72%); the 2026-10-16 test ran 4:52 for 4:42 with only two
# short answers, so 75% left a 6-minute target ~1 minute short. 85% leaves room for longer viewer answers.
_SPEECH_SHARE_OF_TARGET = 0.85
# Short enough that a viewer question waits for at most one unit before the answer.
_UNIT_SECONDS = 28
_BOUNDS_S: dict[str, tuple[float, float]] = {
    "opening": (1.0, 40.0),
    "intro": (1.0, 40.0),
    "highlight": (1.0, 60.0),
    "sizes": (1.0, 60.0),
    "assurance": (1.0, 60.0),
    "offer": (1.0, 60.0),
    "closing": (1.0, 35.0),
}
_TITLES = {
    "opening": "Mở đầu phiên live",
    "intro": "Giới thiệu sản phẩm",
    "highlight": "Điểm nổi bật",
    "sizes": "Bảng size",
    "assurance": "Cam kết và lưu ý",
    "offer": "Giá và ưu đãi",
    "closing": "Lời kết phiên live",
}
_GUIDE = {
    "opening": (
        "Chào khán giả thật ấm áp như gặp người quen, nói vài câu về không khí buổi live hôm "
        "nay, hứa hẹn những gì mọi người sẽ xem, và MỜI khán giả bình luận, đặt câu hỏi (ví dụ "
        "hỏi size, hỏi giá) ngay trong phiên. 2 đến 4 câu. Không nói giá, không nêu tính "
        "năng, không nhắc sản phẩm cụ thể."
    ),
    "intro": (
        "Mở đầu sản phẩm như người trong nghề đang giới thiệu cho bạn bè: nói tên sản phẩm, "
        "nó dành cho ai và hợp dịp nào, tạo sự tò mò. 3 đến 5 câu. Chưa nói giá."
    ),
    "highlight": (
        "Kể về những điểm hay bên dưới bằng lời của MC: mỗi điểm nói thành 1 đến 2 câu, có "
        "hình dung dịp sử dụng (không thêm đặc tính mới), xen một câu hỏi gợi mở hay "
        "lời nhắn khán giả nếu hợp. 2 đến 4 câu. MỖI CÂU CHỈ MỘT CHỦ ĐỀ, không nối các ý khác "
        "chủ đề (ví dụ size với cách giặt) bằng 'và'. Giữ nguyên số liệu và ký hiệu size "
        "(S, M, L, XL...) viết liền, không tách chữ."
    ),
    "sizes": (
        "Đây là bảng size: nói thật dễ nghe bằng khoảng chiều cao và cân nặng, mỗi câu tối đa "
        "3 mức size, không đọc dài chuỗi số; thêm lời dặn thân thiện như người bán tư vấn, mời "
        "mọi người nhắn chiều cao cân nặng để mình tư vấn size. 3 đến 6 câu. Không nói gì "
        "ngoài size."
    ),
    "assurance": (
        "Nói các ý bên dưới một cách trung thực và trấn an như người bán hàng tử tế: giải thích "
        "vì sao điều đó có lợi cho khách, nếu là điều kiện hay hạn chế thì nói rõ ràng, nhẹ "
        "nhàng, không bán gắt. 2 đến 4 câu, ưu tiên đủ điều kiện, bỏ câu đệm. Không mở đầu "
        "bằng 'Mình cam kết' hay 'Cam kết'; mỗi ý vào bằng một cách khác nhau (từ lợi ích của "
        "khách, từ câu khách hay hỏi, hay từ điều cần lưu ý), không lặp một mẫu câu."
    ),
    "offer": (
        "Nói giá (đã đọc thành chữ bên dưới, chép đúng, không viết lại thành số) và ưu đãi nếu "
        "có, kèm một hai câu giúp khách thấy giá trị. Ưu đãi có điều kiện hay thời gian thì giữ "
        "NGUYÊN điều kiện/thời gian như thông tin, không nói như đang áp dụng ngay hôm nay. 2 "
        "đến 5 câu."
    ),
    "closing": (
        "Cảm ơn khán giả chân thành, nhắc lại tinh thần buổi live, mời theo dõi và hẹn gặp lại. "
        "2 đến 4 câu. Không giới thiệu thêm sản phẩm, không nhắc tên sản phẩm hay ưu đãi, "
        "không dùng 'không bỏ lỡ' hay 'đừng bỏ lỡ'."
    ),
}
# The role title is also sent to the model. "Cam kết và lưu ý" made it open every assurance part with
# "Mình cam kết", so the prompt uses a neutral title; the owner-visible title stays in _TITLES.
_PROMPT_TITLES = {**_TITLES, "assurance": "Chính sách mua hàng và lưu ý"}
_CTA_LINE = (
    "Cuối phần này thêm MỘT lời mời nhẹ nhàng (ví dụ bạn nào quan tâm thì nhắn mình hoặc xem "
    "giỏ hàng). Không dùng: chốt đơn ngay, đặt ngay, mua ngay, nhanh tay, không bỏ lỡ, số "
    "lượng có hạn."
)
_STYLE_RULES = (
    "Luôn gọi người xem là 'mọi người' hoặc 'các bạn' (không dùng quý vị, quý khách, anh, "
    "chị, em, bạn nam, bạn nữ) và xưng 'mình' (không xưng em, tôi; không gọi sản phẩm là "
    "'em nó'). Viết đúng chính tả. Chỉ nhắc tên sản phẩm đầy đủ ở phần giới "
    "thiệu; các phần sau gọi 'mẫu này' hoặc loại sản phẩm. Câu phải kết thúc trọn vẹn. Tránh lặp các cụm quen tai ('thật lòng', 'yên tâm', 'có tò mò ... không', 'cực kỳ'): mỗi cụm dùng nhiều nhất một lần trong một phần, đổi cách nói ở các phần khác nhau."
)
_TRUTH_RULES = (
    "Phần 'Thông tin được phép nói' là SỰ THẬT bạn phải giữ đúng, KHÔNG phải kịch bản để đọc "
    "lại: hãy diễn đạt bằng lời của MC, sắp xếp cho tự nhiên, thêm câu hỏi gợi mở hoặc "
    "tình huống sử dụng giả định; không bịa trải nghiệm cá nhân (đã mặc, sờ vào, dùng thử), "
    "không suy ra công dụng, độ bền, cảm giác khi mặc hay cam kết mới. "
    "Thông tin của chủ shop có thể rất ngắn, việc của bạn là làm nó thành lời nói sinh "
    "động. TUYỆT ĐỐI KHÔNG thêm: con số, giá, ưu đãi, thời hạn, bảo hành, chính sách, thông "
    "số, chất liệu, xuất xứ, chứng nhận không có trong thông tin; không hứa hiệu quả hay tác "
    "dụng sức khỏe; không nói 'bán chạy nhất', 'tốt nhất', 'hàng nghìn khách'; không so sánh "
    "với nhãn hàng khác. Không nói giờ giấc hay buổi trong ngày, thời lượng buổi live, vị trí nút "
    "bấm hay giỏ hàng trên màn hình; không thêm chi tiết sản xuất, đường may, công dụng cụ "
    "thể hay lý do kỹ thuật không có trong thông tin. Giá luôn nói bằng chữ, không viết số."
)
_COMMON_RULES = (
    "Không bao giờ nói rằng thiếu thông tin (ví dụ 'chưa có thông tin', 'chưa có khuyến mãi'): "
    "nếu không có gì để nói thì bỏ qua ý đó."
)

UNIT_FAILED_PREFIX = "<Phần này chưa soạn được"
_MAX_UNIT_CHARS = 900
_FALLBACK_MAX_S = 45.0  # extractive paragraphs stay short even though written units may run longer
_DASH_RE = re.compile(r"\s*[—–]\s*")
_FENCE_RE = re.compile(r"```[a-z]*|^[#>*\-\s]+(?=\S)", re.MULTILINE)
# A letter-spaced size ("X L", "X X L") is one token.
_SPLIT_SIZE_RE = re.compile(r"\b((?:X\s+){1,3})(?=[SL]\b)")
_MISSING_INFO_RE = re.compile(
    r"chưa có (?:thông tin|khuyến mãi|ưu đãi|giá|dữ liệu)"
    r"|không có (?:thông tin|khuyến mãi|ưu đãi)"
    r"|hiện (?:tại )?chưa có|chưa (?:rõ|được cung cấp|cập nhật)|thiếu thông tin",
    re.IGNORECASE,
)
_HARD_SELL_RE = re.compile(
    r"chốt đơn ngay|đặt (?:hàng )?ngay|mua ngay|nhanh tay|không bỏ lỡ|đừng bỏ lỡ|số lượng có hạn"
    r"|chốt ngay|order ngay|kẻo hết|sắp hết hàng",
    re.IGNORECASE,
)
# Inviting people to follow the channel is not sales pressure (opening/closing only).
_FOLLOW_INVITE_RE = re.compile(
    r"(?:không|đừng) bỏ lỡ (?:những |các |mọi )?(?:buổi|phiên|lần|video|bản tin|thông báo)",
    re.IGNORECASE,
)
_BRIDGE_RE = re.compile(
    r"(?:sản phẩm|món|mẫu)\s+(?:tiếp theo|kế tiếp|tiếp đến)|(?:tiếp theo|kế tiếp) là",
    re.IGNORECASE,
)
_PRICE_TOKEN_RE = re.compile(
    r"(?<![\w.,])\d[\d.,]*\s*(?:vnđ|vnd|đồng|₫|đ|nghìn|ngàn|triệu|tr|k)(?!\w)", re.IGNORECASE
)
_PROMO_RE = re.compile(r"giảm|tặng|khuyến mãi|ưu đãi|quà|voucher|mã giảm", re.IGNORECASE)
_ASSURANCE_RE = re.compile(
    r"bảo hành|đổi trả|hoàn tiền|giao hàng|vận chuyển|\bship\b|chính hãng|cam kết|kiểm hàng"
    r"|chứng nhận|công bố|không (?:đổi|hoàn)",
    re.IGNORECASE,
)
_CONDITION_RE = re.compile(
    r"\bvào\b|thứ|đầu tháng|cuối tháng|cuối tuần|\bkhi\b|\bnếu\b|\btừ\b.*\bđến\b|mỗi|"
    r"áp dụng|đơn từ|tối thiểu",
    re.IGNORECASE,
)
_HIGHLIGHT_TYPES = ("feature", "benefit", "ingredient", "usage")
_ASSURANCE_TYPES = ("warranty", "shipping", "compliance", "faq", "limitation")
_MAX_CLAIMS_PER_UNIT = 6  # decorative claims (typed mode) speak at most this many per unit
_MAX_HIGHLIGHT_CLAIMS = 18  # typed mode keeps at most three highlight units of selling points
_MAX_MUST_PER_UNIT = 4  # must-keep (assurance) and untyped claims per unit
_MAX_PROMOS_PER_UNIT = 3
_MAX_PARAGRAPH_CLAIMS = 3  # fallback paragraphs
_MAX_SIZE_ROWS = 40
_SIZE_ROW_RE = re.compile(
    r"(?:cao|chiều cao)[^.]*?(?:cân nặng|nặng)|\bsize\s*[A-Za-z0-9]+\b[^.]*\d"
    r"|\b\d\s*m\s*\d{2}\b[^.]*\d\s*(?:kg|kí|ki lô)\b[^.]*\bsize\b",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'”’)\]]*$")
# Reject foreign scripts before wording salvage; never silently delete a corrupted fact.
_FOREIGN_SCRIPT_RE = re.compile(
    r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0003134f\u3040-\u30ff\uac00-\ud7af\ufffd]+"
)
_EXPERIENCE_RE = re.compile(
    r"\b(?:mình|tôi)\s+(?:(?:đã|từng|có)\s+)?(?:dùng thử|mặc thử|sử dụng thử|trải nghiệm|sờ vào)"
    r"|\bsờ vào (?:thấy|là)\b",
    re.IGNORECASE,
)
# Viewer-address forms only; "trẻ em", "em bé", "anh em" are ordinary words, not address.
_ADDRESS_RE = re.compile(
    r"\bquý (?:vị|khách)\b|\bcác anh chị\b|\banh chị\b|\bcác (?:anh|chị)\b"
    r"|(?<!\w)(?:anh|chị|em) ơi\b|\bbạn (?:nam|nữ)\b|\b(?:chào|mời|cảm ơn) (?:anh|chị)\b",
    re.IGNORECASE,
)
# A restriction/condition that qualifies another claim: never dropped, kept beside it.
_CONDITION_CLAIM_RE = re.compile(
    r"\b(?:không|chỉ|trừ|ngoại trừ|nếu|khi|điều kiện|miễn là)\b", re.IGNORECASE
)
# The owner chose these types explicitly: never capped or dropped.
_MUST_TYPES = ("warranty", "shipping", "promotion", "compliance", "limitation")
_COMMIT_OPEN_RE = re.compile(r"^\W*(?:mình\s+|shop\s+)?(?:xin\s+)?cam kết\b", re.IGNORECASE)
_POSITION_RE = re.compile(
    r"cuối cùng của buổi|(?:sản phẩm|món) (?:đầu tiên|cuối cùng)", re.IGNORECASE
)
_COVERAGE = 0.6
_CTA_RE = re.compile(
    r"đặt hàng|giỏ hàng|nhắn mình|nhắn tin|inbox|bình luận để|chốt đơn", re.IGNORECASE
)
_TYPOS = {"thoải chọn": "thoải mái chọn", "đúng mẫi": "đúng mẫu"}
_CHEAP_RE = re.compile(
    r"giá (?:siêu )?rẻ|rẻ nhất|bán chạy nhất|tốt nhất|số (?:một|1)\b"
    r"|hàng (?:nghìn|ngàn|triệu) (?:khách|người)",
    re.IGNORECASE,
)  # subjective/superlative claims are not facts unless the owner approved them
_KH_RE = re.compile(r"(?<!\w)kh(?!\w)")  # texting shorthand typed in product data
# Clearly different subjects only (material/feel/fit are properties of ONE attribute).
_TOPICS = {
    "size": re.compile(r"\bsize\b|chiều cao|cân nặng|kích cỡ", re.IGNORECASE),
    "care": re.compile(r"giặt|phơi|\bủi\b|bảo quản|nhiệt độ", re.IGNORECASE),
    "policy": re.compile(
        r"đổi trả|bảo hành|giao hàng|vận chuyển|hoàn tiền|kiểm hàng", re.IGNORECASE
    ),
    "price": re.compile(r"\bgiá\b|nghìn|triệu|đồng", re.IGNORECASE),
}


def role_bounds_s(role: str) -> tuple[float, float]:
    return _BOUNDS_S[role]


def role_title(role: str) -> str:
    return _TITLES[role]


_CLAUSE_BREAK_RE = re.compile(r"(?<=[,;])\s+|\s+(?=(?:và|nhưng)\s)")


def _duration_s(text: str) -> float:
    return spoken_duration_ms(compile_spoken_text(text).spoken_text) / 1000.0


def _fit_pieces(claim: str, max_s: float) -> list[str]:
    """One claim as paragraphs within the bound: split only at commas, semicolons, "và" and
    "nhưng"; a clause that alone exceeds the bound stays verbatim in its own paragraph."""
    if _duration_s(claim) <= max_s:
        return [claim]
    paras: list[str] = []
    for piece in _CLAUSE_BREAK_RE.split(claim):
        if paras and _duration_s(f"{paras[-1]} {piece}") <= max_s:
            paras[-1] = f"{paras[-1]} {piece}"
        else:
            paras.append(piece)
    return paras


def _paragraphs(items: list[str], max_s: float) -> list[str]:
    """Short paragraphs: <= 3 claims and within the duration bound (split, never exceed)."""
    paras: list[list[str]] = []
    for item in items:
        pieces = _fit_pieces(item, max_s)
        if len(pieces) > 1:
            paras.extend([piece] for piece in pieces)
            continue
        cur = paras[-1] if paras else None
        if cur is not None and len(cur) < _MAX_PARAGRAPH_CLAIMS:
            if _duration_s(" ".join([*cur, item])) <= max_s:
                cur.append(item)
                continue
        paras.append([item])
    return [" ".join(p) for p in paras]


def _is_condition(claim: str) -> bool:
    return bool(_CONDITION_CLAIM_RE.search(claim))


def _size_paragraphs(rows: tuple[str, ...], must: set[str], max_s: float) -> list[str]:
    """As many rows as fit the duration bound; restricted rows are ALWAYS spoken."""
    paras: list[str] = []
    for row in rows:
        keep = row in must or _is_condition(row)
        if paras:
            joined = compile_spoken_text(f"{paras[-1]} {row}").spoken_text
            if spoken_duration_ms(joined) / 1000.0 <= max_s:
                paras[-1] = f"{paras[-1]} {row}"
                continue
        if keep or not paras:
            paras.append(row)
    return paras


def _end(claim: str) -> str:
    """Claims are stored without final punctuation; joined as-is they run into one breath."""
    text = claim.rstrip()
    if text.rstrip("\"'”’)]»").endswith((".", "!", "?", "…", ";")):
        return text
    return f"{text}."


def _ask_aloud(claim: str) -> str:
    """A stored FAQ ("Q? A") is spoken as what viewers ask; fixed wording, no new fact."""
    head, sep, _ = claim.partition("?")
    return f"Nhiều bạn hỏi: {claim}" if sep and len(head) > 3 else claim


def fallback_unit_text(spec: "UnitSpec", product: "ProductBrief") -> str:
    """Deterministic extractive unit for a part the model could not write.

    Only approved claim sentences (ALL of the unit's claims, verbatim, in short paragraphs;
    size rows as many as fit, restricted ones always) and price words; fixed greetings carry
    no facts. No LLM, no invented facts, no markup.
    """
    name = product.name or product.product_id
    max_s = min(role_bounds_s(spec.role)[1], _FALLBACK_MAX_S)
    if spec.role == "opening":
        text = "Chào mọi người, chào mừng mọi người đến với buổi live hôm nay."
    elif spec.role == "closing":
        text = "Cảm ơn mọi người đã theo dõi, hẹn gặp lại mọi người."
    elif spec.role == "intro":
        text = f"Mình giới thiệu {name} đến mọi người."
    elif spec.role == "offer":
        words = [w for w in (spoken_price(p) for p in product.prices) if w]
        price = [f"Giá {' hoặc '.join(words)}."] if words and spec.with_price else []
        text = "\n\n".join(_paragraphs([*price, *spec.promos], max_s))
    elif spec.role == "sizes":
        text = "\n\n".join(_size_paragraphs(tuple(map(_end, spec.claims)), set(spec.must), max_s))
    else:
        text = "\n\n".join(_paragraphs([_end(_ask_aloud(c)) for c in spec.claims], max_s))
    text = _KH_RE.sub("không", text or f"Mình giới thiệu thêm về {name}.")
    if spec.revisit:
        text = "Mình nhắc lại để các bạn mới theo dõi tiện tham khảo. " + text
    return compile_spoken_text(text).spoken_text


def spoken_price(price: str) -> str | None:
    """ "100000.00 VND" / "299.000đ" / "299k" -> "một trăm nghìn đồng"; None if ambiguous."""
    amount = _vnd_amount(price)
    return None if amount is None else f"{number_to_vietnamese_words(amount)} đồng"


def _speak_approved_prices(text: str, prices: tuple[str, ...]) -> str:
    """Rewrite raw price tokens that equal an APPROVED price as words. An unapproved price
    is left as digits so the gate still rejects it."""
    approved = {a for a in (_vnd_amount(p) for p in prices) if a is not None}

    def swap(match: re.Match[str]) -> str:
        amount = _vnd_amount(match.group().strip())
        if amount is not None and amount in approved:
            return f"{number_to_vietnamese_words(amount)} đồng"
        return match.group()

    # "180 nghìn đồng": the token stops at "nghìn", so the swap would read "đồng đồng".
    return re.sub(r"\bđồng(?:\s+đồng)+\b", "đồng", _PRICE_TOKEN_RE.sub(swap, text))


@dataclass(frozen=True)
class SessionBrief:
    title: str = ""
    shop_name: str = ""
    persona: str = ""
    notes: str = ""


@dataclass(frozen=True)
class ProductBrief:
    product_id: str
    name: str
    prices: tuple[str, ...] = ()
    discounts: tuple[str, ...] = ()
    claims: tuple[str, ...] = ()  # flat, authoritative
    claims_by_type: tuple[tuple[str, tuple[str, ...]], ...] = ()  # optional grouping
    info: tuple[tuple[str, str], ...] = ()  # brand / category / short_description
    previous_name: str | None = None  # only when the set is ORDER_AWARE


@dataclass(frozen=True)
class UnitSpec:
    """One planned unit: its role and exactly the facts it may speak."""

    role: str
    claims: tuple[str, ...] = ()
    promos: tuple[str, ...] = ()
    with_price: bool = False
    with_cta: bool = False
    bridge: bool = False  # first unit of a later product on a locked (ORDER_AWARE) order
    first_product: bool = False
    last_product: bool = False
    must: tuple[str, ...] = ()  # claims of this unit the text MUST cover (checked)
    target_seconds: float = 0.0
    revisit: bool = False


_KIND_RANK = {"warranty": 0, "limitation": 1, "compliance": 2}


def _chunks(items: list[str], size: int) -> list[tuple[str, ...]]:
    return [tuple(items[i : i + size]) for i in range(0, len(items), size)]


def _cluster_claims(product: ProductBrief) -> dict:
    """Group the FLAT approved claims (``claims_by_type`` only classifies; a typed text absent
    from the flat list is ignored).

    must-keep = typed warranty/shipping/promotion/compliance/limitation (when types are given)
    plus any claim with a restriction word. Without types nothing can be classified, so nothing
    is capped. A size chart (3+ "cao ... nặng ..." rows) is its own list.
    """
    typed: dict[str, str] = {}
    for kind, texts in product.claims_by_type:
        for text in texts:
            typed.setdefault(text, kind)
    typed_mode = bool(typed)
    highlights: list[str] = []
    assurances: list[str] = []
    promos: list[str] = []
    must: set[str] = set()
    seen: set[str] = set()
    for claim in (*product.claims, *product.discounts):
        if claim in seen:
            continue
        seen.add(claim)
        kind = typed.get(claim)
        if kind in _MUST_TYPES and typed_mode or _is_condition(claim):
            must.add(claim)
        if (
            kind == "promotion"
            or claim in product.discounts
            or (kind is None and _PROMO_RE.search(claim))
        ):
            promos.append(claim)
        elif kind in _ASSURANCE_TYPES or (kind is None and _ASSURANCE_RE.search(claim)):
            assurances.append(claim)
        else:
            highlights.append(claim)
    rows = [c for c in highlights if _SIZE_ROW_RE.search(c)]
    sizes: list[str] = []
    size_overflow: list[str] = []
    if len(rows) >= 3:
        # restricted rows first so the row cap can never remove them; the rest keep their order
        restricted = [r for r in rows if r in must or _is_condition(r)]
        others = [r for r in rows if r not in restricted]
        first = set((restricted[:_MAX_SIZE_ROWS] + others)[:_MAX_SIZE_ROWS])
        sizes = [r for r in rows if r in first]
        # restricted rows beyond one unit go to further units; plain overflow rows are only
        # counted in claims_not_spoken (they reach no unit)
        size_overflow = restricted[_MAX_SIZE_ROWS:]
        highlights = [c for c in highlights if c not in rows]
    return {
        "typed": typed_mode,
        "kinds": typed,
        "highlights": highlights,
        "sizes": sizes,
        "size_overflow": size_overflow,
        "assurances": assurances,
        "promos": promos,
        "must": must,
    }


def plan_units(
    product: ProductBrief,
    *,
    first: bool,
    last: bool,
    ordered_aware: bool = False,
    target_duration_s: float = 0,
) -> list[UnitSpec]:
    """Adaptive, ordered units of one product from its available facts.

    intro -> highlights -> size chart (one unit) -> assurances (warranty, limitation,
    compliance, then shipping; <= 4 claims per unit, never capped) -> offer (price, promotions
    <= 3 per unit). With types, only decorative claims are capped (6); without types nothing is
    dropped (units of <= 4). The single soft CTA is folded into the last product unit.
    """
    c = _cluster_claims(product)
    must: set[str] = c["must"]
    positional = {"first_product": first and ordered_aware, "last_product": last and ordered_aware}

    def spec(role: str, claims=(), promos=(), **extra) -> UnitSpec:
        covered = tuple(x for x in (*claims, *promos) if x in must)
        return UnitSpec(
            role, claims=tuple(claims), promos=tuple(promos), must=covered, **positional, **extra
        )

    highlights: list[str] = c["highlights"]
    if c["typed"]:
        keep = [x for x in highlights if x in must]
        for x in highlights:
            if len(keep) >= _MAX_HIGHLIGHT_CLAIMS:
                break
            if x not in keep:
                keep.append(x)
        highlights = [x for x in highlights if x in keep]
        highlight_chunks = _chunks(highlights, _MAX_CLAIMS_PER_UNIT)
    else:
        highlight_chunks = _chunks(highlights, _MAX_MUST_PER_UNIT)

    def rank(claim: str) -> int:
        kind = c["kinds"].get(claim)
        if kind in _KIND_RANK:
            return _KIND_RANK[kind]
        if _is_condition(claim):
            return 1
        return 0 if re.search(r"bảo hành|đổi trả", claim, re.IGNORECASE) else 3

    ordered_assurance = sorted(c["assurances"], key=rank)  # stable: warranty, limitation, ...
    units = [
        *([spec("opening")] if first else []),
        spec("intro", bridge=ordered_aware and not first),
        *(spec("highlight", chunk) for chunk in highlight_chunks),
        *([spec("sizes", c["sizes"])] if c["sizes"] else []),
        *(spec("sizes", chunk) for chunk in _chunks(c["size_overflow"], _MAX_SIZE_ROWS)),
        *(spec("assurance", chunk) for chunk in _chunks(ordered_assurance, _MAX_MUST_PER_UNIT)),
    ]
    has_price = any(spoken_price(p) for p in product.prices)
    promo_chunks = _chunks(c["promos"], _MAX_PROMOS_PER_UNIT) or ([()] if has_price else [])
    for i, chunk in enumerate(promo_chunks):
        units.append(spec("offer", promos=chunk, with_price=has_price and i == 0))
    # One spaced revisit per factual part, no repeated opening/intro/closing or
    # additional CTA. A longer request expands known information, never invents it.
    candidates = [u for u in units if u.claims and u.role not in ("opening", "intro")]
    # The target is the whole live: answers to viewers and pauses take the rest of it.
    speech_s = target_duration_s * _SPEECH_SHARE_OF_TARGET
    wanted = max(0, math.ceil(speech_s / _UNIT_SECONDS) - len(units) - int(last))
    revisits = [replace(u, revisit=True, with_cta=False) for u in candidates[:wanted]]
    units.extend(revisits)
    body = [i for i, u in enumerate(units) if u.role != "opening"]
    units[body[-1]] = replace(units[body[-1]], with_cta=True)
    if last:
        units.append(spec("closing"))
    if target_duration_s:
        per_part = speech_s / len(units)
        units = [replace(u, target_seconds=min(per_part, role_bounds_s(u.role)[1])) for u in units]
    return units


def _fold(text: str) -> str:
    """Lowercase without diacritics (đ -> d)."""
    folded = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")


# Words compile_spoken_text emits for numbers: the only vocabulary of the extra-number guard.
_NUMBER_WORDS = frozenset(
    "không một hai ba bốn năm sáu bảy tám chín mười mươi mốt tư lăm trăm nghìn ngàn triệu tỷ "
    "lẻ linh rưỡi phẩy".split()
)
_DIGITISH = frozenset("một hai ba bốn năm sáu bảy tám chín mười mốt lăm tư rưỡi".split())
_TIME_UNITS = frozenset({"ngày", "tháng", "tuần", "giờ", "phút", "giây"})
# A lone "năm" before one of these is the number 5, not the year ("năm phần trăm").
_COUNT_UNITS = frozenset(
    "phần ki xăng mi gam lít mét size đồng nghìn ngàn triệu cái chiếc đôi sản".split()
)
# What the compiler says for a unit written next to a digit token.
_UNIT_SPOKEN = {
    "kg": "ki",
    "mg": "mi",
    "ml": "mi",
    "cm": "xăng",
    "mm": "mi",
    "g": "gam",
    "m": "mét",
}
_CLAIM_NUMBER_RE = re.compile(r"(\d+(?:[.,]\d+)?)(%?)\s*([^\W\d_]+)?")
# Negations/restrictions a paraphrase must not lose ("Không bảo hành" must not become "Có").
_RESTRICTION_PHRASES = (
    "không",
    "chưa",
    "chẳng",
    "chỉ",
    "trừ",
    "ngoại trừ",
    "miễn là",
    "nếu",
    "khi",
    "điều kiện",
    "tối đa",
    "tối thiểu",
    "ít nhất",
)
_TIMING_WORDS = ("thứ", "tháng", "đầu", "cuối")
_SIZE_TOKEN_RE = re.compile(r"(?<![A-Za-z])(?:XXXL|XXL|XL|XS|S|M|L)(?![A-Za-z])")


def _spoken_words(text: str) -> list[str]:
    """The canonical spoken form (compile_spoken_text) as lowercase words."""
    return re.findall(r"\w+", compile_spoken_text(text).spoken_text.lower())


def _claim_numbers(claim: str) -> list[tuple[list[str], list[str], str]]:
    """Per number of a claim: (expected phrase, bare number words, unit word right after it).

    Digit tokens are compiled INDIVIDUALLY ("20%" expects "hai mươi phần trăm", unit "phần");
    runs of number WORDS written in the claim ("bảy ngày") are expected phrases too.
    """
    out = []
    for number, percent, unit in _CLAIM_NUMBER_RE.findall(claim):
        bare = _spoken_words(number)
        word = "phần" if percent else (unit or "").lower()
        out.append((_spoken_words(number + percent), bare, _UNIT_SPOKEN.get(word, word)))
    words = re.findall(r"\w+", claim.lower())
    i = 0
    while i < len(words):
        if words[i] not in _NUMBER_WORDS or words[i].isdigit():
            i += 1
            continue
        j = i
        while j < len(words) and words[j] in _NUMBER_WORDS:
            j += 1
        run, unit = words[i:j], (words[j] if j < len(words) else "")
        if not any(w in _DIGITISH for w in run):  # "nghìn" after a digit token, "trăm" alone
            i = j
            continue
        if run[-1] == "năm" and len(run) > 1:  # "một năm": the trailing năm is the unit
            run, unit = run[:-1], "năm"
        if run != ["năm"] or unit in _TIME_UNITS or unit in _COUNT_UNITS:  # else: the year
            out.append((run, run, unit))
        i = j
    return out


def _run_found(words: list[str], phrase: list[str], unit: str) -> bool:
    """``phrase`` occurs as a MAXIMAL run of number words: the neighbours are not number
    words (the unit word itself may follow: "một năm")."""
    n = len(phrase)
    if not n:
        return False
    for i in range(len(words) - n + 1):
        if words[i : i + n] != phrase:
            continue
        before = words[i - 1] if i else ""
        after = words[i + n] if i + n < len(words) else ""
        if before not in _NUMBER_WORDS and (after not in _NUMBER_WORDS or after == unit):
            return True
    return False


def claim_covered(claim: str, text: str, context: tuple[str, ...] = ()) -> bool:
    """Best-effort ADVISORY coverage of one claim by a text (the owner approves the exact text;
    this only guards against silent LLM drift and fails safe into the verbatim fallback).

    Numbers: each number of the claim (digits compiled individually, or number words), as the
    canonical spoken phrase, must occur in the text as a maximal run; right before each unit
    word the claim uses, the text's run of number words must equal an expected phrase or be
    empty ("1 năm" is not "hai mươi mốt năm"). Restriction words (không, chỉ, nếu, khi...),
    timing words (thứ <x>, tháng, đầu, cuối) and uppercase sizes (XL is not XXL) must be kept.
    Other words: at least 60% appear (case/diacritic folded).

    Accepted residuals: numbers swapped between two claims of one unit, prices with grouped
    thousands split by the model, and "Năm nay" read as a numeric unit (quality only).
    """
    claim, text = _coverage_places(claim), _coverage_places(text)
    # A scope split must never discard a restriction before the first scope
    # (e.g. "Chỉ giao hàng toàn quốc ...").
    for phrase in _RESTRICTION_PHRASES:
        pattern = r"(?<!\w)" + phrase + r"(?!\w)"
        if re.search(pattern, claim.lower()) and not re.search(pattern, text.lower()):
            return False
    # Bind delivery times to their geographic scope. A global bag of numbers would
    # accept swapping national 2-3 days with inner-city 1 day, or dropping a city.
    scope = re.compile(r"\b(?:toàn quốc|nội thành|ngoại thành)\b", re.IGNORECASE)
    expected_scopes = list(scope.finditer(claim))
    if len(expected_scopes) > 1 and re.search(r"giao|ship|vận chuyển", claim, re.IGNORECASE):
        actual_scopes = list(scope.finditer(text))
        for i, match in enumerate(expected_scopes):
            piece = claim[
                match.start() : expected_scopes[i + 1].start()
                if i + 1 < len(expected_scopes)
                else len(claim)
            ]
            candidates = [
                text[
                    m.start() : actual_scopes[j + 1].start()
                    if j + 1 < len(actual_scopes)
                    else len(text)
                ]
                for j, m in enumerate(actual_scopes)
                if m.group().lower() == match.group().lower()
            ]
            if not any(claim_covered(piece, candidate) for candidate in candidates):
                return False
    for place in ("hà nội", "hồ chí minh", "nội thành", "ngoại thành", "toàn quốc"):
        if place in claim.lower() and place not in text.lower():
            return False
    spoken = _spoken_words(text)
    claim_words = _spoken_words(claim)
    n = len(claim_words)
    if n and any(spoken[i : i + n] == claim_words for i in range(len(spoken) - n + 1)):
        return True  # the compiled claim itself is in the text (identity)
    numbers = _claim_numbers(claim)
    if any(not _run_found(spoken, expected, unit) for expected, _bare, unit in numbers):
        return False
    allowed = [bare for _e, bare, _u in numbers]
    for other in context:
        allowed += [bare for _e, bare, _u in _claim_numbers(other)]
    for _expected, _bare, unit in numbers:
        if not unit:
            continue
        for j, word in enumerate(spoken):
            if word != unit:
                continue
            start = j
            while start > 0 and spoken[start - 1] in _NUMBER_WORDS:
                start -= 1
            run = spoken[start:j]
            if run and run not in allowed:
                return False
    lowered, claim_lower = text.lower(), claim.lower()
    for phrase in _RESTRICTION_PHRASES:
        if re.search(r"(?<!\w)" + phrase + r"(?!\w)", claim_lower) and not re.search(
            r"(?<!\w)" + phrase + r"(?!\w)", lowered
        ):
            return False
    for word in _TIMING_WORDS:
        if re.search(r"(?<!\w)" + word + r"(?!\w)", claim_lower) and not re.search(
            r"(?<!\w)" + word + r"(?!\w)", lowered
        ):
            return False
    for match in re.finditer(r"(?<!\w)thứ ([^\W\d_]+)", claim_lower):
        if f"thứ {match.group(1)}" not in lowered:
            return False
    for size in set(_SIZE_TOKEN_RE.findall(claim)):
        if not re.search(r"(?<![A-Za-z])" + size + r"(?![A-Za-z])", text):
            return False
    tokens = set(re.findall(r"\w+", _fold(text)))
    words = re.findall(r"[^\W\d_]{2,}", _fold(claim))
    if not words:
        return True
    return sum(1 for w in words if w in tokens) >= _COVERAGE * len(words)


def _coverage_places(text: str) -> str:
    """Only unambiguous Vietnamese city abbreviations, for comparison (never speech)."""
    text = re.sub(
        r"(?<!\w)(?:tp\.?\s*)?hcm(?!\w)|thành phố hồ chí minh",
        "hồ chí minh",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"(?<!\w)hn(?!\w)", "hà nội", text, flags=re.IGNORECASE)


def uncovered_claims(
    text: str, claims: tuple[str, ...], context: tuple[str, ...] = ()
) -> list[str]:
    return [c for c in claims if not claim_covered(c, text, context)]


def claims_not_spoken(approved: tuple[str, ...], saved_text: str) -> int:
    """Approved claims/discounts that the SAVED script text does not cover (owner-visible)."""
    unique = tuple(dict.fromkeys(approved))
    return len(uncovered_claims(saved_text, unique, unique))


def _states_missing_data(text: str, approved: tuple[str, ...]) -> bool:
    """A "missing data" phrase is fine only when an approved statement itself says it
    (e.g. the restriction "Không có khuyến mãi cho đơn dưới 200k.")."""
    # Same whitespace cleanup as the cleaner applies to the model output.
    statements = [" ".join(a.lower().split()) for a in approved]
    return any(
        not any(m.group().lower() in statement for statement in statements)
        for m in _MISSING_INFO_RE.finditer(text)
    )


def _topics(sentence: str) -> set[str]:
    return {name for name, rx in _TOPICS.items() if rx.search(sentence)}


def check_unit_text(
    raw: str | None,
    *,
    spec: "UnitSpec | None" = None,
    prices: tuple[str, ...] = (),
    allow_bridge: bool = False,
    approved: tuple[str, ...] = (),
    product_name: str = "",
    name_used_elsewhere: int = 0,
    must: tuple[str, ...] = (),
    aware: bool = True,
) -> tuple[str | None, str | None]:
    """(text, None) when usable, else (None, machine reason code).

    Reasons: empty, too_long, truncated, missing_info, hard_sell, bridge, address, cta,
    mashup, opener, name_repeat. Approved prices written as digits become words.
    """
    if not raw:
        return None, "empty"
    if _FOREIGN_SCRIPT_RE.search(raw):
        return None, "foreign_script"
    if _EXPERIENCE_RE.search(raw):
        return None, "personal_experience"
    text = _FENCE_RE.sub("", raw).strip().strip("\"'“”")
    text = _DASH_RE.sub(", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for wrong, right in _TYPOS.items():
        text = text.replace(wrong, right)
    text = _KH_RE.sub("không", text)
    text = _SPLIT_SIZE_RE.sub(lambda m: re.sub(r"\s+", "", m.group(1)), text)
    text = _speak_approved_prices(text, prices)
    if len(text) < 6 or not re.search(r"[^\W\d_]{2}", text):
        return None, "empty"
    if len(text) > _MAX_UNIT_CHARS:
        return None, "too_long"
    if not _SENTENCE_END_RE.search(text):
        return None, "truncated"  # cut mid-sentence/mid-word by the provider
    if _states_missing_data(text, approved):
        return None, "missing_info"
    sell_text = text
    if spec is not None and spec.role in ("opening", "closing"):
        sell_text = _FOLLOW_INVITE_RE.sub("", text)
    approved_text = [a.lower() for a in approved]
    if any(
        not any(m.group().lower() in a for a in approved_text)
        for m in _HARD_SELL_RE.finditer(sell_text)
    ):
        return None, "hard_sell"  # pressure wording the owner did not write
    lowered = [a.lower() for a in approved]
    if any(not any(m.group().lower() in a for a in lowered) for m in _CHEAP_RE.finditer(text)):
        return None, "hard_sell"  # each superlative must be one the owner wrote
    if not allow_bridge and _BRIDGE_RE.search(text):
        return None, "bridge"
    if _ADDRESS_RE.search(text):
        return None, "address"
    role = spec.role if spec is not None else ""
    if spec is not None and role not in ("opening", "closing", "sizes") and not spec.with_cta:
        if _CTA_RE.search(text):
            return None, "cta"  # one soft call to action per product, in its last unit
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if " và " in sentence:
            left, _, right = sentence.partition(" và ")
            a, b = _topics(left), _topics(right)
            if a and b and not (a & b):
                return None, "mashup"  # unrelated facts joined with "và"
    owner_facts = (*spec.claims, *spec.promos) if spec is not None else approved
    if role in ("assurance", "highlight", "offer") and not any(
        a.lower().startswith("cam kết") for a in owner_facts
    ):
        sentences = re.split(r"(?<=[.!?])\s+", text)
        # A part opening "Mình cam kết..." is a model tic the owner never wrote.
        commits = sum(bool(_COMMIT_OPEN_RE.search(s)) for s in sentences)
        if _COMMIT_OPEN_RE.search(text) or commits > 1:
            return None, "opener"
    if not aware and role not in ("opening", "closing", "") and _POSITION_RE.search(text):
        return None, "position"  # legacy unlocked order: no first/last wording
    if must and uncovered_claims(text, must, (*spec.claims, *spec.promos) if spec else ()):
        return None, "coverage"  # a must-keep claim (restriction, warranty...) was left out
    if product_name and role not in ("intro", "offer", "") and name_used_elsewhere >= 3:
        if product_name.lower() in text.lower():
            return None, "name_repeat"
    return text, None


# Reasons a model text is rejected for specific WORDING (not for length or missing facts): the offending
# sentence can be cut out instead of paying for another model call.
WORDING_REASONS = ("hard_sell", "cta", "bridge", "address", "position")


def offending_phrases(
    raw: str | None, why: str | None, approved: tuple[str, ...] = ()
) -> list[str]:
    """The wording our own fixed patterns matched for a wording rejection (never any other text)."""
    text = raw or ""
    lowered = [a.lower() for a in approved]
    patterns: tuple[re.Pattern, ...] = {
        "hard_sell": (_HARD_SELL_RE, _CHEAP_RE),
        "cta": (_CTA_RE,),
        "bridge": (_BRIDGE_RE,),
        "address": (_ADDRESS_RE,),
        "position": (_POSITION_RE,),
        "foreign_script": (_FOREIGN_SCRIPT_RE,),
        "personal_experience": (_EXPERIENCE_RE,),
    }.get(why or "", ())
    hits: list[str] = []
    for pattern in patterns:
        for match in pattern.finditer(text):
            phrase = match.group().strip().lower()
            owner_wrote = why == "hard_sell" and any(phrase in a for a in lowered)
            if phrase and not owner_wrote and phrase not in hits:
                hits.append(phrase)
    return hits[:5]


def salvage_unit_text(raw: str | None, why: str | None, **kwargs) -> str | None:
    """The text without the sentences that carry the offending wording, when what is left still passes
    every check (length, coverage of the must-keep claims...). None when nothing is salvageable."""
    if why not in WORDING_REASONS:
        return None
    phrases = offending_phrases(raw, why, tuple(kwargs.get("approved") or ()))
    if not phrases:
        return None
    sentences = re.split(r"(?<=[.!?])\s+", (raw or "").strip())
    kept = [s for s in sentences if not any(p in s.lower() for p in phrases)]
    if not kept or len(kept) == len(sentences):
        return None
    return check_unit_text(" ".join(kept), **kwargs)[0]


_SENTENCE_STOPS = ".!?…"


def rescue_unit_text(
    raw: str | None, why: str | None, *, final: bool = False, **kwargs
) -> str | None:
    """Last chance before another paid call or an extractive fallback, never inventing a word.

    - "truncated": the model's cut-off tail is dropped back to its last complete sentence.
    - "coverage" (only when `final`): the owner's own must-keep statements the model left out are appended
      verbatim after its fluent text. Every other guard still runs on the result.
    None when the text still fails any check."""
    if why not in ("truncated", "coverage") or not raw:
        return None
    text = raw
    if why == "truncated":
        cut = max(raw.rfind(stop) for stop in _SENTENCE_STOPS)
        if cut < 0:
            return None
        text = raw[: cut + 1]
    saved, rejected = check_unit_text(text, **kwargs)
    if saved is not None:
        return saved
    must = tuple(kwargs.get("must") or ())
    if rejected != "coverage" or not final or not must:
        return None
    base, _ = check_unit_text(text, **{**kwargs, "must": ()})
    if base is None:
        return None
    spec = kwargs.get("spec")
    context = (*spec.claims, *spec.promos) if spec is not None else ()
    missing = uncovered_claims(base, must, context)
    if not missing:
        return None
    extra = " ".join(
        claim if claim.rstrip().endswith(tuple(_SENTENCE_STOPS)) else claim.rstrip(" ,;:") + "."
        for claim in missing
    )
    return check_unit_text(f"{base} {extra}", **kwargs)[0]


_RETRY_HINTS = {
    "hard_sell": "Bản trước bị loại vì dùng lời thúc giục hoặc khoe quá đà: {phrases}. Viết lại, "
    "KHÔNG dùng các cụm đó hay ý tương tự; giọng nhẹ nhàng, nêu sự thật như chủ shop đã viết.",
    "cta": "Bản trước bị loại vì có lời mời nhắn tin hoặc đặt hàng ({phrases}) ở phần không được "
    "phép. Bỏ hẳn lời mời đó.",
    "bridge": "Bản trước bị loại vì nói về sản phẩm trước hoặc sau ({phrases}). Bỏ hẳn ý đó.",
    "address": "Bản trước bị loại vì xưng hô không phù hợp ({phrases}). Dùng 'mình' và 'mọi người'.",
    "position": "Bản trước bị loại vì nói về thứ tự phần ({phrases}). Bỏ hẳn ý đó.",
}


def retry_hint(why: str | None, raw: str | None, approved: tuple[str, ...] = ()) -> str:
    """What to tell the model on the second attempt: the exact wording to drop, not just a reason code."""
    if why == "foreign_script":
        bad = ", ".join(_FOREIGN_SCRIPT_RE.findall(raw or ""))[:80]
        return f"\nBản trước lẫn ký tự khác tiếng Việt: {bad}. Chỉ dùng tiếng Việt, viết lại trọn câu, giữ đúng thông tin."
    if why == "empty":
        return "\nBản trước rỗng. Trả về lời thoại tiếng Việt hoàn chỉnh, không trả suy nghĩ hay chuỗi rỗng."
    if why == "personal_experience":
        return (
            "\nBản trước bịa trải nghiệm cá nhân: "
            + ", ".join(_EXPERIENCE_RE.findall(raw or ""))[:100]
            + ". Bỏ trải nghiệm đó, chỉ nêu thông tin được phép."
        )
    if why == "opener":
        return (
            "\nBản trước mở đầu bằng 'Mình cam kết' hoặc 'Cam kết'. Viết lại, mở bằng cách khác "
            "(từ lợi ích của khách hoặc câu khách hay hỏi), không dùng từ 'cam kết' để mở câu."
        )
    if why == "name_repeat":
        return "\nBản trước lặp tên sản phẩm đầy đủ. Gọi là 'mẫu này' hoặc loại sản phẩm; không lặp tên thương hiệu."
    template = _RETRY_HINTS.get(why or "")
    phrases = offending_phrases(raw, why, approved)
    if template and phrases:
        return "\n" + template.format(phrases=", ".join(f"'{p}'" for p in phrases))
    return f"\nBản trước không đạt kiểm tra: {why}. Sửa đúng lỗi, kết thúc trọn câu."


def clean_unit_text(raw: str | None, **kwargs) -> str | None:
    return check_unit_text(raw, **kwargs)[0]


def _facts_block(product: ProductBrief, spec: UnitSpec, used: list[str]) -> str:
    lines = [f"Tên sản phẩm: {product.name or product.product_id}"]
    if spec.role not in ("intro", "offer"):
        # The name is context only: a part that repeats it is rejected (name_repeat) and falls back
        # to the verbatim owner text.
        lines.append(
            "Không nhắc tên sản phẩm này trong phần này; gọi là 'mẫu này' hoặc loại sản phẩm."
        )
    if spec.role == "intro":
        lines += [f"{k}: {v}" for k, v in product.info if v]
    if spec.claims:
        lines.append(
            "Thông tin được phép nói (phải nhắc đủ các ý này, đúng sự thật, sắp xếp lại cho tự nhiên):"
        )
        lines += [f"- {c}" for c in spec.claims]
    if spec.role == "offer":
        spoken = [w for w in (spoken_price(p) for p in product.prices) if w]
        if spoken:
            lines.append("Giá được phép nói (đã đọc thành chữ): " + "; ".join(spoken))
            if len(spoken) > 1:
                lines.append("Có nhiều mức giá: nói như một khoảng giá hoặc nêu từng mức.")
        if spec.promos:
            lines.append("Ưu đãi được phép nói (giữ nguyên điều kiện/thời gian):")
            lines += [f"- {p}" for p in spec.promos]
            if any(_CONDITION_RE.search(p) for p in spec.promos):
                lines.append(
                    "Ưu đãi này CÓ điều kiện/thời gian: nói rõ điều kiện, KHÔNG nói như đang "
                    "áp dụng hôm nay, KHÔNG thúc giục."
                )
    if used:
        used = [c for c in used if not spec.revisit or c not in (*spec.claims, *spec.promos)]
    if used:
        lines.append("Đã nói ở các phần trước, TUYỆT ĐỐI không nhắc lại: " + " | ".join(used))
    return "\n".join(lines)


def _position_line(product: ProductBrief, spec: UnitSpec) -> str:
    if spec.role != "intro":
        return ""
    if spec.first_product:
        return "Đây là sản phẩm đầu tiên của buổi live: không dùng 'tiếp theo' hay 'kế tiếp'."
    if spec.bridge:
        prev = f" ({product.previous_name})" if product.previous_name else ""
        return f"Câu đầu là lời nối tự nhiên từ sản phẩm trước{prev} sang sản phẩm này."
    return "Không nhắc tới sản phẩm trước hay sau."


def build_unit_prompt(
    session: SessionBrief,
    product: ProductBrief,
    specs: list[UnitSpec],
    index: int,
    *,
    tail: str = "",
    used_ctas: frozenset[str] = frozenset(),
    before: str = "",
    after: str = "",
    used_openings: tuple[str, ...] = (),
) -> str:
    spec = specs[index]
    used = [c for s in specs[:index] for c in (*s.claims, *s.promos)]
    parts = [
        "Bạn là MC livestream bán hàng Việt Nam giàu kinh nghiệm: nói chuyện như người thật đang "
        "trò chuyện với khán giả, giọng ấm, tự nhiên, có cảm xúc, câu dài ngắn đan xen, không "
        "đọc như đọc danh sách hay đọc lại tài liệu.",
        "Viết ĐÚNG MỘT phần lời thoại để đọc thành tiếng, đủ dài và đủ ý như người nói thật "
        "(xem số câu ở vai trò bên dưới).",
        "Chỉ trả về lời thoại. Không tiêu đề, không markdown, không emoji, không chú thích, "
        "không dấu gạch ngang dài. Chỉ dùng tiếng Việt; tên thương hiệu giữ nguyên.",
        _TRUTH_RULES,
        _COMMON_RULES,
        _STYLE_RULES,
        f"Phiên live: {session.title or '(chưa đặt tên)'}"
        + (f"; shop: {session.shop_name}" if session.shop_name else "")
        + (f"; MC: {session.persona}" if session.persona else ""),
    ]
    if spec.role == "opening" and not session.shop_name:
        parts.append("Chưa biết tên shop: không nhắc tên shop.")
    if session.notes:
        parts.append(f"Ghi chú của chủ shop: {session.notes}")
    parts.append(
        f"Vai trò của phần này ({index + 1}/{len(specs)}): {_PROMPT_TITLES[spec.role]}. "
        f"{_GUIDE[spec.role]}"
    )
    if spec.target_seconds:
        # This is prompt calibration, not a second gate duration estimator.
        low = max(120, int(spec.target_seconds * _VOICE_CHARS_PER_S * 0.85))
        high = min(850, max(low, int(spec.target_seconds * _VOICE_CHARS_PER_S * 1.15)))
        parts.append(
            f"Độ dài mục tiêu: khoảng {low} đến {high} ký tự tiếng Việt. "
            "Đây là gợi ý, không kéo dài chỉ để đủ số câu hay ký tự. Ưu tiên nói đủ thông tin "
            "và điều kiện trước, bỏ câu đệm; không thêm thông số, cam kết, ưu đãi, trải nghiệm "
            "cá nhân hay sự thật mới về sản phẩm."
        )
    if spec.revisit:
        parts.append(
            "Đây là phần NHẮC LẠI cho người mới vào, KHÔNG giới thiệu lần đầu: không chào lại, "
            "không mở màn sản phẩm; mở bằng một chuyển ý nhắc lại tự nhiên, diễn đạt KHÁC phần "
            "trước; giữ nguyên số liệu, điều kiện. "
            "Không giả vờ rằng có bình luận hay khách đặt mua nếu chưa có dữ liệu."
        )
    position = _position_line(product, spec)
    if position:
        parts.append(position)
    parts.append(_facts_block(product, spec, used))
    if spec.with_cta:
        parts.append(_CTA_LINE)
    elif spec.role not in ("opening", "closing", "sizes"):
        parts.append(
            "Phần này không có lời mời mua hay nhắn tin: không dùng đặt hàng, chốt đơn, giỏ hàng, nhắn mình, inbox."
        )
    if tail:
        parts.append("Phần liền trước (không lặp lại cách diễn đạt này): " + tail)
    if before:
        parts.append("Phần liền trước trong kịch bản (tham khảo mạch, không chép lại): " + before)
    if after:
        parts.append("Phần liền sau trong kịch bản (đừng nói trước ý của phần đó): " + after)
    if used_openings:
        parts.append(
            "Cách mở đầu các phần khác đã dùng, hãy mở bằng cách khác: " + " | ".join(used_openings)
        )
    if used_ctas:
        parts.append("Các cụm kêu gọi đã dùng, đừng dùng lại: " + ", ".join(sorted(used_ctas)))
    return "\n".join(parts)


def build_unit_repair_prompt(
    session: SessionBrief,
    product: ProductBrief,
    specs: list[UnitSpec],
    index: int,
    *,
    failed_text: str,
    problems: list[str],
    tail: str = "",
    before: str = "",
    after: str = "",
    used_openings: tuple[str, ...] = (),
) -> str:
    base = build_unit_prompt(
        session,
        product,
        specs,
        index,
        tail=tail,
        before=before,
        after=after,
        used_openings=used_openings,
    )
    # Legacy long-form duration advice asks for compact prices to inflate the
    # estimator. Ordered speech requires prices in words: keep the measured
    # bound, but repair length through content rather than its notation.
    problems = [
        re.split(r"\s(?:KEEP|TRIM)\b", problem, maxsplit=1)[0]
        + " Giữ nguyên số liệu và điều kiện, giá vẫn viết bằng chữ. Nếu quá dài, bỏ câu đệm; "
        "nếu quá ngắn, diễn đạt thêm từ thông tin được phép, không bịa chi tiết."
        if "KEEP the compact price/number tokens" in problem
        else problem
        for problem in problems
    ]
    return (
        base
        + "\n\nBản trước bị từ chối:\n"
        + failed_text
        + "\nLý do:\n- "
        + "\n- ".join(problems)
        + "\nViết lại phần này, sửa đúng các lỗi trên, vẫn tự nhiên và đủ dài, không thêm thông tin sai."
    )


class LLMDeadlineError(TimeoutError):
    """The whole-job or per-call generation deadline was exceeded."""


class BoundedLLM:
    """``(prompt) -> str`` with bounded transport retries and hard deadlines.

    At most ``attempts`` transport attempts per call (default 2), a wall-clock
    limit per call and a deadline for the whole job. Content problems are not
    retried here: the unit gate and the single local repair own those.
    """

    def __init__(
        self,
        fn: Callable[[str], str] | None,
        *,
        repair_fn: Callable[[str], str] | None = None,
        attempts: int = 2,
        call_timeout_s: float = 60.0,
        job_deadline_s: float = 600.0,
        backoff_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._fn = fn
        self._repair_fn = repair_fn
        self._attempts = max(1, attempts)
        self._call_timeout_s = call_timeout_s
        self._deadline = clock() + job_deadline_s
        self._backoff_s = backoff_s
        self._clock = clock
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))

    def __call__(self, prompt: str, *, repair: bool = False) -> str:
        if self._fn is None:
            raise ConnectionError("llm_unavailable")
        last: Exception | None = None
        for attempt in range(self._attempts):
            remaining = self._deadline - self._clock()
            if remaining <= 0:
                raise LLMDeadlineError("generation deadline exceeded")
            try:
                return self._call_once(
                    prompt,
                    min(self._call_timeout_s, remaining),
                    self._repair_fn if repair and self._repair_fn is not None else self._fn,
                )
            except Exception as exc:  # noqa: BLE001 - any provider failure is a transport failure
                last = exc
                # A timed-out sync request may already be billed and still running.
                # Do not overlap another paid request; use the safe unit fallback.
                if (
                    isinstance(exc, (LLMDeadlineError, TimeoutError))
                    or attempt + 1 >= self._attempts
                ):
                    break
                self._sleep(self._backoff_s * (attempt + 1))
        assert last is not None
        raise last

    def _call_once(self, prompt: str, timeout_s: float, fn: Callable[[str], str]) -> str:
        # ponytail: a timed-out provider thread is abandoned, not killed (sync client);
        # the provider's own HTTP timeout ends it. Move to an async client to cancel.
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(fn, prompt)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout as exc:
            raise TimeoutError("llm call timed out") from exc
        finally:
            executor.shutdown(wait=False)
