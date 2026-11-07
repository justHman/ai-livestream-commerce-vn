"""Contradictory policy facts, a 100% discount, "em nó" and unapproved praise never reach the spoken script.

Regression for the 2026-11-07 test: "Mua rồi miễn đổi trả" and "Có đổi trả được không? Được 100%" were both
must-keep, so the writer merged them into "được 100% nếu mua rồi mà"."""

from __future__ import annotations

import pytest

from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    check_unit_text,
    plan_units,
    unsafe_claims,
)

NO_RETURN = "Mua rồi miễn đổi trả"
YES_RETURN_FAQ = "Có đổi trả được không? Được 100%"


def _spoken_claims(product: ProductBrief) -> set[str]:
    units = plan_units(product, first=True, last=True)
    return {c for u in units for c in (*u.claims, *u.promos, *u.must)}


def test_a_return_promise_and_a_blanket_denial_are_both_unsafe():
    assert unsafe_claims((NO_RETURN, YES_RETURN_FAQ)) == {NO_RETURN, YES_RETURN_FAQ}


@pytest.mark.parametrize(
    "claims",
    [
        ("Đổi trả trong 7 ngày", "Không đổi trả sau 7 ngày"),
        ("Bảo hành 12 tháng", "Không bảo hành khi rơi vỡ"),
        ("Miễn phí đổi trả", "Giao hàng ngay trong ngày"),
        ("Có đổi trả được không? Được trong 7 ngày", "Bảo hành 12 tháng"),
    ],
)
def test_a_scoped_exception_or_agreeing_policies_are_kept(claims):
    assert unsafe_claims(claims) == set()


def test_a_blanket_no_warranty_contradicts_a_warranty_promise():
    assert unsafe_claims(("Không bảo hành", "Bảo hành 12 tháng")) == {
        "Không bảo hành",
        "Bảo hành 12 tháng",
    }


def test_a_return_denial_does_not_contradict_a_warranty_promise():
    assert unsafe_claims(("Không đổi trả", "Bảo hành 12 tháng")) == set()


def test_a_free_product_discount_is_unsafe():
    assert unsafe_claims(("Giảm 100% vì dễ bắt",)) == {"Giảm 100% vì dễ bắt"}


def test_a_free_shipping_discount_stays():
    assert unsafe_claims(("Giảm 100% phí vận chuyển cho đơn từ 300k",)) == set()


def test_a_normal_discount_stays():
    assert unsafe_claims(("Giảm 20% cho đơn đầu tiên",)) == set()


def test_planner_speaks_neither_contradicting_return_claim():
    product = ProductBrief("p", "Chuột", claims=(NO_RETURN, YES_RETURN_FAQ, "Hàng tự nhiên"))
    assert _spoken_claims(product) == {"Hàng tự nhiên"}


def test_planner_speaks_no_free_discount():
    product = ProductBrief(
        "p", "Chuột", claims=("Hàng tự nhiên",), discounts=("Giảm 100% vì dễ bắt",)
    )
    assert "Giảm 100% vì dễ bắt" not in _spoken_claims(product)


def test_planner_keeps_a_real_must_claim_next_to_a_contradiction():
    product = ProductBrief("p", "Chuột", claims=(NO_RETURN, YES_RETURN_FAQ, "Chỉ giao nội thành"))
    units = plan_units(product, first=True, last=True)
    assert any("Chỉ giao nội thành" in u.must for u in units)


@pytest.mark.parametrize(
    "text",
    [
        "Các bạn thấy không, em nó còn sở hữu chiếc đuôi dài nổi bật nữa chứ.",
        "Đây là một em thú cưng rất đặc biệt, đuôi dài.",
        "Những em hamster này có đuôi dài.",
    ],
)
def test_calling_the_product_em_is_rejected(text):
    assert check_unit_text(text)[1] == "address"


@pytest.mark.parametrize(
    "text",
    [
        "Sản phẩm này hợp để làm quà cho trẻ em.",
        "Mình có một em bé ở nhà nên mình hiểu nhu cầu này.",
    ],
)
def test_ordinary_em_words_are_not_address(text):
    assert check_unit_text(text)[1] is None


@pytest.mark.parametrize(
    "text",
    [
        "Hàng tự nhiên nên mọi người yên tâm về độ an toàn nhé.",
        "Shop giao ngay trong ngày để bạn nhận sản phẩm nhanh nhất.",
        "Giá này rất hợp lý để mọi người sở hữu ngay.",
    ],
)
def test_praise_the_owner_never_wrote_is_rejected(text):
    assert check_unit_text(text)[1] == "hard_sell"


def test_praise_the_owner_wrote_is_allowed():
    text = "Sản phẩm này có độ an toàn cao."
    assert check_unit_text(text, approved=("Độ an toàn cao đã kiểm định",))[1] is None
