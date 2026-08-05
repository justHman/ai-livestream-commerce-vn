"""Benchmark fixture data — self-contained copies of the Workbench fixtures.

Task 1.79 removed ``core/debug/mock_data.py``; the canonical product and
viewer-message fixtures live in ``workbench/src/fixtures/*.json``. This module
loads them (products normalized to the legacy dict shape) plus the four
benchmark-only Stage 2 comment texts that the Workbench viewer set does not
carry. Benchmark lanes stay runnable without the Workbench tree by falling
back to a static copy if the JSON files are absent.
"""

from __future__ import annotations

import json
import pathlib

_WORKBENCH_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "workbench" / "src" / "fixtures"

# Four Stage 2 benchmark comment texts not present in the Workbench viewer set.
_BENCHMARK_ONLY_MSGS = [
    "Serum vitamin C bao nhiêu tiền ạ?",
    "Áo hoodie có mũ đôi không shop?",
    "Áo hoodie có túi kangaroo không?",
    "Áo hoodie giá sao shop?",
]

# Static fallback (kept in sync with workbench/src/fixtures/products.json).
_FALLBACK_PRODUCTS = [
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
        "material": "",
        "shipping": "Freeship toàn quốc cho đơn từ 250k",
        "warranty": "Hàng chính hãng 100%, đổi trả 7 ngày",
        "in_stock": True,
        "stock_total": 200,
        "ref_image": "",
        "features": ["SPF50+", "chống nước", "phù hợp da nhạy cảm"],
    },
    {
        "id": "P002",
        "name": "Serum Vitamin C 20% làm sáng da",
        "description": "Serum Vitamin C 20% + E + Ferulic, làm sáng da, mờ thâm",
        "price": 189000,
        "original_price": 280000,
        "promotion": "Giảm 33% — chỉ 189k (giá gốc 280k)",
        "colors": [],
        "sizes": [],
        "material": "",
        "shipping": "Freeship toàn quốc cho đơn từ 250k",
        "warranty": "Hàng chính hãng, đổi trả 7 ngày",
        "in_stock": True,
        "stock_total": 150,
        "ref_image": "",
        "features": ["Vitamin C 20%", "làm sáng", "mờ thâm"],
    },
    {
        "id": "P003",
        "name": "Áo thun cotton form rộng unisex",
        "description": "Áo thun cotton 100% form rộng unisex, thoáng mát",
        "price": 149000,
        "original_price": 220000,
        "promotion": "Giảm 32% — chỉ 149k (giá gốc 220k)",
        "colors": ["đen", "trắng"],
        "sizes": ["S", "M", "L", "XL"],
        "material": "cotton 100%",
        "shipping": "Freeship toàn quốc cho đơn từ 250k",
        "warranty": "Đổi trả miễn phí trong 7 ngày",
        "in_stock": True,
        "stock_total": 300,
        "ref_image": "image_30822a.png",
        "features": ["cotton 100%", "form rộng", "unisex"],
    },
]


def _load_products() -> list[dict]:
    path = _WORKBENCH_FIXTURES / "products.json"
    if not path.is_file():
        return [dict(p) for p in _FALLBACK_PRODUCTS]
    products = json.loads(path.read_text(encoding="utf-8"))
    return [
        {k: (v if v is not None else "") for k, v in p.items()}
        for p in products
    ]


def _load_viewer_msgs() -> list[str]:
    path = _WORKBENCH_FIXTURES / "viewer_messages.json"
    if path.is_file():
        msgs = [entry["text"] for entry in json.loads(path.read_text(encoding="utf-8"))]
    else:
        msgs = []
    return msgs + [t for t in _BENCHMARK_ONLY_MSGS if t not in msgs]


MOCK_PRODUCTS = _load_products()
MOCK_VIEWER_MSGS = _load_viewer_msgs()
