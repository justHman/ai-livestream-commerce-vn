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
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace

from ..compile import compile_spoken_text, expand_vietnamese_number, number_to_vietnamese_words
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
_BOUNDS_S: dict[str, tuple[float, float]] = {
    "opening": (1.0, 40.0),
    "intro": (1.0, 40.0),
    "highlight": (1.0, 45.0),
    "sizes": (1.0, 45.0),
    "assurance": (1.0, 45.0),
    "offer": (1.0, 45.0),
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
        "Chào khán giả, giới thiệu ngắn về buổi live, tạo không khí thân thiện và MỜI khán giả "
        "bình luận, đặt câu hỏi (ví dụ hỏi size, hỏi giá) ngay trong phiên. Không nói giá, "
        "không nêu tính năng, không nhắc sản phẩm cụ thể."
    ),
    "intro": (
        "Giới thiệu sản phẩm bằng 1 đến 2 câu tự nhiên từ thông tin được phép nói (tên, "
        "thương hiệu, loại, mô tả). Chưa nói giá."
    ),
    "highlight": (
        "Chọn các ý quan trọng nhất bên dưới, nói trong 1 đến 3 câu ngắn; MỖI CÂU CHỈ MỘT CHỦ "
        "ĐỀ, không nối các ý khác chủ đề (ví dụ size với cách giặt) bằng 'và'. Giữ nguyên số "
        "liệu và ký hiệu size (S, M, L, XL...) viết liền, không tách chữ."
    ),
    "sizes": (
        "Đây là bảng size: nói thật dễ nghe bằng khoảng chiều cao và cân nặng, tối đa 3 mức "
        "size trong một câu, không đọc dài chuỗi số; có thể mời mọi người nhắn chiều cao cân "
        "nặng để mình tư vấn size. Không nói gì ngoài size."
    ),
    "assurance": (
        "Nói các ý bên dưới một cách trung thực, tự nhiên; nếu là điều kiện hay hạn chế thì "
        "nói rõ ràng, nhẹ nhàng, không bán gắt. MỖI ý đúng một lần."
    ),
    "offer": (
        "Nói giá (đã đọc thành chữ bên dưới, chép đúng, không viết lại thành số) và ưu đãi nếu "
        "có. Ưu đãi có điều kiện hay thời gian thì giữ NGUYÊN điều kiện/thời gian như thông "
        "tin, không nói như đang áp dụng ngay hôm nay."
    ),
    "closing": "Cảm ơn khán giả, nhắc theo dõi và hẹn gặp lại. Không giới thiệu thêm sản phẩm.",
}
_CTA_LINE = (
    "Cuối phần này thêm MỘT lời mời nhẹ nhàng (ví dụ bạn nào quan tâm thì nhắn mình hoặc xem "
    "giỏ hàng). Không dùng: chốt đơn ngay, đặt ngay, mua ngay, nhanh tay, không bỏ lỡ, số "
    "lượng có hạn."
)
_STYLE_RULES = (
    "Luôn gọi người xem là 'mọi người' hoặc 'các bạn' (không dùng quý vị, quý khách, anh, "
    "chị, em, bạn nam, bạn nữ). Viết đúng chính tả. Chỉ nhắc tên sản phẩm đầy đủ ở phần giới "
    "thiệu; các phần sau gọi 'mẫu này' hoặc loại sản phẩm. Câu phải kết thúc trọn vẹn."
)
_COMMON_RULES = (
    "Không bao giờ nói rằng thiếu thông tin (ví dụ 'chưa có thông tin', 'chưa có khuyến mãi'): "
    "nếu không có gì để nói thì bỏ qua ý đó."
)

UNIT_FAILED_PREFIX = "<Phần này chưa soạn được"
_MAX_UNIT_CHARS = 600
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
    r"|chốt ngay|order ngay",
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
_MAX_MUST_PER_UNIT = 4  # must-keep (assurance) and untyped claims per unit
_MAX_PROMOS_PER_UNIT = 3
_MAX_PARAGRAPH_CLAIMS = 3  # fallback paragraphs
_MAX_SIZE_ROWS = 40
_SIZE_ROW_RE = re.compile(
    r"(?:cao|chiều cao)[^.]*?(?:cân nặng|nặng)|\bsize\s*[A-Za-z0-9]+\b[^.]*\d", re.IGNORECASE
)
_SENTENCE_END_RE = re.compile(r"[.!?…][\"'”’)\]]*$")
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
_POSITION_RE = re.compile(
    r"cuối cùng của buổi|(?:sản phẩm|món) (?:đầu tiên|cuối cùng)", re.IGNORECASE
)
_COVERAGE = 0.6
_CTA_RE = re.compile(
    r"đặt hàng|giỏ hàng|nhắn mình|nhắn tin|inbox|bình luận để|chốt đơn", re.IGNORECASE
)
_TYPOS = {"thoải chọn": "thoải mái chọn"}
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


