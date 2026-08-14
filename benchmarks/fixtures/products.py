"""Fixed product catalog for benchmark fixtures (task 1.77).

Two VN live-commerce products with rich structured attributes so the
O(1) factual-answer path (price/shipping/size/color/stock) and the semantic
retrieval path both have deterministic inputs. Products are canonical
``EntityDocument`` values (the rigid ``Product``/``ProductVariant`` catalog
was removed with the universal entity migration, 8.12); the builder mirrors
``ProductEntityIn.to_entity`` so the corpus matches the API wire shape.
Embeddings are filled at fixture-generation time from the hashing embedder.
"""

from __future__ import annotations

from backend.application.entity.models import EntityDocument, Fact, KnowledgeBlock
from backend.application.entity.registry import (
    COMMERCE_PRICE_CURRENT,
    COMMERCE_PRICE_ORIGINAL,
    COMMERCE_PROMOTION,
    COMMERCE_SHIPPING,
    COMMERCE_STOCK_AVAILABLE,
    COMMERCE_STOCK_QUANTITY,
    COMMERCE_WARRANTY,
    IDENTITY_BRAND,
    IDENTITY_SKU,
)


def _fact(key: str, type_: str, value) -> Fact:
    return Fact(key=key, type=type_, value=value)


def _block(
    block_id: str, kind: str, title: str, content: str, tags: list[str] | None = None
) -> KnowledgeBlock:
    return KnowledgeBlock(id=block_id, kind=kind, title=title, content=content, tags=tags or [])


def _make_products() -> list[EntityDocument]:
    return [
        EntityDocument(
            id="P004",
            entity_type="product",
            name="Áo hoodie HeyGen màu trắng",
            tags=["thời trang"],
            facts=[
                _fact(IDENTITY_BRAND, "str", "HeyGen"),
                _fact(IDENTITY_SKU, "str", "P004"),
                _fact(COMMERCE_PRICE_CURRENT, "int", 299000),
                _fact(COMMERCE_PRICE_ORIGINAL, "int", 399000),
                _fact(COMMERCE_PROMOTION, "str", "Giảm 25% — chỉ 299k (giá gốc 399k)"),
                _fact(COMMERCE_STOCK_AVAILABLE, "bool", True),
                _fact(COMMERCE_STOCK_QUANTITY, "int", 20),
                _fact(COMMERCE_SHIPPING, "str", "Freeship đơn từ 200k, 2-4 ngày"),
                _fact(COMMERCE_WARRANTY, "str", "Đổi trả trong 7 ngày"),
                _fact("custom.material", "str", "cotton 100%"),
                _fact("custom.origin", "str", "Việt Nam"),
                _fact("custom.usage", "str", "Mặc thường ngày, giặt máy ở chế độ nhẹ"),
                _fact("custom.how_to_buy", "str", "Chốt đơn qua comment hoặc đặt hàng trên shop"),
                _fact("custom.ref_image", "str", "asset://products/P004.png"),
            ],
            knowledge_blocks=[
                _block(
                    "desc:P004",
                    "description",
                    "Mô tả",
                    "Áo hoodie unisex chất cotton dày dặn, form rộng",
                ),
                _block(
                    "custom:features:P004",
                    "custom",
                    "features",
                    "hoodie, form rộng, cotton dày",
                    tags=["features"],
                ),
                _block(
                    "color:P004",
                    "custom",
                    "color",
                    "trắng, đen, xám",
                    tags=["color"],
                ),
                _block(
                    "size:P004",
                    "custom",
                    "size",
                    "M, L, XL",
                    tags=["size"],
                ),
            ],
        ),
        EntityDocument(
            id="P002",
            entity_type="product",
            name="Serum Vitamin C 20%",
            tags=["mỹ phẩm"],
            facts=[
                _fact(IDENTITY_BRAND, "str", "Livento Skin"),
                _fact(IDENTITY_SKU, "str", "P002"),
                _fact(COMMERCE_PRICE_CURRENT, "int", 250000),
                _fact(COMMERCE_PRICE_ORIGINAL, "int", 320000),
                _fact(COMMERCE_PROMOTION, "str", "Giảm 22% — chỉ 250k (giá gốc 320k)"),
                _fact(COMMERCE_STOCK_AVAILABLE, "bool", True),
                _fact(COMMERCE_STOCK_QUANTITY, "int", 30),
                _fact(COMMERCE_SHIPPING, "str", "Freeship đơn từ 200k, 2-4 ngày"),
                _fact(COMMERCE_WARRANTY, "str", "Đổi trả trong 7 ngày"),
                _fact("custom.material", "str", "Vitamin C 20%, Hyaluronic"),
                _fact("custom.origin", "str", "Hàn Quốc"),
                _fact("custom.usage", "str", "Dùng sáng và tối, 3-4 giọt sau toner"),
                _fact("custom.how_to_buy", "str", "Chốt đơn qua comment hoặc đặt hàng trên shop"),
                _fact("custom.ref_image", "str", "asset://products/P002.png"),
            ],
            knowledge_blocks=[
                _block(
                    "desc:P002",
                    "description",
                    "Mô tả",
                    "Serum vitamin C 20% sáng da, mờ thâm, chống oxy hóa",
                ),
                _block(
                    "custom:features:P002",
                    "custom",
                    "features",
                    "vitamin C, sáng da, chống oxy hóa",
                    tags=["features"],
                ),
                _block(
                    "color:P002",
                    "custom",
                    "color",
                    "trong suốt",
                    tags=["color"],
                ),
                _block(
                    "size:P002",
                    "custom",
                    "size",
                    "30ml",
                    tags=["size"],
                ),
            ],
        ),
    ]


CORPUS_PRODUCTS: list[EntityDocument] = _make_products()
