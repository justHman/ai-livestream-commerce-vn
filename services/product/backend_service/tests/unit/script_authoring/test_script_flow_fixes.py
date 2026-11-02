"""Script flow fixes: length follows the facts, verbatim units fit the bound, neutral wording,
order-code CTA, quantity-vs-price gate, English leaks."""

from __future__ import annotations

import pytest

from backend.application.script_authoring.gate.rules.commerce_claims import _PRICE_RE
from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    SessionBrief,
    UnitSpec,
    _duration_s,
    build_unit_prompt,
    check_unit_text,
    fallback_unit_text,
    plan_units,
)
from backend.application.script_authoring.service_impl import _order_code

TEN_FACTS = tuple(f"Thông tin số {n} của gói." for n in range(1, 11))
THIRTY_EIGHT_FACTS = tuple(f"Thông tin số {n} của nền tảng." for n in range(1, 39))
# ~9 s spoken each (16 characters per second).
LONG_CLAIMS = tuple(
    f"Điểm số {n} rất dài dòng, nói về cách chuẩn bị sản phẩm, chọn người dẫn, nghe thử giọng và theo dõi phiên live trong Studio."
    for n in range(1, 4)
)


def _revisits(product: ProductBrief, target: float) -> int:
    return sum(
        u.revisit for u in plan_units(product, first=False, last=False, target_duration_s=target)
    )


def test_thin_product_gets_at_most_one_recap_instead_of_padding_to_the_platform_target():
    # 360 s used to plan ~7 recap units for ten short facts.
    assert _revisits(ProductBrief("p", "Gói A", claims=TEN_FACTS), 360) <= 1


def test_thin_product_targets_about_fourteen_seconds_per_fact_not_the_platform_target():
    units = plan_units(
        ProductBrief("p", "Gói A", claims=TEN_FACTS), first=False, last=False, target_duration_s=360
    )
    assert sum(u.target_seconds for u in units) == pytest.approx(0.85 * 140, abs=1.0)


def test_rich_product_keeps_the_full_platform_target():
    units = plan_units(
        ProductBrief("p", "Nền tảng", claims=THIRTY_EIGHT_FACTS),
        first=False,
        last=False,
        target_duration_s=360,
    )
    assert sum(u.target_seconds for u in units) == pytest.approx(0.85 * 360, abs=1.0)


def test_very_thin_product_never_goes_below_ninety_seconds():
    units = plan_units(
        ProductBrief("p", "Gói A", claims=("Một ý ngắn thôi.",)),
        first=False,
        last=False,
        target_duration_s=360,
    )
    assert sum(u.target_seconds for u in units) == pytest.approx(0.85 * 90, abs=1.0)


def test_claims_that_are_long_when_spoken_split_into_more_units():
    units = plan_units(ProductBrief("p", "Gói A", claims=LONG_CLAIMS), first=False, last=False)
    assert [u.role for u in units].count("highlight") == 2


def test_splitting_by_duration_never_drops_a_claim():
    units = plan_units(ProductBrief("p", "Gói A", claims=LONG_CLAIMS), first=False, last=False)
    assert {c for u in units for c in u.claims} == set(LONG_CLAIMS)


def test_extractive_fallback_fits_the_role_bound_and_keeps_the_must_claim_first():
    must = "Điều kiện cuối cùng cần nhớ là phút không dùng được sau khi hết hạn."
    spec = UnitSpec("highlight", claims=(*LONG_CLAIMS * 3, must), must=(must,))
    text = fallback_unit_text(spec, ProductBrief("p", "Gói A"))
    assert must in text and _duration_s(text) <= 60


def _prompt(product: ProductBrief, spec: UnitSpec) -> str:
    return build_unit_prompt(SessionBrief(), product, [UnitSpec("intro"), spec], 1)


