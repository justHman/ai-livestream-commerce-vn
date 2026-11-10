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
