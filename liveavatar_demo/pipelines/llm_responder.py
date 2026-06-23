"""Mock LLM Responder — hardcoded Vietnamese commerce response templates.

Generates text responses for common livestream commerce scenarios.
When a real LLM (Qwen3-4B) is available, swap this module out.

Response categories:
- greeting / welcome
- product introduction
- price inquiry
- promotion / deal
- closing / farewell
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class Product:
    """A single product in the catalog."""

    id: str
    name: str
    price: int  # VND
    original_price: int
    description: str
    features: list[str]


# ── Vietnamese commerce templates ─────────────────────────────────────

GREETING_TEMPLATES = [
    "Xin chào tất cả các bạn! Chào mừng đến với phiên livestream hôm nay! 🎉",
    "Chào các bạn! Hôm nay mình có những deal cực hot cho các bạn nhé! 🔥",
    "Hello mọi người! Mấy bà ơi, hôm nay sale khủng lắm nha! 💕",
]

PRODUCT_INTRO_TEMPLATES = [
    "Mấy bà ơi, sản phẩm {name} này cực kỳ hot luôn! {features} — giá chỉ {price}k nè!",
    "Đây nè, {name}! {description} Giá gốc {original_price}k nhưng hôm nay chỉ {price}k thôi! 😱",
    "Sản phẩm {name} — {features}. Deal hôm nay: {price}k thay vì {original_price}k nha! 🛍️",
]

PRICE_TEMPLATES = [
    "Giá {name} chỉ {price}k thôi mấy bà! So với giá gốc {original_price}k thì giảm tận {discount}% luôn! 😍",
    "{name} đang sale sập sàn: {price}k (gốc {original_price}k). Tiết kiệm {discount}% luôn nè!",
]

PROMO_TEMPLATES = [
    "🔥 FLASH DEAL! Mua {name} trong 5 phút tới nhận thêm voucher 50k nha!",
    "⏰ Countdown bắt đầu! {name} chỉ {price}k cho 50 đơn đầu tiên! Nhanh tay nha!",
    "🎁 Mua 2 {name} tặng 1 mini size! Chỉ áp dụng trong livestream thôi nha!",
]

CLOSING_TEMPLATES = [
    "Cảm ơn các bạn đã xem livestream hôm nay! Nhớ follow kênh để không bỏ lỡ deal nhé! 💕",
    "Livestream sắp kết thúc rồi! Các bạn nhanh tay chốt đơn {name} trước khi hết flash sale nha! 🏃‍♀️",
    "Cảm ơn mọi người! Hẹn gặp lại ở phiên livestream tiếp theo nha! Đừng quên theo dõi kênh nè! ✨",
]


class MockLLMResponder:
    """Template-based Vietnamese commerce responder.

    Parameters
    ----------
    catalog_path : Path or None
        Path to product_catalog.yaml. If None, uses built-in demo products.
    """

    def __init__(self, catalog_path: Optional[Path] = None) -> None:
        self.products = self._load_catalog(catalog_path)
        self._rng = random.Random(42)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def respond(self, viewer_message: str) -> str:
        """Generate a commerce response based on viewer message keywords.

        Parameters
        ----------
        viewer_message : str
            Chat message from a viewer.

        Returns
        -------
        str
            Vietnamese commerce response.
        """
        msg_lower = viewer_message.lower()

        # Detect intent by keyword matching
        product = self._match_product(msg_lower)

        if any(w in msg_lower for w in ["xin chào", "hello", "hi", "chào"]):
            return self._rng.choice(GREETING_TEMPLATES)

        if any(w in msg_lower for w in ["giá", "bao nhiêu", "tiền", "price"]):
            if product:
                return self._format_price(product)
            return self._format_price(self._rng.choice(self.products))

        if any(w in msg_lower for w in ["khuyến mãi", "sale", "giảm", "deal", "promo"]):
            if product:
                return self._format_promo(product)
            return self._format_promo(self._rng.choice(self.products))

        if any(w in msg_lower for w in ["tạm biệt", "bye", "kết thúc"]):
            if product:
                return self._rng.choice(CLOSING_TEMPLATES).format(
                    name=product.name
                )
            return self._rng.choice(CLOSING_TEMPLATES)

        # Default: introduce a random product
        if product:
            return self._format_product_intro(product)
        return self._format_product_intro(self._rng.choice(self.products))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _match_product(self, message: str) -> Optional[Product]:
        """Find a product mentioned in the viewer message."""
        for p in self.products:
            # Match by name keywords or product ID
            name_words = p.name.lower().split()
            if any(w in message for w in name_words if len(w) > 2):
                return p
            if p.id in message:
                return p
        return None

    def _format_product_intro(self, p: Product) -> str:
        features_str = ", ".join(p.features[:3])
        return self._rng.choice(PRODUCT_INTRO_TEMPLATES).format(
            name=p.name,
            description=p.description,
            features=features_str,
            price=p.price // 1000,
            original_price=p.original_price // 1000,
        )

    def _format_price(self, p: Product) -> str:
        discount = round((1 - p.price / p.original_price) * 100)
        return self._rng.choice(PRICE_TEMPLATES).format(
            name=p.name,
            price=p.price // 1000,
            original_price=p.original_price // 1000,
            discount=discount,
        )

    def _format_promo(self, p: Product) -> str:
        return self._rng.choice(PROMO_TEMPLATES).format(
            name=p.name,
            price=p.price // 1000,
        )

    @staticmethod
    def _load_catalog(catalog_path: Optional[Path]) -> list[Product]:
        """Load product catalog from YAML or return built-in demo products."""
        if catalog_path and catalog_path.exists():
            with open(catalog_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            return [Product(**p) for p in data.get("products", [])]

        # Built-in demo products
        return [
            Product(
                id="P001",
                name="Kem chống nắng La Roche-Posay",
                price=289000,
                original_price=459000,
                description="Kem chống nắng SPF50+ PA++++ bảo vệ da toàn diện",
                features=["SPF50+", "chống nước 80 phút", "không bóng dầu"],
            ),
            Product(
                id="P002",
                name="Serum Vitamin C Ordinaire",
                price=329000,
                original_price=490000,
                description="Serum vitamin C 23% làm sáng da, mờ thâm nám",
                features=["23% Vitamin C", "làm sáng da", "mờ thâm"],
            ),
            Product(
                id="P003",
                name="Mặt nạ thủy tinh Hada Labo",
                price=89000,
                original_price=149000,
                description="Mặt nạ dưỡng ẩm HA cấp tốc, 5 phút da căng bóng",
                features=["5 phút áp dụng", "5 loại HA", "căng bóng tức thì"],
            ),
            Product(
                id="P004",
                name="Sữa rửa mặt Senka",
                price=119000,
                original_price=169000,
                description="Sữa rửa mặt tạo bọt mịn, làm sạch sâu nhưng không khô da",
                features=["tạo bọt mịn", "làm sạch sâu", "không khô da"],
            ),
        ]