def _paragraphs(items: list[str], max_s: float) -> list[str]:
    """Short paragraphs: <= 3 claims and within the duration bound (split, never exceed)."""
    paras: list[list[str]] = []
    for item in items:
        cur = paras[-1] if paras else None
        if cur is not None and len(cur) < _MAX_PARAGRAPH_CLAIMS:
            joined = compile_spoken_text(" ".join([*cur, item])).spoken_text
            if spoken_duration_ms(joined) / 1000.0 <= max_s:
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


def fallback_unit_text(spec: "UnitSpec", product: "ProductBrief") -> str:
    """Deterministic extractive unit for a part the model could not write.

    Only approved claim sentences (ALL of the unit's claims, verbatim, in short paragraphs;
    size rows as many as fit, restricted ones always) and price words; fixed greetings carry
    no facts. No LLM, no invented facts, no markup.
    """
    name = product.name or product.product_id
    max_s = role_bounds_s(spec.role)[1]
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
        text = "\n\n".join(_size_paragraphs(spec.claims, set(spec.must), max_s))
    else:
        text = "\n\n".join(_paragraphs(list(spec.claims), max_s))
    return compile_spoken_text(text or f"Mình giới thiệu thêm về {name}.").spoken_text


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

    return _PRICE_TOKEN_RE.sub(swap, text)


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
    if len(rows) >= 3:
        # restricted rows first so the row cap can never remove them; the rest keep their order
        keep = [r for r in rows if r in must][:_MAX_SIZE_ROWS]
        keep += [r for r in rows if r not in keep][: _MAX_SIZE_ROWS - len(keep)]
        sizes = [r for r in rows if r in keep]
        highlights = [c for c in highlights if c not in rows]
    return {
        "typed": typed_mode,
        "kinds": typed,
        "highlights": highlights,
        "sizes": sizes,
        "assurances": assurances,
        "promos": promos,
        "must": must,
    }


def plan_units(
    product: ProductBrief, *, first: bool, last: bool, ordered_aware: bool = False
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
            if len(keep) >= _MAX_CLAIMS_PER_UNIT:
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
        *(spec("assurance", chunk) for chunk in _chunks(ordered_assurance, _MAX_MUST_PER_UNIT)),
    ]
    has_price = any(spoken_price(p) for p in product.prices)
    promo_chunks = _chunks(c["promos"], _MAX_PROMOS_PER_UNIT) or ([()] if has_price else [])
    for i, chunk in enumerate(promo_chunks):
        units.append(spec("offer", promos=chunk, with_price=has_price and i == 0))
    body = [i for i, u in enumerate(units) if u.role != "opening"]
    units[body[-1]] = replace(units[body[-1]], with_cta=True)
    if last:
        units.append(spec("closing"))
    return units


def _fold(text: str) -> str:
    """Lowercase without diacritics (đ -> d)."""
    folded = unicodedata.normalize("NFD", text.lower().replace("đ", "d"))
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Mn")


def claim_covered(claim: str, text: str) -> bool:
    """Lexical coverage: every number appears exactly (digits or spoken words) and at least
    60% of the other content tokens appear, after case/diacritic folding."""
    haystack = _fold(text)
    tokens = set(re.findall(r"\w+", haystack))
    for number in re.findall(r"\d[\d.,]*", claim):
        number = number.rstrip(".,")
        # spoken digits keep their tone marks ("tám" must not match "tâm")
        spoken = (
            expand_vietnamese_number(number).lower()
            if number.replace(".", "").replace(",", "").isdigit()
            else ""
        )
        digits = re.search(r"(?<![0-9])" + re.escape(number) + r"(?![0-9])", haystack)
        said = spoken and re.search(r"(?<!\w)" + re.escape(spoken) + r"(?!\w)", text.lower())
        if not digits and not said:
            return False
    words = [w for w in re.findall(r"[^\W\d_]{2,}", _fold(claim))]
    if not words:
        return True
    return sum(1 for w in words if w in tokens) >= _COVERAGE * len(words)


def uncovered_claims(text: str, claims: tuple[str, ...]) -> list[str]:
    return [c for c in claims if not claim_covered(c, text)]


