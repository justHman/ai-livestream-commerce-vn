"""Polish 2026-11-11 (3): jargon spoken, origin conflict, intro names the product, fallback offer."""

from __future__ import annotations

import pytest

from backend.application.script_authoring.compile import compile_spoken_text
from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    SessionBrief,
    UnitSpec,
    build_unit_prompt,
    check_unit_text,
    fallback_unit_text,
    unsafe_claims,
)


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("Cổng USB-C", "Cổng u ét bi xi"),
        ("cổng USB-A", "cổng u ét bi ây"),
        ("Wi-Fi 6", "wai phai sáu"),
        ("Bluetooth 5.3", "blu tút năm phẩy ba"),
        ("Dung lượng 256GB", "Dung lượng hai trăm năm mươi sáu ghi ga bai"),
        ("Pin 5000mAh", "Pin năm nghìn mi li am pe giờ"),
        ("Chuẩn IPX4", "Chuẩn ai pi ếch bốn"),
        ("Màn 4K", "Màn bốn ca"),
        ("Kem SPF50", "Kem ét pi ép năm mươi"),
        ("Nặng 0,84 kg", "Nặng không phẩy tám mươi bốn ki lô gam"),
        ("Giảm 20%", "Giảm hai mươi phần trăm"),
    ],
)
def test_product_jargon_is_spoken_and_stable(raw, spoken):
    out = compile_spoken_text(raw).spoken_text
    assert out == spoken
    assert compile_spoken_text(out).spoken_text == out


def test_two_origins_across_claims_or_in_one_sentence_are_unsafe():
    assert unsafe_claims(("Nhập khẩu chính hãng từ Đức", "Made in Vietnam")) == {
        "Nhập khẩu chính hãng từ Đức",
        "Made in Vietnam",
    }
    assert unsafe_claims(("Nhập khẩu từ Đức", "Xuất xứ Germany")) == set()


def test_a_text_naming_two_origins_or_a_contradicted_one_is_rejected():
    spec = UnitSpec("highlight", claims=("Nhập khẩu từ Đức", "Sản xuất tại Việt Nam"))
    for text in (
        "Máy nhập khẩu từ Đức và sản xuất tại Việt Nam nhé.",
        "Máy nhập khẩu từ Đức, rất hay dùng.",
    ):
        assert check_unit_text(text, spec=spec, approved=spec.claims)[1] == "mashup"


def test_intro_must_name_the_product():
    spec = UnitSpec("intro", claims=("Gạo thơm hạt dài",))
    name = "Gạo ST25 túi 5kg"
    bad = "Hôm nay mình có một món rất đáng thử, hạt dài và thơm."
    assert (
        check_unit_text(bad, spec=spec, approved=spec.claims, product_name=name)[1] == "intro_name"
    )
    good = "Mình giới thiệu gạo ST25, hạt dài và thơm."
    assert check_unit_text(good, spec=spec, approved=spec.claims, product_name=name)[1] is None


def test_intro_prompt_forbids_repeating_the_opening():
    product = ProductBrief(product_id="p", name="Gạo ST25", claims=("Hạt dài",))
    specs = [UnitSpec("opening"), UnitSpec("intro", claims=("Hạt dài",))]
    prompt = build_unit_prompt(SessionBrief(title="t"), product, specs, 1)
    assert "nêu đúng tên sản phẩm" in prompt
    assert "Mở đầu" in prompt


def test_fallback_offer_lists_prices_and_ends_every_sentence():
    product = ProductBrief(product_id="p", name="Thùng gói", prices=("360000", "990000"))
    spec = UnitSpec(
        "offer", promos=("Giảm 10 nghìn khi mua ba thùng", "Tặng hai gói"), with_price=True
    )
    text = fallback_unit_text(spec, product)
    assert "hoặc" not in text
    assert text.startswith("Các mức giá")
    assert all(p.rstrip().endswith(".") for p in text.split("\n\n"))


def test_fallback_keeps_digits_for_display_and_compiles_spoken_form():
    product = ProductBrief(product_id="p", name="Máy")
    spec = UnitSpec("highlight", claims=("Nặng 0,84 kg",))
    text = fallback_unit_text(spec, product)
    assert "0,84" in text
    assert "không phẩy tám mươi bốn" in compile_spoken_text(text).spoken_text


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("Giảm 2K", "Giảm hai nghìn"),
        ("Chỉ 4K", "Chỉ bốn nghìn"),
        ("Giá 8K", "Giá tám nghìn"),
        ("Giá 20K", "Giá hai mươi nghìn"),
        ("Nhẫn vàng 24K", "Nhẫn vàng hai mươi bốn nghìn"),
        ("Màn hình 4K", "Màn hình bốn ca"),
        ("Quay video 8K", "Quay video tám ca"),
        ("Wi-Fi6", "wai phai sáu"),
        ("Bluetooth5.3", "blu tút năm phẩy ba"),
        ("USB-C3", "u ét bi xi ba"),
        ("Kính UV400", "Kính u vê bốn trăm"),
    ],
)
def test_resolution_k_only_beside_a_screen_word_and_glued_numbers(raw, spoken):
    assert compile_spoken_text(raw).spoken_text == spoken


@pytest.mark.parametrize(
    "claims",
    [
        ("Nhập khẩu trực tiếp từ Đức", "Sản xuất tại Đức"),
        ("Made in USA", "Xuất xứ Mỹ"),
        ("Made in Germany", "Nhập khẩu từ Đức"),
        ("Nhập khẩu và phân phối bởi Công ty A", "Sản xuất tại Việt Nam"),
        ("Hàng nhập từ Anh", "Xuất xứ UK"),
    ],
)
def test_same_origin_or_no_origin_words_are_not_a_conflict(claims):
    assert unsafe_claims(claims) == set()


@pytest.mark.parametrize(
    "claims",
    [
        ("Nhập khẩu linh kiện từ Nhật", "Sản xuất tại VN"),
        ("Nhập khẩu trực tiếp từ Đức", "Made in Vietnam"),
    ],
)
def test_different_known_origins_are_a_conflict(claims):
    assert unsafe_claims(claims) == set(claims)


def test_one_origin_sentence_is_not_rejected_when_data_has_one_origin():
    claims = ("Nhập khẩu trực tiếp từ Đức", "Sản xuất tại Đức")
    spec = UnitSpec("highlight", claims=claims)
    out = check_unit_text("Máy nhập khẩu từ Đức nhé.", spec=spec, approved=claims)
    assert out[1] is None


def test_short_word_name_matches_but_a_nameless_intro_is_rejected():
    spec = UnitSpec("intro", claims=("Cotton",))
    kw = {"spec": spec, "approved": spec.claims, "product_name": "Áo Polo Nam"}
    assert check_unit_text("Mình có chiếc áo này, vải cotton.", **kw)[1] is None
    assert check_unit_text("Mình có món này, vải cotton.", **kw)[1] == "intro_name"
