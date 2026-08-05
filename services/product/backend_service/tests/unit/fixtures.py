"""Shared offline fixtures for backend unit tests (OpenSpec 1.50).

Mock product catalog for Director/run-plan tests. The legacy
``core/debug/mock_data.py`` moved to the workbench fixture files (1.45/1.48);
service tests keep a minimal service-local copy so they stay self-contained.
"""

from __future__ import annotations

MOCK_PRODUCTS = [
    {
        "id": "P004",
        "name": "Áo hoodie HeyGen màu trắng",
        "description": "Áo hoodie trơn màu trắng kem, có mũ trùm, in logo HeyGen tinh tế ở ngực trái.",
        "price": 350000,
        "original_price": 500000,
        "promotion": "Giảm 30% — chỉ 350k (giá gốc 500k)",
        "colors": ["trắng kem"],
        "sizes": ["S", "M", "L", "XL"],
        "material": "nỉ cotton",
        "shipping": "Freeship toàn quốc cho đơn từ 250k",
        "warranty": "Đổi trả miễn phí trong 7 ngày nếu lỗi từ nhà sản xuất",
        "in_stock": True,
        "stock_total": 120,
        "ref_image": "image_20509e.png",
        "features": [
            "áo hoodie có mũ",
            "logo HeyGen",
            "màu trắng kem",
            "dài tay",
            "phong cách tối giản",
        ],
    },
    {
        "id": "P001",
        "name": "Kem chống nắng La Roche-Posay SPF50+",
        "description": "Kem chống nắng phổ rộng SPF50+, chống nước, phù hợp da nhạy cảm",
        "price": 329000,
        "original_price": 490000,
        "promotion": "Giảm 33% — chỉ 329k (giá gốc 490k)",
        "colors": [],
        "sizes": [],
        "material": None,
        "shipping": "Freeship đơn từ 200k, giao 2-4 ngày",
        "warranty": "Đổi trả trong 7 ngày nếu chưa sử dụng",
        "in_stock": True,
        "stock_total": 150,
        "ref_image": None,
        "features": ["SPF50+", "chống nước", "phổ rộng", "da nhạy cảm", "không bết dính"],
    },
    {
        "id": "P002",
        "name": "Serum Vitamin C 20% làm sáng da",
        "description": "Serum Vitamin C 20% + HA, mờ thâm nám, sáng da, chai 30ml",
        "price": 189000,
        "original_price": 299000,
        "promotion": "Flash sale: 189k (giá gốc 299k) — chỉ còn 50 chai!",
        "colors": [],
        "sizes": [],
        "material": None,
        "shipping": "Freeship đơn từ 200k",
        "warranty": "Đổi trả 7 ngày",
        "in_stock": True,
        "stock_total": 50,
        "ref_image": None,
        "features": ["vitamin C 20%", "mờ thâm", "sáng da", "HA dưỡng ẩm"],
    },
]
