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
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace

from ..compile import number_to_vietnamese_words
from ..gate.rules.commerce_claims import _vnd_amount

__all__ = [
    "BoundedLLM",
    "LLMDeadlineError",
    "ProductBrief",
    "SessionBrief",
    "UNIT_FAILED_PREFIX",
    "UnitSpec",
    "build_unit_prompt",
    "build_unit_repair_prompt",
    "clean_unit_text",
    "failed_unit_text",
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
    "assurance": (1.0, 45.0),
    "offer": (1.0, 45.0),
    "closing": (1.0, 35.0),
}
_TITLES = {
    "opening": "Mở đầu phiên live",
    "intro": "Giới thiệu sản phẩm",
    "highlight": "Điểm nổi bật",
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
        "Nói các ý được phép nói bên dưới trong 1 đến 2 câu, MỖI ý đúng một lần, giữ nguyên "
        "số liệu và ký hiệu size (S, M, L, XL...) viết liền, không tách chữ."
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
    r"chưa có (?:thông tin|khuyến mãi|ưu đãi|giá|dữ liệu)|không có (?:thông tin|khuyến mãi)"
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
_MAX_CLAIMS_PER_UNIT = 3


def role_bounds_s(role: str) -> tuple[float, float]:
    return _BOUNDS_S[role]


def role_title(role: str) -> str:
    return _TITLES[role]


def failed_unit_text(role: str) -> str:
    """Visible placeholder for a unit the model could not write.

    The angle brackets trip the TTS markup rule, so a script holding one can never
    pass the gate, be approved or be spoken until the owner rewrites that part.
    """
    return f"{UNIT_FAILED_PREFIX}: {role_title(role)}. Hãy tự viết lại phần này>"


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


def _cluster_claims(product: ProductBrief) -> tuple[list[str], list[str], list[str]]:
    """(highlights, assurances, promotions): each approved claim lands in exactly one list."""
    typed: dict[str, str] = {}
    for kind, texts in product.claims_by_type:
        for text in texts:
            typed.setdefault(text, kind)
    highlights: list[str] = []
    assurances: list[str] = []
    promos: list[str] = []
    seen: set[str] = set()
    for claim in (*product.claims, *typed, *product.discounts):
        if claim in seen:
            continue
        seen.add(claim)
        kind = typed.get(claim)
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
    return highlights, assurances, promos


def _chunks(items: list[str]) -> list[tuple[str, ...]]:
    return [
        tuple(items[i : i + _MAX_CLAIMS_PER_UNIT])
        for i in range(0, len(items), _MAX_CLAIMS_PER_UNIT)
    ]


def plan_units(
    product: ProductBrief, *, first: bool, last: bool, ordered_aware: bool = False
) -> list[UnitSpec]:
    """Adaptive, ordered units of one product from its available facts.

    opening (first product) -> intro -> one unit per claim cluster -> offer (price and
    promotions) -> closing (last product). The single soft CTA is folded into the last
    product unit. A bridge is asked of the intro only for a later product on a locked order.
    """
    highlights, assurances, promos = _cluster_claims(product)
    flags = {"first_product": first, "last_product": last}
    units = [
        *([UnitSpec("opening", **flags)] if first else []),
        UnitSpec("intro", bridge=ordered_aware and not first, **flags),
        *(UnitSpec("highlight", claims=c, **flags) for c in _chunks(highlights)),
        *(UnitSpec("assurance", claims=c, **flags) for c in _chunks(assurances)),
    ]
    if product.prices or promos:
        units.append(
            UnitSpec("offer", promos=tuple(promos), with_price=bool(product.prices), **flags)
        )
    body = [i for i, u in enumerate(units) if u.role != "opening"]
    units[body[-1]] = replace(units[body[-1]], with_cta=True)
    if last:
        units.append(UnitSpec("closing", **flags))
    return units


def clean_unit_text(
    raw: str | None, *, prices: tuple[str, ...] = (), allow_bridge: bool = False
) -> str | None:
    """One paragraph of plain speech, or ``None`` when the output is unusable.

    Unusable = empty/garbled, mentions missing information, hard-sells, or points at "the
    next product" where no bridge belongs. Approved prices written as digits become words.
    """
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw).strip().strip("\"'“”")
    text = _DASH_RE.sub(", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = _SPLIT_SIZE_RE.sub(lambda m: re.sub(r"\s+", "", m.group(1)), text)
    text = _speak_approved_prices(text, prices)
    if len(text) < 6 or len(text) > _MAX_UNIT_CHARS or not re.search(r"[^\W\d_]{2}", text):
        return None
    if _MISSING_INFO_RE.search(text) or _HARD_SELL_RE.search(text):
        return None
    if not allow_bridge and _BRIDGE_RE.search(text):
        return None
    return text


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
