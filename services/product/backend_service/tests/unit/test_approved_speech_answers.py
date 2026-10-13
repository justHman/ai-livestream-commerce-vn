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
    assert product.answers() == ("Giá một trăm tám mươi nghìn đồng nhé mọi người.",)


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
    assert product.answers() == ("Giá một trăm tám mươi nghìn đồng nhé mọi người.",)


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


def test_a_typed_limitation_stays_with_the_promotion_whatever_its_wording() -> None:
    product = _product(
        "Đang giảm hai mươi phần trăm nhé.",
        allowed_claims=["Giảm 20%", "Áp dụng cho đơn từ 500000 đồng"],
        claims_by_type={
            "promotion": ["Giảm 20%"],
            "limitation": ["Áp dụng cho đơn từ 500000 đồng"],
        },
    )
    (answer,) = product.answers()
    assert "hai mươi phần trăm" in answer and "Áp dụng cho đơn từ" in answer


def test_a_promotion_the_script_does_not_state_stays_out_of_the_price_answer() -> None:
    product = _product(
        "Giá một trăm tám mươi nghìn đồng nhé.",
        product_name="Áo khoác denim",
        prices=["180000 VND"],
        allowed_claims=["Giảm 50% khi mua hai sản phẩm"],
        claims_by_type={"promotion": ["Giảm 50% khi mua hai sản phẩm"]},
    )
    assert "Áo khoác denim giá một trăm tám mươi nghìn đồng nhé mọi người." in product.answers()
    assert not any("năm mươi phần trăm" in a and "giá" in a for a in product.answers())


def _live_like_product() -> ApprovedProduct:
    return _product(
        "Giao hàng toàn quốc từ hai đến ba ngày, nội thành H C M và H N ship hỏa tốc trong một "
        "ngày. Mẫu này có giá chỉ một trăm tám mươi nghìn đồng thôi nha. Khi mua hai sản phẩm sẽ "
        "được giảm mười phần trăm tổng hóa đơn. Miễn phí đổi trả trong mười lăm ngày đầu kể từ "
        "khi nhận hàng.",
        product_name="Áo khoác denim",
        prices=["180000 VND"],
        allowed_claims=[
            "Giao hàng toàn quốc từ 2 đến 3 ngày, nội thành HCM và HN ship hỏa tốc trong 1 ngày",
            "Mua 2 sản phẩm giảm 10% tổng hóa đơn",
            "Miễn phí đổi trả trong 15 ngày đầu kể từ khi nhận hàng",
        ],
        claims_by_type={"promotion": ["Mua 2 sản phẩm giảm 10% tổng hóa đơn"]},
    )


def test_giá_chỉ_with_the_approved_price_is_not_a_restriction() -> None:
    from backend.application.script_authoring.approved_speech import _natural_verdict

    product = _live_like_product()
    facts = list(product.answers())
    ok = "Áo khoác denim giá chỉ một trăm tám mươi nghìn đồng thôi nha mọi người."
    assert _natural_verdict(ok, product, facts) == (ok, "")


def test_a_restriction_the_facts_do_not_contain_is_still_refused() -> None:
    from backend.application.script_authoring.approved_speech import _natural_verdict

    product = _live_like_product()
    facts = list(product.answers())
    for invented in (
        "Mẫu này chỉ còn hai sản phẩm thôi nha mọi người.",  # invented scarcity
        "Áo khoác denim giá chỉ hai mươi nghìn đồng thôi nha mọi người.",  # not the approved price
        "Giao hàng toàn quốc từ hai đến ba ngày, không mất phí nha mọi người.",  # invented negation
    ):
        text, reason = _natural_verdict(invented, product, facts)
        assert text is None and reason in {"restriction_word", "number_not_in_facts"}, invented


def test_the_refusal_reason_is_a_fixed_code_never_the_text() -> None:
    from backend.application.script_authoring.approved_speech import _natural_verdict

    product = _live_like_product()
    facts = list(product.answers())
    for raw, reason in (
        ("Mẫu này giá 190 nghìn đồng nha.", "digit"),  # not the approved price: stays digits
        ("Bạn nào muốn đặt hàng thì nhắn mình nhé.", "guard_cta"),
        ("Giao hàng trong năm ngày nhé mọi người.", "number_not_in_facts"),
        ("Nội thành Hồ Chí Minh và Hà Nội ship hỏa tốc nhé mọi người.", "ungrounded_words"),
    ):
        assert _natural_verdict(raw, product, facts) == (None, reason), raw


def test_an_approved_price_written_in_digits_is_read_once() -> None:
    from backend.application.script_authoring.approved_speech import _natural_verdict

    product = _live_like_product()
    text, _ = _natural_verdict(
        "Mẫu này giá 180 nghìn đồng nha mọi người.", product, list(product.answers())
    )
    assert text == "Mẫu này giá một trăm tám mươi nghìn đồng nha mọi người."


def _price_answer(product: ApprovedProduct) -> str:
    (price,) = [a for a in product.answers() if a.startswith("Áo khoác denim giá")]
    return price


PRICE_ONLY = "Áo khoác denim giá một trăm tám mươi nghìn đồng nhé mọi người."


def test_the_price_answer_never_mentions_the_promotion_so_no_condition_can_be_dropped() -> None:
    for script, claims in (
        (
            "Giá một trăm tám mươi nghìn đồng nhé. "
            "Giảm mười phần trăm tại TP. HCM, nhưng chỉ áp dụng cho đơn từ hai sản phẩm.",
            ["Giảm 10% tại TP. HCM", "Chỉ áp dụng cho đơn từ 2 sản phẩm"],
        ),
        (
            "Giá một trăm tám mươi nghìn đồng nhé. "
            "Giảm mười phần trăm... nhưng chỉ áp dụng cho đơn giao hàng nội thành.",
            ["Giảm 10%", "Áp dụng cho đơn giao hàng nội thành"],
        ),
    ):
        product = _product(
            script,
            product_name="Áo khoác denim",
            prices=["180000 VND"],
            allowed_claims=claims,
            claims_by_type={"promotion": [claims[0]]},
        )
        assert _price_answer(product) == PRICE_ONLY, script
        combined = [a for a in product.answers() if a.startswith("Giảm")]
        assert combined and "mười phần trăm" in combined[0]


def test_the_combined_promotion_answer_keeps_the_promotion_with_its_condition() -> None:
    product = _product(
        "Giá một trăm tám mươi nghìn đồng nhé. "
        "Giảm mười phần trăm tại TP. HCM, nhưng chỉ áp dụng cho đơn từ hai sản phẩm.",
        product_name="Áo khoác denim",
        prices=["180000 VND"],
        allowed_claims=["Giảm 10% tại TP. HCM", "Chỉ áp dụng cho đơn từ 2 sản phẩm"],
        claims_by_type={"promotion": ["Giảm 10% tại TP. HCM"]},
    )
    (combined,) = [a for a in product.answers() if a.startswith("Giảm")]
    assert "mười phần trăm" in combined and "hai sản phẩm" in combined
