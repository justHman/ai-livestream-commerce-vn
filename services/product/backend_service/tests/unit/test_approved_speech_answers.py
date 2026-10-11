"""A viewer question is answered with a whole approved fact, not only with a script sentence."""

from __future__ import annotations

import json

from backend.application.script_authoring.approved_speech import ApprovedProduct


def _product(spoken_text: str, **facts) -> ApprovedProduct:
    return ApprovedProduct("p1", "v1", spoken_text, "h", json.dumps(facts, ensure_ascii=False))


def test_claims_need_not_appear_verbatim_in_the_rewritten_script() -> None:
    product = _product(
        "Mẫu này miễn phí đổi trả trong mười lăm ngày đầu kể từ khi nhận hàng nhé.",
        allowed_claims=["Miễn phí đổi trả trong 15 ngày đầu kể từ khi nhận hàng"],
    )
    assert product.answers() == ("Miễn phí đổi trả trong mười lăm ngày đầu kể từ khi nhận hàng",)


def test_the_price_is_only_the_approved_price_in_a_fixed_sentence() -> None:
    product = _product(
        "Hôm nay giá một trăm tám mươi nghìn đồng nhé.", prices=["180000.00 VND"], allowed_claims=[]
    )
    assert product.answers() == ("Giá một trăm tám mươi nghìn đồng.",)


def test_blank_and_duplicate_claims_are_dropped_and_nothing_is_invented() -> None:
    product = _product(
        "Hàng chính hãng nhé.", allowed_claims=["  ", "Hàng chính hãng", "Hàng chính hãng"]
    )
    assert product.answers() == ("Hàng chính hãng",)
    assert _product("x").answers() == ()


def test_a_phone_number_in_a_claim_is_read_digit_by_digit() -> None:
    product = _product(
        "Liên hệ không chín không một hai ba bốn năm sáu bảy để được hỗ trợ.",
        allowed_claims=["Liên hệ 0901.234.567 để được hỗ trợ"],
    )
    assert product.answers() == (
        "Liên hệ không chín không một hai ba bốn năm sáu bảy để được hỗ trợ",
    )


def test_a_promotion_or_condition_is_never_offered_without_its_condition() -> None:
    product = _product(
        "Giảm hai mươi phần trăm. Chỉ áp dụng khi mua hai sản phẩm. Hàng chính hãng.",
        allowed_claims=["Giảm 20%", "Chỉ áp dụng khi mua hai sản phẩm", "Hàng chính hãng"],
        claims_by_type={
            "promotion": ["Giảm 20%"],
            "limitation": ["Chỉ áp dụng khi mua hai sản phẩm"],
        },
    )
    answers = product.answers()
    assert "Hàng chính hãng" in answers
    assert "Giảm hai mươi phần trăm" not in answers
    assert any(
        "Giảm hai mươi phần trăm" in a and "Chỉ áp dụng khi mua hai sản phẩm" in a for a in answers
    )


def test_a_hyphenated_phone_number_is_read_digit_by_digit() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    spoken = compile_spoken_text("Gọi 0901-234-567 nhé").spoken_text
    assert spoken == "Gọi không chín không một hai ba bốn năm sáu bảy nhé"


def test_a_price_inside_a_bigger_number_is_not_the_stated_price() -> None:
    product = _product("Giá một trăm tám mươi nghìn đồng nhé.", prices=["180000 VND", "80000 VND"])
    assert product.answers() == ("Giá một trăm tám mươi nghìn đồng.",)


def test_a_condition_stays_with_its_promotion_even_if_the_script_omits_it() -> None:
    product = _product(
        "Đang giảm hai mươi phần trăm nhé.",
        allowed_claims=["Giảm 20%", "Chỉ áp dụng khi mua hai sản phẩm"],
        claims_by_type={"promotion": ["Giảm 20%"]},
    )
    (answer,) = product.answers()
    assert "hai mươi phần trăm" in answer and "Chỉ áp dụng khi mua hai sản phẩm" in answer


def test_a_natural_number_must_be_a_whole_number_of_the_facts() -> None:
    from backend.application.script_authoring.approved_speech import _validate_natural

    product = _product("x", prices=["180000 VND"], allowed_claims=[])
    facts = ["Giá một trăm tám mươi nghìn đồng."]
    assert _validate_natural("Giá một trăm tám mươi nghìn đồng nhé.", product, facts)
    assert _validate_natural("Giá tám mươi nghìn đồng nhé.", product, facts) is None
