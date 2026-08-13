"""Shared offline fixtures for backend unit tests (OpenSpec 1.50).

Mock product catalog for Director/run-plan tests. The legacy
``core/debug/mock_data.py`` moved to the workbench fixture files (1.45/1.48);
service tests keep a minimal service-local copy so they stay self-contained.

Task 8.7: ``MOCK_ENTITIES`` is the entity-document mirror of ``MOCK_PRODUCTS``
(the same 3 products as ``EntityDocument``). ``MOCK_PRODUCTS`` stays only
until task 8.12 removes the legacy shape.
"""

from __future__ import annotations

from backend.application.entity.models import EntityDocument, Fact, KnowledgeBlock

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


def _entity_from_product(product: dict) -> EntityDocument:
    """One EntityDocument mirroring a legacy MOCK_PRODUCTS entry (task 8.7)."""
    facts = [
        Fact(key="commerce.price.current", type="int", value=product["price"], unit="VND"),
        Fact(
            key="commerce.price.original",
            type="int",
            value=product["original_price"],
            unit="VND",
        ),
        Fact(key="commerce.promotion", type="str", value=product["promotion"]),
        Fact(key="commerce.shipping", type="str", value=product["shipping"]),
        Fact(key="commerce.warranty", type="str", value=product["warranty"]),
        Fact(key="commerce.stock.available", type="bool", value=product["in_stock"]),
        Fact(key="commerce.stock.quantity", type="int", value=product["stock_total"]),
    ]
    blocks = [
        KnowledgeBlock(
            id=f"desc:{product['id']}",
            kind="description",
            title="Mô tả",
            content=product["description"],
        ),
        KnowledgeBlock(
            id=f"features:{product['id']}",
            kind="custom",
            title="features",
            content=", ".join(product["features"]),
            tags=["features"],
        ),
    ]
    if product.get("material"):
        facts.append(Fact(key="custom.material", type="str", value=product["material"]))
    if product.get("ref_image"):
        facts.append(Fact(key="custom.ref_image", type="str", value=product["ref_image"]))
    if product.get("colors"):
        blocks.append(
            KnowledgeBlock(
                id=f"color:{product['id']}",
                kind="custom",
                title="color",
                content=", ".join(product["colors"]),
                tags=["color"],
            )
        )
    if product.get("sizes"):
        blocks.append(
            KnowledgeBlock(
                id=f"size:{product['id']}",
                kind="custom",
                title="size",
                content=", ".join(product["sizes"]),
                tags=["size"],
            )
        )
    return EntityDocument(
        id=product["id"],
        entity_type="product",
        name=product["name"],
        facts=facts,
        knowledge_blocks=blocks,
    )


MOCK_ENTITIES: list[EntityDocument] = [_entity_from_product(product) for product in MOCK_PRODUCTS]
