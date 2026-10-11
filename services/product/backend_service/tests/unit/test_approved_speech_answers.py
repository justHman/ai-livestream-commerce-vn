"""A viewer question is answered with a whole approved fact, not only with a script sentence."""

from __future__ import annotations

import json

from backend.application.script_authoring.approved_speech import ApprovedProduct


def _product(spoken_text: str, **facts) -> ApprovedProduct:
    return ApprovedProduct("p1", "v1", spoken_text, "h", json.dumps(facts, ensure_ascii=False))


def test_claims_need_not_appear_verbatim_in_the_rewritten_script() -> None:
    product = _product(
        "Mẫu này đổi trả thoải mái trong mười lăm ngày nhé.",
        allowed_claims=["Miễn phí đổi trả trong 15 ngày đầu kể từ khi nhận hàng"],
    )
    assert product.answers() == ("Miễn phí đổi trả trong mười lăm ngày đầu kể từ khi nhận hàng",)


def test_the_price_is_only_the_approved_price_in_a_fixed_sentence() -> None:
    product = _product("Giá ưu đãi nhé.", prices=["180000.00 VND"], allowed_claims=[])
    assert product.answers() == ("Giá một trăm tám mươi nghìn đồng.",)


def test_blank_and_duplicate_claims_are_dropped_and_nothing_is_invented() -> None:
    product = _product("x", allowed_claims=["  ", "Hàng chính hãng", "Hàng chính hãng"])
    assert product.answers() == ("Hàng chính hãng",)
    assert _product("x").answers() == ()
