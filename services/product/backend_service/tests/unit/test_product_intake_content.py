"""Content quality of the intake result, built from the real Fresho M2 sample the owner pasted (2026-11-10)."""

import json

from backend.application.product_intake import extract_facts
from backend.application.product_intake.extractor import _flags

NAME = "Tên sản phẩm: Máy xay sinh tố cầm tay mini sạc USB Fresho M2"
CATEGORY = "Ngành hàng: Đồ gia dụng nhà bếp"
PRICE = "Giá: 289.000 đồng (giá niêm yết 399.000 đồng)"
AUDIENCE = "Đối tượng: Dân văn phòng, sinh viên, người tập gym"
EXCHANGE = "Lỗi do nhà sản xuất đổi mới trong 7 ngày"
RETURN = "Đổi trả trong 30 ngày"
WARRANTY = "Bảo hành 6 tháng"
SOURCE = "\n".join([NAME, CATEGORY, PRICE, AUDIENCE, EXCHANGE, RETURN, WARRANTY])


def run(*lines, ftype="feature"):
    payload = json.dumps({"facts": [{"type": ftype, "text": x, "evidence": x} for x in lines]})
    return extract_facts(SOURCE, lambda _p: payload)


def test_name_and_category_label_lines_are_dropped_as_form_duplicates():
    r = run(NAME, CATEGORY)
    assert [d.reason for d in r.dropped] == ["form_field_duplicate"] * 2


def test_audience_line_becomes_a_benefit_without_the_label():
    r = run(AUDIENCE)
    assert [(f.type, f.text) for f in r.facts] == [
        ("benefit", "Dân văn phòng, sinh viên, người tập gym")
    ]


def test_price_line_is_kept():
    assert [f.text for f in run(PRICE, ftype="promotion").facts] == [PRICE]


def test_return_and_exchange_periods_are_not_time_limited_offers():
    assert "time_limited" not in _flags(EXCHANGE)
    assert "time_limited" not in _flags(RETURN)


def test_return_and_warranty_keep_the_guarantee_flag():
    assert "guarantee" in _flags(RETURN)
    assert "guarantee" in _flags(WARRANTY)


def test_real_promotions_are_still_time_limited():
    for text in (
        "Tặng kèm 1 cáp sạc khi mua trong tuần này",
        "Giảm 20% hôm nay",
        "Giảm giá trong 3 giờ",
        "Ưu đãi trong 3 ngày tới",
        "Flash sale số lượng có hạn",
        "Ưu đãi hợp lệ đến hết 30/11",
    ):
        assert "time_limited" in _flags(text), text


def test_seven_day_exchange_vs_thirty_day_return_warns():
    (w,) = run(EXCHANGE, RETURN, ftype="warranty").warnings
    assert (w["code"], w["facts"]) == ("policy_period_conflict", [0, 1])


def test_warranty_months_vs_return_days_do_not_warn():
    assert run(WARRANTY, RETURN, ftype="warranty").warnings == ()


def test_same_return_period_twice_does_not_warn():
    twin = "Hoàn tiền nếu đổi trả trong 30 ngày"
    r = extract_facts(
        SOURCE + "\n" + twin,
        lambda _p: json.dumps(
            {"facts": [{"type": "warranty", "text": t, "evidence": t} for t in (RETURN, twin)]}
        ),
    )
    assert r.warnings == ()


# --- 2026-11-11: the model rewrote the form fields as "X là Y", which the "X:" filter missed ---
ANH_20 = [
    ("feature", "Tên sản phẩm là Máy xay sinh tố cầm tay mini sạc USB Fresho M2"),
    ("feature", "Ngành hàng là Đồ gia dụng nhà bếp"),
    ("promotion", "Giá 289.000 đồng, giá niêm yết 399.000 đồng, đang giảm khoảng 28%"),
    (
        "benefit",
        "Dành cho dân văn phòng, sinh viên, người tập gym muốn làm nước ép, sinh tố tại chỗ",
    ),
    ("feature", "Dung tích 380 ml"),
    ("feature", "Thân nhựa Tritan không chứa BPA"),
    ("feature", "6 lưỡi dao thép không gỉ"),
    ("feature", "Một lần sạc đầy xay được khoảng 12 mẻ"),
    ("usage", "Tự làm sạch bằng cách xay nước xà phòng loãng 15 giây"),
    ("feature", "Nắp kín chống tràn"),
    ("feature", "Bỏ vừa túi xách"),
    ("feature", "Nhỏ gọn nặng 420 g"),
    ("usage", "Xay trái cây mềm, đá viên nhỏ và rau lá trong khoảng 30 giây"),
    ("feature", "Có 3 màu (kem, xanh bạc hà, hồng phấn)"),
    ("warranty", "Bảo hành 6 tháng"),
    ("warranty", "Lỗi do nhà sản xuất đổi mới trong 7 ngày"),
    ("shipping", "Miễn phí vận chuyển đơn từ 199.000 đồng"),
    ("limitation", "Không xay đồ nóng và không xay xương hay đá cục lớn"),
    ("warranty", "Đổi trả trong 30 ngày"),
    ("promotion", "Tặng kèm 1 cáp sạc USB-C và 1 chổi vệ sinh khi mua trong tuần này"),
]


def test_anh_20_facts_drop_only_the_rewritten_name_and_category():
    lines = [t for _, t in ANH_20]
    payload = json.dumps({"facts": [{"type": t, "text": x, "evidence": x} for t, x in ANH_20]})
    r = extract_facts("\n".join(lines), lambda _p: payload)
    assert [(d.text, d.reason) for d in r.dropped] == [
        (ANH_20[0][1], "form_field_duplicate"),
        (ANH_20[1][1], "form_field_duplicate"),
    ]
    assert [f.text for f in r.facts] == lines[2:]
    # the two period facts (7 days exchange, 30 days return) are the 14th and 17th kept facts
    assert len(r.warnings) == 1 and r.warnings[0]["code"] == "policy_period_conflict"
    assert [r.facts[i].text for i in r.warnings[0]["facts"]] == [lines[15], lines[18]]


def test_rewritten_form_field_variants_are_dropped():
    for text in (
        "Tên sản phẩm là Máy xay Fresho M2",
        "Tên sản phẩm: Máy xay Fresho M2",
        "Sản phẩm tên Máy xay Fresho M2",
        "Ngành hàng là Đồ gia dụng nhà bếp",
        "Danh mục là Đồ gia dụng nhà bếp.",
        "Loại sản phẩm là Máy xay sinh tố",
        "Thuộc ngành hàng Đồ gia dụng nhà bếp",
    ):
        r = extract_facts(
            text,
            lambda _p, t=text: json.dumps(
                {"facts": [{"type": "feature", "text": t, "evidence": t}]}
            ),
        )
        assert [d.reason for d in r.dropped] == ["form_field_duplicate"], text


def test_form_field_rewrite_with_extra_information_is_kept():
    for text in (
        "Ngành hàng là Đồ gia dụng, giao toàn quốc trong 2 ngày",
        "Tên sản phẩm là Máy xay Fresho M2 (tặng kèm cáp sạc)",
    ):
        r = extract_facts(
            text,
            lambda _p, t=text: json.dumps(
                {"facts": [{"type": "feature", "text": t, "evidence": t}]}
            ),
        )
        assert [f.text for f in r.facts] == [text], text
