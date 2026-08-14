"""Cross-domain entity fixtures (task 8.13): EntityDocument data for six verticals.

Used by entity tests and later by the Workbench Entity Data Studio. Content is
real-world Vietnamese commerce: names, VND prices, aliases, promotion/shipping/
warranty prose. Facts use canonical Common Fact Registry keys where they exist
(Decision 11); the real-estate vertical exercises the ``custom.*`` namespace.
"""

from __future__ import annotations

from backend.application.entity.models import (
    EntityDocument,
    Fact,
    KnowledgeBlock,
    Relation,
)

__all__ = [
    "CROSS_DOMAIN_ENTITIES",
    "COSMETICS",
    "CUSTOM_REAL_ESTATE",
    "ELECTRONICS",
    "FASHION",
    "FOOD",
    "HOUSEHOLD",
    "VERTICAL_BY_ID",
]

FASHION = EntityDocument(
    id="product:fashion-hoodie-heygen",
    entity_type="product",
    revision=3,
    name="Áo hoodie HeyGen màu trắng",
    aliases=["Hoodie trắng kem", "Áo hoodie trơn có mũ", "Hoodie HeyGen"],
    tags=["fashion", "thời trang", "hoodie", "unisex"],
    facts=[
        Fact(
            key="commerce.price.current",
            type="int",
            value=350000,
            unit="VND",
            labels=["Giá hiện tại"],
            revision=3,
            freshness="volatile",
        ),
        Fact(
            key="commerce.price.original",
            type="int",
            value=500000,
            unit="VND",
            labels=["Giá gốc"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.available",
            type="bool",
            value=True,
            labels=["Còn hàng"],
            revision=3,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.quantity",
            type="int",
            value=120,
            unit="items",
            labels=["Tồn kho"],
            revision=3,
            freshness="volatile",
        ),
        Fact(
            key="commerce.promotion",
            type="str",
            value="Giảm 30% — chỉ 350k (giá gốc 500k)",
            labels=["Khuyến mãi"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.shipping",
            type="str",
            value="Freeship toàn quốc cho đơn từ 250k",
            labels=["Vận chuyển"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="commerce.warranty",
            type="str",
            value="Đổi trả miễn phí trong 7 ngày nếu lỗi từ nhà sản xuất",
            labels=["Bảo hành"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.brand",
            type="str",
            value="HeyGen",
            labels=["Thương hiệu"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.sku",
            type="str",
            value="HG-HOODIE-WHITE-001",
            labels=["Mã SKU"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="custom.fashion.material",
            type="str",
            value="Nỉ cotton",
            labels=["Chất liệu"],
            revision=1,
            freshness="stable",
        ),
    ],
    knowledge_blocks=[
        KnowledgeBlock(
            id="kb:fashion-hoodie-heygen-description",
            kind="description",
            title="Mô tả sản phẩm",
            content=(
                "Áo hoodie trơn màu trắng kem, có mũ trùm, in logo HeyGen tinh tế ở ngực trái.\n"
                "Chất nỉ cotton dày dặn, mặc ấm mà không bí, form unisex rộng rãi phù hợp cả nam và nữ.\n"
                "Dài tay, phong cách tối giản, dễ phối với quần jeans hoặc chân váy."
            ),
            tags=["mô tả", "chất liệu", "thiết kế"],
            revision=2,
        ),
    ],
    relations=[
        Relation(target_entity_id="shop:heygen-store-vn", relation_type="belongs_to_shop"),
    ],
)

COSMETICS = EntityDocument(
    id="product:cosmetics-kcn-la-roche-posay",
    entity_type="product",
    revision=2,
    name="Kem chống nắng La Roche-Posay SPF50+",
    aliases=["Kem chống nắng LRP", "Anthelios SPF50", "KCN LRP"],
    tags=["cosmetics", "mỹ phẩm", "kem chống nắng", "chăm sóc da"],
    facts=[
        Fact(
            key="commerce.price.current",
            type="int",
            value=329000,
            unit="VND",
            labels=["Giá hiện tại"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.price.original",
            type="int",
            value=490000,
            unit="VND",
            labels=["Giá gốc"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.available",
            type="bool",
            value=True,
            labels=["Còn hàng"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.quantity",
            type="int",
            value=150,
            unit="items",
            labels=["Tồn kho"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.promotion",
            type="str",
            value="Giảm 33% — chỉ 329k (giá gốc 490k)",
            labels=["Khuyến mãi"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.shipping",
            type="str",
            value="Freeship đơn từ 200k, giao 2-4 ngày",
            labels=["Vận chuyển"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="commerce.warranty",
            type="str",
            value="Đổi trả trong 7 ngày nếu chưa sử dụng",
            labels=["Bảo hành"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.brand",
            type="str",
            value="La Roche-Posay",
            labels=["Thương hiệu"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.sku",
            type="str",
            value="LRP-ANTH-50-001",
            labels=["Mã SKU"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="custom.cosmetics.spf",
            type="str",
            value="SPF50+",
            labels=["Chỉ số chống nắng"],
            revision=1,
            freshness="stable",
        ),
    ],
    knowledge_blocks=[
        KnowledgeBlock(
            id="kb:cosmetics-kcn-la-roche-posay-usage",
            kind="usage",
            title="Hướng dẫn sử dụng",
            content=(
                "Bước 1: Làm sạch da mặt và lau khô trước khi sử dụng.\n"
                "Bước 2: Lấy một lượng kem bằng đồng xu, chấm đều lên 5 điểm trên mặt: trán, hai má, mũi và cằm.\n"
                "Bước 3: Dùng đầu ngón tay tán đều kem theo hướng từ trong ra ngoài, đặc biệt chú ý vùng chữ T và cổ.\n"
                "Bước 4: Đợi 2-3 phút cho kem thấm hoàn toàn trước khi trang điểm hoặc ra nắng.\n\n"
                "Nên thoa lại sau mỗi 2 giờ nếu hoạt động ngoài trời liên tục hoặc sau khi bơi, đổ mồ hôi nhiều.\n"
                "Sản phẩm phổ rộng SPF50+ chống nước, phù hợp da nhạy cảm, không bết dính. Tránh vùng mắt; "
                "nếu kem dính vào mắt hãy rửa sạch ngay bằng nước."
            ),
            tags=["hướng dẫn", "cách dùng", "chống nắng"],
            revision=1,
        ),
    ],
    relations=[
        Relation(target_entity_id="shop:heygen-store-vn", relation_type="belongs_to_shop"),
    ],
)

FOOD = EntityDocument(
    id="product:food-ca-phe-robusta-daklak",
    entity_type="product",
    revision=1,
    name="Cà phê rang xay nguyên chất Đắk Lắk 500g",
    aliases=["Cà phê Đắk Lắk", "Cafe rang xay", "Cà phê robusta"],
    tags=["food", "thực phẩm", "cà phê", "đặc sản"],
    facts=[
        Fact(
            key="commerce.price.current",
            type="int",
            value=180000,
            unit="VND",
            labels=["Giá hiện tại"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.price.original",
            type="int",
            value=220000,
            unit="VND",
            labels=["Giá gốc"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.available",
            type="bool",
            value=True,
            labels=["Còn hàng"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.quantity",
            type="int",
            value=80,
            unit="items",
            labels=["Tồn kho"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.promotion",
            type="str",
            value="Mua 2 túi giảm thêm 20k — freeship đơn từ 300k",
            labels=["Khuyến mãi"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.shipping",
            type="str",
            value="Giao hàng toàn quốc 1-3 ngày, đóng gói hút chân không",
            labels=["Vận chuyển"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="commerce.warranty",
            type="str",
            value="Đổi trả nếu sản phẩm lỗi, hết hạn hoặc không đúng mô tả",
            labels=["Bảo hành"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.brand",
            type="str",
            value="Bản Đôn",
            labels=["Thương hiệu"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.sku",
            type="str",
            value="BD-RX-500-001",
            labels=["Mã SKU"],
            revision=1,
            freshness="stable",
        ),
    ],
    knowledge_blocks=[
        KnowledgeBlock(
            id="kb:food-ca-phe-robusta-daklak-description",
            kind="description",
            title="Mô tả sản phẩm",
            content=(
                "Cà phê robusta nguyên chất được trồng và rang xay tại Đắk Lắk, đóng gói 500g hút chân không giữ trọn hương vị.\n"
                "Vị đậm đắng truyền thống, hậu vị ngọt nhẹ đặc trưng của cà phê vùng cao nguyên.\n"
                "Pha được cả phin, máy espresso hoặc pha phê đá kiểu Việt Nam."
            ),
            tags=["mô tả", "nguồn gốc", "hương vị"],
            revision=1,
        ),
    ],
    relations=[
        Relation(target_entity_id="shop:heygen-store-vn", relation_type="belongs_to_shop"),
    ],
)

ELECTRONICS = EntityDocument(
    id="product:electronics-tai-nghe-sony-wh1000",
    entity_type="product",
    revision=2,
    name="Tai nghe chống ồn Sony WH-1000XM5",
    aliases=["Tai nghe Sony XM5", "Sony WH1000XM5", "Tai nghe chống ồn"],
    tags=["electronics", "điện tử", "tai nghe", "chống ồn"],
    facts=[
        Fact(
            key="commerce.price.current",
            type="int",
            value=8990000,
            unit="VND",
            labels=["Giá hiện tại"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.price.original",
            type="int",
            value=10500000,
            unit="VND",
            labels=["Giá gốc"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.available",
            type="bool",
            value=True,
            labels=["Còn hàng"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.quantity",
            type="int",
            value=35,
            unit="items",
            labels=["Tồn kho"],
            revision=2,
            freshness="volatile",
        ),
        Fact(
            key="commerce.promotion",
            type="str",
            value="Giảm 1.610.000đ — tặng kèm bao da chính hãng",
            labels=["Khuyến mãi"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.shipping",
            type="str",
            value="Freeship toàn quốc, giao nhanh nội thành Hà Nội 2-4 giờ",
            labels=["Vận chuyển"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="commerce.warranty",
            type="str",
            value="Bảo hành chính hãng 12 tháng tại trung tâm Sony Việt Nam",
            labels=["Bảo hành"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.brand",
            type="str",
            value="Sony",
            labels=["Thương hiệu"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.sku",
            type="str",
            value="SONY-WH1000XM5-BK",
            labels=["Mã SKU"],
            revision=1,
            freshness="stable",
        ),
    ],
    knowledge_blocks=[
        KnowledgeBlock(
            id="kb:electronics-tai-nghe-sony-wh1000-description",
            kind="description",
            title="Mô tả sản phẩm",
            content=(
                "Tai nghe chụp tai chống ồn chủ động (ANC) thế hệ mới nhất của Sony, công nghệ AI Noise Cancelling.\n"
                "Driver 30mm màng carbon, hỗ trợ Hi-Res Audio, pin lên tới 30 giờ và sạc nhanh USB-C.\n"
                "Kết nối Bluetooth 5.2 đa điểm, điều khiển cảm ứng bên ngoài tai nghe."
            ),
            tags=["mô tả", "thông số", "pin"],
            revision=1,
        ),
    ],
    relations=[
        Relation(target_entity_id="shop:heygen-store-vn", relation_type="belongs_to_shop"),
    ],
)

HOUSEHOLD = EntityDocument(
    id="product:household-may-loc-nuoc-karofi",
    entity_type="product",
    revision=1,
    name="Máy lọc nước Karofi 5 lõi KA-05",
    aliases=["Máy lọc nước Karofi", "Máy lọc nước 5 lõi", "Karofi KA-05"],
    tags=["household", "gia dụng", "máy lọc nước"],
    facts=[
        Fact(
            key="commerce.price.current",
            type="int",
            value=3490000,
            unit="VND",
            labels=["Giá hiện tại"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.price.original",
            type="int",
            value=4500000,
            unit="VND",
            labels=["Giá gốc"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.available",
            type="bool",
            value=True,
            labels=["Còn hàng"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.quantity",
            type="int",
            value=25,
            unit="items",
            labels=["Tồn kho"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.promotion",
            type="str",
            value="Giảm 1 triệu đồng, tặng bộ lõi lọc dự phòng trị giá 600k",
            labels=["Khuyến mãi"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.shipping",
            type="str",
            value="Miễn phí vận chuyển và lắp đặt tận nơi tại Hà Nội và TP.HCM",
            labels=["Vận chuyển"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="commerce.warranty",
            type="str",
            value="Bảo hành máy 24 tháng, 5 năm đối với thân bình",
            labels=["Bảo hành"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.brand",
            type="str",
            value="Karofi",
            labels=["Thương hiệu"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="identity.sku",
            type="str",
            value="KAROFI-KA05-01",
            labels=["Mã SKU"],
            revision=1,
            freshness="stable",
        ),
    ],
    knowledge_blocks=[
        KnowledgeBlock(
            id="kb:household-may-loc-nuoc-karofi-description",
            kind="description",
            title="Mô tả sản phẩm",
            content=(
                "Máy lọc nước RO 5 lõi lọc chuẩn Bộ Y Tế, công suất 10 lít/giờ, phù hợp gia đình 2-6 người.\n"
                "Lõi lọc gồm: bông PP, than hoạt tính dạng hạt, than nén, màng RO 75G và lõi khoáng.\n"
                "Tự ngắt khi đầy bình chứa, vòi chống nhiễm khuẩn, lõi lọc dễ thay tại nhà."
            ),
            tags=["mô tả", "lõi lọc", "công suất"],
            revision=1,
        ),
    ],
    relations=[
        Relation(target_entity_id="shop:heygen-store-vn", relation_type="belongs_to_shop"),
    ],
)

CUSTOM_REAL_ESTATE = EntityDocument(
    id="product:realestate-can-ho-vinhomes-ocean-park",
    entity_type="product",
    revision=1,
    name="Căn hộ 2PN Vinhomes Ocean Park Gia Lâm 63m²",
    aliases=["Căn hộ Vinhomes Ocean Park", "Chung cư Gia Lâm", "CH 2PN Ocean Park"],
    tags=["real-estate", "bất động sản", "căn hộ", "2PN"],
    facts=[
        Fact(
            key="commerce.price.current",
            type="int",
            value=2900000000,
            unit="VND",
            labels=["Giá hiện tại"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.available",
            type="bool",
            value=True,
            labels=["Còn hàng"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.stock.quantity",
            type="int",
            value=3,
            unit="items",
            labels=["Tồn kho"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="commerce.promotion",
            type="str",
            value="Chiết khấu 3% cho khách thanh toán sớm, hỗ trợ vay 70% giá trị",
            labels=["Khuyến mãi"],
            revision=1,
            freshness="volatile",
        ),
        Fact(
            key="custom.realestate.bedrooms",
            type="int",
            value=2,
            unit="phòng",
            labels=["Số phòng ngủ"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="custom.realestate.bathrooms",
            type="int",
            value=2,
            unit="phòng",
            labels=["Số phòng vệ sinh"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="custom.realestate.area",
            type="float",
            value=63.0,
            unit="m²",
            labels=["Diện tích"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="custom.realestate.floor",
            type="int",
            value=15,
            unit="tầng",
            labels=["Tầng"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="custom.realestate.furnished",
            type="bool",
            value=False,
            labels=["Nội thất"],
            revision=1,
            freshness="stable",
        ),
        Fact(
            key="custom.realestate.legal",
            type="str",
            value="Sổ hồng vĩnh viễn, sang tên chính chủ",
            labels=["Pháp lý"],
            revision=1,
            freshness="stable",
        ),
    ],
    knowledge_blocks=[
        KnowledgeBlock(
            id="kb:realestate-can-ho-vinhomes-ocean-park-description",
            kind="description",
            title="Mô tả căn hộ",
            content=(
                "Căn hộ 2 phòng ngủ, 2 vệ sinh tại đại đô thị Vinhomes Ocean Park, Gia Lâm, Hà Nội.\n"
                "Diện tích 63m², view công viên, ban công hướng Đông Nam thoáng mát.\n"
                "Bàn giao hoàn thiện cơ bản, tiện ích nội khu: hồ bơi, phòng gym, trường học liên cấp và Vincom thương mại.\n"
                "Giao thông thuận tiện cầu Vĩnh Tuy 2, chỉ 15 phút tới trung tâm Hà Nội."
            ),
            tags=["mô tả", "vị trí", "tiện ích"],
            revision=1,
        ),
    ],
    relations=[
        Relation(
            target_entity_id="shop:vinhomes-ocp-sales",
            relation_type="belongs_to_shop",
            metadata={"seller": "chủ đầu tư"},
        ),
    ],
)

CROSS_DOMAIN_ENTITIES: list[EntityDocument] = [
    FASHION,
    COSMETICS,
    FOOD,
    ELECTRONICS,
    HOUSEHOLD,
    CUSTOM_REAL_ESTATE,
]

VERTICAL_BY_ID: dict[str, EntityDocument] = {entity.id: entity for entity in CROSS_DOMAIN_ENTITIES}