def claims_not_spoken(approved: tuple[str, ...], saved_text: str) -> int:
    """Approved claims/discounts that the SAVED script text does not cover (owner-visible)."""
    return len(uncovered_claims(saved_text, tuple(dict.fromkeys(approved))))


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
    mashup, name_repeat. Approved prices written as digits become words.
    """
    if not raw:
        return None, "empty"
    text = _FENCE_RE.sub("", raw).strip().strip("\"'“”")
    text = _DASH_RE.sub(", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    for wrong, right in _TYPOS.items():
        text = text.replace(wrong, right)
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
    if _HARD_SELL_RE.search(text):
        return None, "hard_sell"
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
    if not aware and role not in ("opening", "closing", "") and _POSITION_RE.search(text):
        return None, "position"  # legacy unlocked order: no first/last wording
    if must and uncovered_claims(text, must):
        return None, "coverage"  # a must-keep claim (restriction, warranty...) was left out
    if product_name and role not in ("intro", "") and name_used_elsewhere >= 2:
        if product_name.lower() in text.lower():
            return None, "name_repeat"
    return text, None


def clean_unit_text(raw: str | None, **kwargs) -> str | None:
    return check_unit_text(raw, **kwargs)[0]


def _facts_block(product: ProductBrief, spec: UnitSpec, used: list[str]) -> str:
    lines = [f"Tên sản phẩm: {product.name or product.product_id}"]
    if spec.role == "intro":
        lines += [f"{k}: {v}" for k, v in product.info if v]
    if spec.claims:
        lines.append("Thông tin được phép nói (dùng hết, mỗi ý một lần):")
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
) -> str:
    spec = specs[index]
    used = [c for s in specs[:index] for c in (*s.claims, *s.promos)]
    parts = [
        "Bạn là MC livestream bán hàng Việt Nam, giọng thân thiện, tự nhiên, nói ngắn gọn.",
        "Viết ĐÚNG MỘT phần lời thoại để đọc thành tiếng, gồm 1 đến 2 câu ngắn.",
        "Chỉ trả về lời thoại. Không tiêu đề, không markdown, không emoji, không chú thích, "
        "không dấu gạch ngang dài.",
        "Mọi con số, giá, ưu đãi, tính năng PHẢI lấy từ phần thông tin được phép nói; "
        "tuyệt đối không bịa thêm. Giá luôn nói bằng chữ, không viết số.",
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
        f"Vai trò của phần này ({index + 1}/{len(specs)}): {role_title(spec.role)}. "
        f"{_GUIDE[spec.role]}"
    )
    position = _position_line(product, spec)
    if position:
        parts.append(position)
    parts.append(_facts_block(product, spec, used))
    if spec.with_cta:
        parts.append(_CTA_LINE)
    if tail:
        parts.append("Phần liền trước (không lặp lại cách diễn đạt này): " + tail)
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
) -> str:
    base = build_unit_prompt(session, product, specs, index, tail=tail)
    return (
        base
        + "\n\nBản trước bị từ chối:\n"
        + failed_text
        + "\nLý do:\n- "
        + "\n- ".join(problems)
        + "\nViết lại phần này, sửa đúng các lỗi trên, giữ ngắn gọn, chỉ dùng thông tin được phép nói."
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
        attempts: int = 2,
        call_timeout_s: float = 60.0,
        job_deadline_s: float = 600.0,
        backoff_s: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._fn = fn
        self._attempts = max(1, attempts)
        self._call_timeout_s = call_timeout_s
        self._deadline = clock() + job_deadline_s
        self._backoff_s = backoff_s
        self._clock = clock
        self._sleep = sleep or (lambda seconds: time.sleep(seconds))

    def __call__(self, prompt: str) -> str:
        if self._fn is None:
            raise ConnectionError("llm_unavailable")
        last: Exception | None = None
        for attempt in range(self._attempts):
            remaining = self._deadline - self._clock()
            if remaining <= 0:
                raise LLMDeadlineError("generation deadline exceeded")
            try:
                return self._call_once(prompt, min(self._call_timeout_s, remaining))
            except Exception as exc:  # noqa: BLE001 - any provider failure is a transport failure
                last = exc
                if isinstance(exc, LLMDeadlineError) or attempt + 1 >= self._attempts:
                    break
                self._sleep(self._backoff_s * (attempt + 1))
        assert last is not None
        raise last

    def _call_once(self, prompt: str, timeout_s: float) -> str:
        # ponytail: a timed-out provider thread is abandoned, not killed (sync client);
        # the provider's own HTTP timeout ends it. Move to an async client to cancel.
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._fn, prompt)
        try:
            return future.result(timeout=timeout_s)
        except FutureTimeout as exc:
            raise TimeoutError("llm call timed out") from exc
        finally:
            executor.shutdown(wait=False)
