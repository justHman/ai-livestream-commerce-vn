"""Hotfix 2026-11-11: decimals read wrong, "no promotion" talk, thin opening, two origins in one claim."""

from __future__ import annotations

import pytest

from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    SessionBrief,
    UnitSpec,
    build_unit_prompt,
    check_unit_text,
    unsafe_claims,
)

FACTS = (
    "Khối lượng 0,84 kg",
    "Bluetooth 5.3",
    "Mỗi viên 4,5 gram",
    "Thùng 24 gói",
    "Bảo hành 12 tháng",
)


def _check(text, role="highlight", prices=()):
    spec = UnitSpec(role, claims=FACTS, with_price=role == "offer")
    return check_unit_text(text, spec=spec, approved=FACTS, prices=prices)


@pytest.mark.parametrize(
    "bad",
    [
        "Thùng hai mươi bốn gói nặng tám trăm bốn mươi kg.",
        "Máy dùng Bluetooth năm ba nên kết nối nhanh.",
        "Mỗi viên nặng bốn năm gram.",
    ],
)
def test_a_decimal_read_as_an_integer_is_rejected(bad):
    assert _check(bad)[1] == "number_mismatch"


@pytest.mark.parametrize(
    "good",
    [
        "Thùng 24 gói, nặng 0,84 kg.",
        "Thùng hai mươi bốn gói nặng không phẩy tám mươi bốn ki lô gam.",
        "Bluetooth 5.3 và mỗi viên 4,5 gram nhé.",
        "Bảo hành mười hai tháng, mình nói một chút về nó.",
    ],
)
def test_digits_or_the_right_words_pass(good):
    assert _check(good)[1] is None


def test_an_approved_price_in_words_still_passes_and_a_wrong_one_does_not():
    prices = ("399000",)
    ok = "Giá ba trăm chín mươi chín nghìn đồng, bảo hành 12 tháng."
    assert _check(ok, role="offer", prices=prices)[1] is None
    wrong = "Giá ba trăm chín mươi chín nghìn đồng, bảo hành 6 tháng."
    assert _check(wrong, role="offer", prices=prices)[1] == "number_mismatch"
    assert _check("Giá bốn trăm nghìn đồng.", role="offer", prices=prices)[1] == "number_mismatch"


def test_k_shorthand_fact_may_be_spoken_with_nghin():
    spec = UnitSpec("offer", claims=("Phí gửi 30k",))
    _text, why = check_unit_text(
        "Phí gửi ba mươi nghìn đồng.", spec=spec, approved=("Phí gửi 30k",), prices=()
    )
    assert why is None, why


@pytest.mark.parametrize(
    "text",
    [
        "Hiện không có chương trình khuyến mãi đi kèm.",
        "Giá này không thêm giảm gì cả nhé.",
        "Không có giảm giá gì thêm đâu.",
    ],
)
def test_saying_there_is_no_promotion_is_rejected(text):
    spec = UnitSpec("offer", claims=("Sạc nhanh",))
    assert check_unit_text(text, spec=spec, approved=("Sạc nhanh",), prices=())[1] == "missing_info"


def test_the_offer_prompt_says_nothing_about_promotions_when_there_are_none():
    product = ProductBrief("p", "Loa mini", prices=("399000",), claims=("Sạc nhanh",))
    spec = UnitSpec("offer", claims=("Sạc nhanh",), with_price=True)
    prompt = build_unit_prompt(SessionBrief(), product, [spec], 0)
    assert "Không có ưu đãi" not in prompt
    assert "cũng không nhắc là không có" in prompt


def test_opening_prompt_demands_two_sentences_and_an_invitation():
    product = ProductBrief("p", "Loa mini")
    prompt = build_unit_prompt(SessionBrief(), product, [UnitSpec("opening")], 0)
    assert "TỐI THIỂU 2 câu" in prompt and "PHẢI mời" in prompt


@pytest.mark.parametrize("word", ["Welcome", "comment", "Hello"])
def test_english_words_in_an_opening_are_foreign_script(word):
    text = f"Chào mọi người, {word} nha. Mọi người cứ bình luận nhé."
    assert check_unit_text(text, spec=UnitSpec("opening"))[1] == "foreign_script"


def test_one_claim_naming_two_origins_is_unsafe():
    both = "Nhập khẩu từ Đức và sản xuất tại Việt Nam"
    assert unsafe_claims((both, "Bảo hành 12 tháng")) == {both}
    assert unsafe_claims(("Xuất xứ: Đức", "Sản xuất tại Việt Nam")) == {
        "Xuất xứ: Đức",
        "Sản xuất tại Việt Nam",
    }
    assert unsafe_claims(("Nhập khẩu từ Đức",)) == set()
