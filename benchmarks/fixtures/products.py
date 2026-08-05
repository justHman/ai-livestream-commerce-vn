"""Fixed product catalog for benchmark fixtures (task 1.77).

Two VN live-commerce products with rich structured attributes so the
O(1) factual-answer path (price/shipping/size/color/stock) and the semantic
retrieval path both have deterministic inputs. Embeddings are filled at
fixture-generation time from the hashing embedder.
"""

from __future__ import annotations

from backend.application.director.catalog import Product, ProductVariant


def _make_products() -> list[Product]:
    return [
        Product(
            id="P004",
            name="Áo hoodie HeyGen màu trắng",
            description="Áo hoodie unisex chất cotton dày dặn, form rộng",
            price=299000,
            original_price=399000,
            brand="HeyGen",
            category="thời trang",
            colors=["trắng", "đen", "xám"],
            sizes=["M", "L", "XL"],
            material="cotton 100%",
            origin="Việt Nam",
            features=["hoodie", "form rộng", "cotton dày"],
            variants=[
                ProductVariant(sku="P004-M", color="trắng", size="M", price=299000, stock=12),
                ProductVariant(sku="P004-XL", color="trắng", size="XL", price=299000, stock=8),
            ],
            in_stock=True,
            stock_total=20,
            shipping="Freeship đơn từ 200k, 2-4 ngày",
            warranty="Đổi trả trong 7 ngày",
            how_to_buy="Chốt đơn qua comment hoặc đặt hàng trên shop",
            usage="Mặc thường ngày, giặt máy ở chế độ nhẹ",
            ref_image="asset://products/P004.png",
        ),
        Product(
            id="P002",
            name="Serum Vitamin C 20%",
            description="Serum vitamin C 20% sáng da, mờ thâm, chống oxy hóa",
            price=250000,
            original_price=320000,
            brand="Livento Skin",
            category="mỹ phẩm",
            colors=["trong suốt"],
            sizes=["30ml"],
            material="Vitamin C 20%, Hyaluronic",
            origin="Hàn Quốc",
            features=["vitamin C", "sáng da", "chống oxy hóa"],
            variants=[ProductVariant(sku="P002-30", size="30ml", price=250000, stock=30)],
            in_stock=True,
            stock_total=30,
            shipping="Freeship đơn từ 200k, 2-4 ngày",
            warranty="Đổi trả trong 7 ngày",
            how_to_buy="Chốt đơn qua comment hoặc đặt hàng trên shop",
            usage="Dùng sáng và tối, 3-4 giọt sau toner",
            ref_image="asset://products/P002.png",
        ),
    ]


CORPUS_PRODUCTS: list[Product] = _make_products()