def test_plan_is_called_this_plan_not_a_model():
    prompt = _prompt(
        ProductBrief("p", "Gói 1 ngày", claims=("Ý một.",)),
        UnitSpec("highlight", claims=("Ý một.",)),
    )
    assert "gọi 'gói này'" in prompt


def test_other_products_are_called_this_product():
    prompt = _prompt(
        ProductBrief("p", "Áo denim", claims=("Ý một.",)), UnitSpec("highlight", claims=("Ý một.",))
    )
    assert "gọi 'sản phẩm này'" in prompt


def test_prompt_no_longer_says_mau_nay():
    prompt = _prompt(
        ProductBrief("p", "Gói 1 ngày", claims=("Ý một.",)),
        UnitSpec("highlight", claims=("Ý một.",)),
    )
    assert "mẫu này" not in prompt


def test_default_call_to_action_has_no_shopping_cart():
    prompt = _prompt(ProductBrief("p", "Gói A"), UnitSpec("offer", with_cta=True))
    assert "giỏ hàng" not in prompt.split("Cuối phần này")[1]


def test_order_code_call_to_action_asks_for_the_exact_comment():
    prompt = _prompt(
        ProductBrief("p", "Gói A", order_code="G30"),
        UnitSpec("offer", with_cta=True, order_code="G30"),
    )
    assert "'Chốt G30 1'" in prompt


def test_plan_units_hand_the_order_code_to_every_unit():
    units = plan_units(
        ProductBrief("p", "Gói A", claims=("Ý một.",), order_code="G30"), first=False, last=False
    )
    assert {u.order_code for u in units} == {"G30"}


def test_order_code_is_accepted_in_the_unit_that_invites_it():
    spec = UnitSpec("offer", with_cta=True, order_code="G30")
    text, why = check_unit_text("Bạn nào muốn chốt thì bình luận: Chốt G30 1.", spec=spec)
    assert (text is None, why) == (False, None)


def test_order_code_is_rejected_in_a_unit_that_must_not_invite():
    spec = UnitSpec("highlight", claims=("Ý một.",), order_code="G30")
    assert check_unit_text("Muốn lấy thì bình luận: Chốt G30 1.", spec=spec) == (None, "cta")


@pytest.mark.parametrize(
    ("raw", "code"),
    [("g30", "G30"), ("T1800", "T1800"), (None, ""), ("x", ""), ("G-30", ""), ("1ABC", "")],
)
def test_order_code_from_another_service_is_sanitized(raw, code):
    assert _order_code(raw) == code


@pytest.mark.parametrize(
    "text",
    ["Gói có 1.800 phút dùng trong 30 ngày.", "Có 1.799.000 phút.", "Đã có 2.500 người dùng thử."],
)
def test_grouped_quantities_are_not_prices(text):
    assert _PRICE_RE.findall(text) == []


@pytest.mark.parametrize(
    ("text", "price"),
    [
        ("Giá 1.799.000đ cho cả gói.", "1.799.000đ"),
        ("Giá 299.000 đồng.", "299.000 đồng"),
        ("Giá chỉ 299.000.", "299.000"),
        ("Giá 499k thôi.", "499k"),
    ],
)
def test_real_prices_are_still_found(text, price):
    assert _PRICE_RE.findall(text) == [price]


def test_english_number_word_in_the_script_is_rejected():
    assert check_unit_text("Gói dùng được trong thirty ngày kể từ khi thanh toán.") == (
        None,
        "foreign_script",
    )


def test_english_word_is_allowed_when_the_owner_wrote_it():
    text, why = check_unit_text(
        "Gói dùng được trong thirty ngày kể từ khi thanh toán.", approved=("Thời hạn thirty ngày.",)
    )
    assert (text is None, why) == (False, None)


def test_known_brand_and_product_words_are_not_foreign():
    text, why = check_unit_text(
        "Livento phát lên Facebook Live, thanh toán qua PayOS, có gói Free và avatar LemonSlice."
    )
    assert (text is None, why) == (False, None)
