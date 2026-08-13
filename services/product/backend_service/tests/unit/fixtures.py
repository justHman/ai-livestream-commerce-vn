"""Shared offline fixtures for backend unit tests (OpenSpec 1.50).

Mock product catalog for Director/run-plan tests. The legacy
``core/debug/mock_data.py`` moved to the workbench fixture files (1.45/1.48);
service tests keep a minimal service-local copy so they stay self-contained.

Task 8.7: ``MOCK_ENTITIES`` is the entity-document mirror of the legacy
product list (the same 3 products as ``EntityDocument``). Task 8.12 removed
the legacy shape; ``MOCK_ENTITIES`` remains the canonical fixture.
"""

from __future__ import annotations

from backend.application.entity.models import EntityDocument, Fact, KnowledgeBlock

MOCK_ENTITIES: list[EntityDocument] = [
    EntityDocument(
        id="P004",
        entity_type="product",
        name="Áo hoodie HeyGen màu trắng",
        facts=[
            Fact(key="commerce.price.current", type="int", value=350000, unit="VND"),
            Fact(
                key="commerce.price.original",
                type="int",
                value=500000,
                unit="VND",
            ),
            Fact(key="commerce.promotion", type="str", value="Giảm 30% — chỉ 350k (giá gốc 500k)"),
            Fact(key="commerce.shipping", type="str", value="Freeship toàn quốc cho đơn từ 250k"),
            Fact(
                key="commerce.warranty",
                type="str",
                value="Đổi trả miễn phí trong 7 ngày nếu lỗi từ nhà sản xuất",
            ),
            Fact(key="commerce.stock.available", type="bool", value=True),
            Fact(key="commerce.stock.quantity", type="int", value=120),
            Fact(key="custom.material", type="str", value="nỉ cotton"),
            Fact(key="custom.ref_image", type="str", value="image_20509e.png"),
        ],
        knowledge_blocks=[
            KnowledgeBlock(
                id="desc:P004",
                kind="description",
                title="Mô tả",
                content="Áo hoodie trơn màu trắng kem, có mũ trùm, in logo HeyGen tinh tế ở ngực trái.",
            ),
            KnowledgeBlock(
                id="features:P004",
                kind="custom",
                title="features",
                content=", ".join(
                    [
                        "áo hoodie có mũ",
                        "logo HeyGen",
                        "màu trắng kem",
                        "dài tay",
                        "phong cách tối giản",
                    ]
                ),
                tags=["features"],
            ),
            KnowledgeBlock(
                id="color:P004",
                kind="custom",
                title="color",
                content="trắng kem",
                tags=["color"],
            ),
            KnowledgeBlock(
                id="size:P004",
                kind="custom",
                title="size",
                content="S, M, L, XL",
                tags=["size"],
            ),
        ],
    ),
    EntityDocument(
        id="P001",
        entity_type="product",
        name="Kem chống nắng La Roche-Posay SPF50+",
        facts=[
            Fact(key="commerce.price.current", type="int", value=329000, unit="VND"),
            Fact(
                key="commerce.price.original",
                type="int",
                value=490000,
                unit="VND",
            ),
            Fact(key="commerce.promotion", type="str", value="Giảm 33% — chỉ 329k (giá gốc 490k)"),
            Fact(key="commerce.shipping", type="str", value="Freeship đơn từ 200k, giao 2-4 ngày"),
            Fact(
                key="commerce.warranty", type="str", value="Đổi trả trong 7 ngày nếu chưa sử dụng"
            ),
            Fact(key="commerce.stock.available", type="bool", value=True),
            Fact(key="commerce.stock.quantity", type="int", value=150),
        ],
        knowledge_blocks=[
            KnowledgeBlock(
                id="desc:P001",
                kind="description",
                title="Mô tả",
                content="Kem chống nắng phổ rộng SPF50+, chống nước, phù hợp da nhạy cảm",
            ),
            KnowledgeBlock(
                id="features:P001",
                kind="custom",
                title="features",
                content=", ".join(
                    ["SPF50+", "chống nước", "phổ rộng", "da nhạy cảm", "không bết dính"]
                ),
                tags=["features"],
            ),
        ],
    ),
    EntityDocument(
        id="P002",
        entity_type="product",
        name="Serum Vitamin C 20% làm sáng da",
        facts=[
            Fact(key="commerce.price.current", type="int", value=189000, unit="VND"),
            Fact(
                key="commerce.price.original",
                type="int",
                value=299000,
                unit="VND",
            ),
            Fact(
                key="commerce.promotion",
                type="str",
                value="Flash sale: 189k (giá gốc 299k) — chỉ còn 50 chai!",
            ),
            Fact(key="commerce.shipping", type="str", value="Freeship đơn từ 200k"),
            Fact(key="commerce.warranty", type="str", value="Đổi trả 7 ngày"),
            Fact(key="commerce.stock.available", type="bool", value=True),
            Fact(key="commerce.stock.quantity", type="int", value=50),
        ],
        knowledge_blocks=[
            KnowledgeBlock(
                id="desc:P002",
                kind="description",
                title="Mô tả",
                content="Serum Vitamin C 20% + HA, mờ thâm nám, sáng da, chai 30ml",
            ),
            KnowledgeBlock(
                id="features:P002",
                kind="custom",
                title="features",
                content=", ".join(["vitamin C 20%", "mờ thâm", "sáng da", "HA dưỡng ẩm"]),
                tags=["features"],
            ),
        ],
    ),
]
