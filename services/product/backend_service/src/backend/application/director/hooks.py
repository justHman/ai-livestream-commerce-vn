"""Hook pool — pre-generated engagement lines (challenge 1, hybrid approach).

Decision (confirmed): gen the pool ONCE at session init from shop/product info
(LLM call), cache it, then rotate at runtime (0ms, varied, no bot-looking
repetition). This module holds the rotation + a built-in default pool so the
Director runs before any LLM gen.

Categories: "opening" (greet/setup), "engagement" (low-traffic hooks: ask for
likes/follow, open questions), "closing" (thanks/follow). To personalize,
call `populate(category, lines)` with LLM-generated lines at session start.
"""

from __future__ import annotations

from typing import Optional

_DEFAULT_POOL = {
    "opening": [
        "Chào cả nhà, shop và MC rất vui được gặp mọi người trong phiên live hôm nay nha!",
        "Mọi người nhớ thả tim, chia sẻ, comment và follow để không bỏ lỡ deal của shop nhé!",
        "Hôm nay shop sẽ giới thiệu lần lượt các sản phẩm theo agenda, mọi người ở lại xem và đặt câu hỏi nha!",
    ],
    "engagement": [
        "Mọi người thả tim cho shop để được giá tốt hơn nha!",
        "Ai đang xem thì cho shop xin một comment để biết nha!",
        "Mọi người thấy sản phẩm này màu nào đẹp hơn, comment cho shop biết nhé!",
        "Nhớ follow kênh để không bỏ lỡ deal hôm nay nha cả nhà!",
    ],
    "closing": [
        "Cảm ơn cả nhà đã xem live! Nhớ follow để đón phiên sau nha!",
        "Phiên live sắp kết thúc rồi, mọi người nhanh tay chốt đơn nha!",
        "Cảm ơn mọi người! Hẹn gặp lại ở phiên live tiếp theo nhé!",
    ],
}


class HookPool:
    """Rotating pool of engagement lines per category."""

    def __init__(self, pool: Optional[dict[str, list[str]]] = None) -> None:
        self._pool: dict[str, list[str]] = {k: list(v) for k, v in (pool or _DEFAULT_POOL).items()}
        self._cycle_cursor: dict[str, int] = {}
        self._rebuild_cycles()

    def _rebuild_cycles(self) -> None:
        self._cycle_cursor = {k: 0 for k, v in self._pool.items() if v}

    def populate(self, category: str, lines: list[str]) -> None:
        """Replace a category's lines (e.g. LLM-generated from shop info at init)."""
        if lines:
            self._pool[category] = list(lines)
            self._rebuild_cycles()

    def get(self, category: str, index: int) -> str:
        lines = self._pool.get(category) or self._pool.get("engagement") or []
        if not lines:
            return "Cảm ơn mọi người đã theo dõi nha!"
        return lines[index % len(lines)]

    def next_hook(self, category: str) -> str:
        lines = self._pool.get(category) or self._pool.get("engagement") or []
        if not lines:
            return "Cảm ơn mọi người đã theo dõi nha!"
        cursor = self._cycle_cursor.get(category, 0)
        self._cycle_cursor[category] = cursor + 1
        return lines[cursor % len(lines)]
