"""Ordered session script: fixed unit roles, unit prompts and a bounded LLM call.

One generated unit is one short spoken part (1-3 sentences) with a fixed role:
session opening (first product only), intro, selling points, offer/price,
trust, CTA and session closing (last product only). Roles are fixed by the
backend, so no free-form planning call is spent and no model can change how
many parts a product has. Prompts carry the authoritative product facts only;
the deterministic unit gate decides whether the text may be kept.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass

__all__ = [
    "BoundedLLM",
    "LLMDeadlineError",
    "ProductBrief",
    "SessionBrief",
    "UNIT_FAILED_PREFIX",
    "build_unit_prompt",
    "build_unit_repair_prompt",
    "clean_unit_text",
    "failed_unit_text",
    "plan_roles",
    "role_bounds_s",
    "role_title",
]

# Spoken-duration bounds (seconds, canonical estimator) per role. Wide on purpose:
# the owner edits the text; these only reject empty/one-word and runaway output.
_BOUNDS_S: dict[str, tuple[float, float]] = {
    "opening": (3.0, 40.0),
    "intro": (3.0, 40.0),
    "benefit": (3.0, 45.0),
    "offer": (3.0, 45.0),
    "trust": (3.0, 40.0),
    "cta": (2.0, 35.0),
    "closing": (2.0, 35.0),
}
_TITLES = {
    "opening": "Mở đầu phiên live",
    "intro": "Giới thiệu sản phẩm",
    "benefit": "Điểm nổi bật",
    "offer": "Giá và ưu đãi",
    "trust": "Tạo niềm tin",
    "cta": "Chốt đơn",
    "closing": "Lời kết phiên live",
}
_GUIDE = {
    "opening": (
        "Chào khán giả, giới thiệu ngắn về buổi live và shop, tạo không khí thân thiện. "
        "Không nói giá, không nêu tính năng sản phẩm."
    ),
    "intro": "Giới thiệu sản phẩm bằng một câu định vị hấp dẫn, gợi tò mò. Chưa nói giá.",
    "benefit": (
        "Nêu đúng MỘT điểm nổi bật (lấy từ thông tin được phép nói), nói gần như nguyên văn, "
        "mở đầu bằng một cụm nối tự nhiên với ý trước."
    ),
    "offer": "Nêu giá đúng như thông tin được phép nói và ưu đãi nếu có. Không tự tạo giá hay quà.",
    "trust": (
        "Tạo niềm tin bằng thông tin được phép nói (hoặc lời trấn an chung, không có số liệu "
        "mới). Không cam kết điều gì ngoài thông tin được phép nói."
    ),
    "cta": "Kêu gọi chốt đơn tự nhiên, một lời mời ngắn. Không ép mua.",
    "closing": "Cảm ơn khán giả, nhắc theo dõi và hẹn gặp lại. Không giới thiệu thêm sản phẩm.",
}

UNIT_FAILED_PREFIX = "<Phần này chưa soạn được"
_MAX_UNIT_CHARS = 700
_DASH_RE = re.compile(r"\s*[—–]\s*")
_FENCE_RE = re.compile(r"```[a-z]*|^[#>*\-\s]+(?=\S)", re.MULTILINE)


def role_bounds_s(role: str) -> tuple[float, float]:
    return _BOUNDS_S[role]


def role_title(role: str) -> str:
    return _TITLES[role]


def plan_roles(*, first: bool, last: bool, claim_count: int) -> list[str]:
    """Fixed, ordered unit roles of one product.

    The session opening belongs to the first product and the closing to the last.
    One selling point per allowed claim (2 to 3 when there are claims, one generic
    otherwise), so the unit count never depends on model output.
    """
    benefits = min(3, max(2, claim_count)) if claim_count else 1
    return [
        *(["opening"] if first else []),
        "intro",
        *(["benefit"] * benefits),
        "offer",
        "trust",
        "cta",
        *(["closing"] if last else []),
    ]


def failed_unit_text(role: str) -> str:
    """Visible placeholder for a unit the model could not write.

    The angle brackets trip the TTS markup rule, so a script holding one can never
    pass the gate, be approved or be spoken until the owner rewrites that part.
    """
    return f"{UNIT_FAILED_PREFIX}: {role_title(role)}. Hãy tự viết lại phần này>"


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
    claims: tuple[str, ...] = ()
    previous_name: str | None = None  # only when the set is ORDER_AWARE
    next_name: str | None = None  # only when the set is ORDER_AWARE


def clean_unit_text(raw: str | None) -> str | None:
    """One paragraph of plain speech, or ``None`` when the output is empty/garbled."""
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw).strip().strip("\"'“”")
    text = _DASH_RE.sub(", ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 6 or len(text) > _MAX_UNIT_CHARS or not re.search(r"[^\W\d_]{2}", text):
        return None
    return text


def _facts_block(product: ProductBrief, role: str, benefit_no: int) -> str:
    lines = [f"Tên sản phẩm: {product.name or product.product_id}"]
    if role == "offer":
        if product.prices:
            lines.append("Giá được phép nói (chép đúng): " + "; ".join(product.prices))
        else:
            lines.append("Không có giá được phép nói: KHÔNG nêu bất kỳ con số giá nào.")
        if product.discounts:
            lines.append("Ưu đãi được phép nói: " + "; ".join(product.discounts))
    elif role in ("benefit", "trust") and product.claims:
        # trust reuses the claim after the last selling point so the two never repeat.
        pick = benefit_no if role == "benefit" else benefit_no + 1
        claim = product.claims[pick % len(product.claims)]
        lines.append(f"Thông tin được phép nói: {claim}")
    elif role in ("benefit", "trust"):
        lines.append(
            "Không có thông tin cụ thể: chỉ nói chung chung, KHÔNG nêu tính năng hay số liệu."
        )
    return "\n".join(lines)


def _bridge_line(product: ProductBrief, role: str, first_product: bool, last_product: bool) -> str:
    if role == "intro" and not first_product:
        prev = f" ({product.previous_name})" if product.previous_name else ""
        return f"Câu đầu là lời nối từ sản phẩm trước{prev} sang sản phẩm này."
    if role == "cta" and not last_product:
        nxt = f" là {product.next_name}" if product.next_name else ""
        return f"Kết thúc bằng một cụm dẫn nhẹ sang sản phẩm tiếp theo{nxt}."
    return ""


def build_unit_prompt(
    session: SessionBrief,
    product: ProductBrief,
    roles: list[str],
    index: int,
    *,
    tail: str = "",
    used_ctas: frozenset[str] = frozenset(),
) -> str:
    role = roles[index]
    benefit_no = sum(1 for r in roles[:index] if r == "benefit")
    first_product = "opening" in roles
    last_product = "closing" in roles
    parts = [
        "Bạn là MC livestream bán hàng Việt Nam, giọng thân thiện, tự nhiên, nói ngắn gọn.",
        "Viết ĐÚNG MỘT phần lời thoại để đọc thành tiếng, gồm 1 đến 3 câu (khoảng 8 đến 25 giây).",
        "Chỉ trả về lời thoại. Không tiêu đề, không markdown, không emoji, không chú thích, "
        "không dấu gạch ngang dài.",
        "Mọi con số, giá, ưu đãi, tính năng PHẢI lấy từ phần thông tin được phép nói; "
        "tuyệt đối không bịa thêm.",
        f"Phiên live: {session.title or '(chưa đặt tên)'}"
        + (f"; shop: {session.shop_name}" if session.shop_name else "")
        + (f"; MC: {session.persona}" if session.persona else ""),
    ]
    if session.notes:
        parts.append(f"Ghi chú của chủ shop: {session.notes}")
    parts.append(
        f"Vai trò của phần này ({index + 1}/{len(roles)}): {role_title(role)}. {_GUIDE[role]}"
    )
    bridge = _bridge_line(product, role, first_product, last_product)
    if bridge:
        parts.append(bridge)
    parts.append(_facts_block(product, role, benefit_no))
    if tail:
        parts.append("Phần liền trước (không lặp lại cách diễn đạt này): " + tail)
    if used_ctas:
        parts.append("Các cụm kêu gọi đã dùng, đừng dùng lại: " + ", ".join(sorted(used_ctas)))
    return "\n".join(parts)


def build_unit_repair_prompt(
    session: SessionBrief,
    product: ProductBrief,
    roles: list[str],
    index: int,
    *,
    failed_text: str,
    problems: list[str],
    tail: str = "",
) -> str:
    base = build_unit_prompt(session, product, roles, index, tail=tail)
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
