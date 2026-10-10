"""Task 4.1/4.2 tests: display->spoken compilation and idempotency.

Deterministic, pure: no LLM/network/filesystem. Every case asserts the
exact spoken form, the provenance list of applied normalizer ids, and
idempotency (``compile_spoken_text(compile_spoken_text(x)) ==
compile_spoken_text(x)``).
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.compile import (
    CompiledScriptVersion,
    CompileResult,
    compile_spoken_text,
    expand_vietnamese_number,
)


def _compile(display: str) -> CompileResult:
    return compile_spoken_text(display)


def test_price_expands_to_spoken_words() -> None:
    result = _compile("Kem ABC chỉ 299.000đ")
    assert result.spoken_text == "Kem A B C chỉ hai trăm chín mươi chín nghìn đồng"
    assert "currency_and_price" in result.applied


def test_price_with_space_and_denomination_word() -> None:
    result = _compile("Giá 1.299.000 đồng")
    assert result.spoken_text == "Giá một triệu hai trăm chín mươi chín nghìn đồng"
    assert "currency_and_price" in result.applied


def test_percent_expands_to_phan_tram() -> None:
    result = _compile("Giảm 20% hôm nay")
    assert result.spoken_text == "Giảm hai mươi phần trăm hôm nay"
    assert "percent" in result.applied


def test_decimal_percent() -> None:
    result = _compile("Giảm 12,5%")
    assert result.spoken_text == "Giảm mười hai phẩy năm phần trăm"
    assert "percent" in result.applied


def test_bare_number_expands() -> None:
    result = _compile("Số lượng 50 cái")
    assert result.spoken_text == "Số lượng năm mươi cái"
    assert "number_to_words" in result.applied


def test_acronym_and_sku_spell_letter_by_letter() -> None:
    result = _compile("Mã ABC hàng chính hãng")
    assert result.spoken_text == "Mã A B C hàng chính hãng"
    assert "acronym_spelling" in result.applied


def test_punctuation_hyphen_becomes_comma() -> None:
    result = _compile("Em chào cả nhà — mời xem sản phẩm mới")
    assert result.spoken_text == "Em chào cả nhà, mời xem sản phẩm mới"
    assert "punctuation_and_hyphen" in result.applied


def test_mixed_vietnamese_english() -> None:
    result = _compile("Kem ABC giảm 20%, giá 299.000đ")
    assert result.spoken_text == (
        "Kem A B C giảm hai mươi phần trăm, giá hai trăm chín mươi chín nghìn đồng"
    )
    assert set(result.applied) >= {"currency_and_price", "percent", "acronym_spelling"}


def test_unsupported_markup_stripped() -> None:
    result = _compile("Mua ngay tại **shopee** [link](https://x.com)")
    assert "**" not in result.spoken_text
    assert "https" not in result.spoken_text
    assert "strip_markup_and_controls" in result.applied


def test_hidden_control_chars_stripped() -> None:
    result = _compile("Chào​bạn")
    assert result.spoken_text == "Chào bạn"
    assert "strip_markup_and_controls" in result.applied


def test_no_semantic_embellishment() -> None:
    """Compilation never adds words that were not in the display text."""
    result = _compile("Sản phẩm tốt.")
    assert result.spoken_text == "Sản phẩm tốt."
    assert result.applied == ()


def test_plain_text_applies_no_normalizers() -> None:
    result = _compile("Xin chào các bạn, hôm nay mình giới thiệu kem dưỡng ẩm.")
    assert result.spoken_text == "Xin chào các bạn, hôm nay mình giới thiệu kem dưỡng ẩm."
    assert result.applied == ()


@pytest.mark.parametrize(
    "display",
    [
        "Kem ABC chỉ 299.000đ",
        "Giảm 20% hôm nay",
        "Số lượng 50 cái, giá 99đ",
        "Mã SKU-123 hàng chính hãng",
        "Giá 1.299.000 đồng",
        "Em chào cả nhà — mời xem sản phẩm mới",
        "Nhiều chỗ  trống  và  TAB\tở đây",
        "Giảm giá 20% cho 3 món, quà tặng 1 khăn",
        "Xin chào các bạn. Hôm nay mình giới thiệu kem dưỡng ẩm.",
        "50k mỗi cái",
    ],
)
def test_idempotent_normalization(display: str) -> None:
    first = compile_spoken_text(display)
    second = compile_spoken_text(first.spoken_text)
    assert second.spoken_text == first.spoken_text
    # A spoken form is already a spoken form: no normalizers re-apply.
    assert second.applied == ()


def test_expand_vietnamese_number() -> None:
    assert expand_vietnamese_number("299") == "hai trăm chín mươi chín"
    assert expand_vietnamese_number("1299000") == ("một triệu hai trăm chín mươi chín nghìn")
    assert expand_vietnamese_number("12,5") == "mười hai phẩy năm"
    assert expand_vietnamese_number("0") == "không"
    assert expand_vietnamese_number("101") == "một trăm lẻ một"


def test_compiled_script_version_joins_segments_in_order() -> None:
    version = CompiledScriptVersion(
        script_item_id="script_item:abc",
        segment_spoken_texts=[
            "Kem ABC chỉ hai trăm chín mươi chín nghìn đồng.",
            "Giảm hai mươi phần trăm hôm nay.",
        ],
        segment_version_ids=["segment:1", "segment:2"],
        plan_version=3,
    )
    assert version.compiled_spoken_text() == (
        "Kem ABC chỉ hai trăm chín mươi chín nghìn đồng. Giảm hai mươi phần trăm hôm nay."
    )


def test_compiled_script_version_normalizes_trailing_punctuation() -> None:
    version = CompiledScriptVersion(
        script_item_id="script_item:abc",
        segment_spoken_texts=[
            "Đoạn một không có chấm",
            "Đoạn hai có chấm.",
            "Đoạn ba có chấm hỏi?",
        ],
        segment_version_ids=["segment:1", "segment:2", "segment:3"],
    )
    assert version.compiled_spoken_text() == (
        "Đoạn một không có chấm. Đoạn hai có chấm. Đoạn ba có chấm hỏi."
    )


def test_machine_price_sizes_and_paragraphs_compile_to_spoken_units() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    out = compile_spoken_text("Giá 100000.00 VND.\n\nSize XL, XXL và ABC.").spoken_text
    assert out == "Giá một trăm nghìn đồng.\n\nSize XL, XXL và A B C."
    assert compile_spoken_text(out).spoken_text == out  # idempotent, boundaries kept


def test_a_number_before_sentence_punctuation_is_still_spoken() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    assert (
        compile_spoken_text("Cao từ 36 đến 44.").spoken_text
        == "Cao từ ba mươi sáu đến bốn mươi bốn."
    )
    assert compile_spoken_text("Có 2, rồi 3.").spoken_text == "Có hai, rồi ba."


@pytest.mark.parametrize(
    "written, spoken",
    [
        ("Size 36-44.", "Size ba mươi sáu đến bốn mươi bốn."),
        ("Size 36-44 nhé.", "Size ba mươi sáu đến bốn mươi bốn nhé."),
        ("Gọi 0901234567.", "Gọi không chín không một hai ba bốn năm sáu bảy."),
        (
            "Gọi 0901234567 nhé.",
            "Gọi không chín không một hai ba bốn năm sáu bảy nhé.",
        ),
        ("Giá 299.000đ.", "Giá hai trăm chín mươi chín nghìn đồng."),
        ("Giảm 20%.", "Giảm hai mươi phần trăm."),
        ("Nặng 1,5 kg.", "Nặng một phẩy năm ki lô gam."),
    ],
)
def test_ranges_phones_prices_percents_and_decimals(written, spoken) -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    assert compile_spoken_text(written).spoken_text == spoken


@pytest.mark.parametrize(
    "text",
    [
        "Bảo quản nhiệt độ <5 hoặc >40 độ C.",
        "Cao dưới 1m65, 45-60kg: size M.",
        "Giá 299.000đ, giảm 20%.",
        "Size 36-44 nhé.",
        "Gọi 0901234567 nhé.",
        "Dung tích 1,5 lít, nặng 1,5 kg.",
        "Có <b>chữ</b> đậm.",
    ],
)
def test_compile_is_idempotent_and_leaves_no_angle_brackets_for_comparisons(text) -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    once = compile_spoken_text(text).spoken_text
    assert compile_spoken_text(once).spoken_text == once
    if "<b>" not in text:
        assert "<" not in once and ">" not in once


def test_comparison_signs_and_compact_measurements_are_spoken() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    assert (
        compile_spoken_text("Nhiệt độ <5 hoặc >40.").spoken_text
        == "Nhiệt độ dưới năm hoặc trên bốn mươi."
    )
    assert (
        compile_spoken_text("Cao 1m65, 45-60kg.").spoken_text
        == "Cao một mét sáu mươi lăm, bốn mươi lăm đến sáu mươi ki lô gam."
    )


@pytest.mark.parametrize(
    "written, spoken",
    [
        ("Nặng 1,05 kg.", "Nặng một phẩy không năm ki lô gam."),
        ("Nặng 1,5 kg.", "Nặng một phẩy năm ki lô gam."),
        ("Nặng 2,25 kg.", "Nặng hai phẩy hai mươi lăm ki lô gam."),
        ("Giảm 0,05%.", "Giảm không phẩy không năm phần trăm."),
        ("Giảm 0,125%.", "Giảm không phẩy một hai năm phần trăm."),
        ("Khối lượng 1.000 kg.", "Khối lượng một nghìn ki lô gam."),
        (
            "Chỉ áp dụng từ 01-10-2026.",
            "Chỉ áp dụng từ ngày một tháng mười năm hai nghìn không trăm hai mươi sáu.",
        ),
        (
            "Hết hạn 05/10/2026.",
            "Hết hạn ngày năm tháng mười năm hai nghìn không trăm hai mươi sáu.",
        ),
        ("Size 36-44.", "Size ba mươi sáu đến bốn mươi bốn."),
        ("Giá 1000000000000 đồng.", "Giá một nghìn tỷ đồng."),
        ("Giá 1.000.000.000.000đ.", "Giá một nghìn tỷ đồng."),
    ],
)
def test_decimals_units_dates_and_huge_numbers(written, spoken) -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    assert compile_spoken_text(written).spoken_text == spoken


SALES_SENTENCES = [
    "Giá chỉ 299.000đ cho mỗi hộp, giảm 20% hôm nay.",
    "Combo 2 hộp giá 550k, tặng kèm 1 túi vải.",
    "Nặng 1,05 kg, dung tích 1,5 lít, cao 1m65.",
    "Giảm 0,5% cho đơn từ 1.000.000đ.",
    "Cao 1m60-1m70, nặng 55-65kg: size M.",
    "Size 36-44 đều có sẵn nhé.",
    "Gọi 0901234567 hoặc +84901234567 để được tư vấn.",
    "Ưu đãi từ 01-10-2026 đến 15-10-2026.",
    "Chương trình áp dụng ngày 5/10 và 10/10/2026.",
    "Nhiệt độ bảo quản <5 hoặc >40 độ C.",
    "Bảo hành 12 tháng, đổi trả trong 7 ngày.",
    "Dùng 2 lần mỗi ngày, mỗi lần 15 phút.",
    "Hạn dùng 36 tháng kể từ ngày sản xuất.",
    "Khối lượng 1.000 kg mỗi lô, 500g mỗi gói.",
    "Giá 100000.00 VND cho 1 sản phẩm.",
    "Mua 3 tặng 1, tổng 4 sản phẩm.",
    "Đường kính 25cm, dày 5mm, nặng 250g.",
    "Pin 5000mAh dùng được 2 ngày.",
    "Giá 1000000000000 đồng chỉ là ví dụ.",
    "Mã giảm 20k cho đơn đầu tiên.",
    "Livestream lúc 20h hôm nay, 19h30 vào phòng.",
    "Tặng voucher 50.000đ cho 100 khách đầu tiên.",
    "Cân nặng 45-52kg mặc size S, 53-60kg mặc size M.",
    "Chiều cao 1m55 đến 1m62 chọn size M.",
    "Giảm 0,125% phí vận chuyển.",
    "Có 3 màu, 4 size, 5 kiểu dáng.",
    "Tỷ lệ 70% cotton, 30% polyester.",
    "Giá 1,5 triệu hoặc 1.500.000đ.",
    "Từ 2-4 ngày là nhận được hàng.",
    "Thứ 4 hàng tuần giảm 15%.",
    "Khách hàng hài lòng 98,5%.",
    "Hotline 1900 1234 mở cửa 8-22h.",
    "Size XL 80-90kg, XXL 90-100kg.",
    "Giá 299,000đ cho 1 chiếc.",
    "Sản phẩm số 1 năm 2026.",
    "Đơn từ 200k miễn phí ship, tối đa 30k.",
    "Phiên bản 2.0 ra mắt ngày 01/10/2026.",
    "Trọng lượng 0,25kg, rộng 10,5cm.",
    "Giá niêm yết 999.999.999.999đ.",
    "Điện áp 220V, công suất 1500W.",
]


@pytest.mark.parametrize("sentence", SALES_SENTENCES)
def test_compile_never_raises_is_idempotent_and_leaves_no_digits(sentence) -> None:
    import re

    from backend.application.script_authoring.compile import compile_spoken_text

    once = compile_spoken_text(sentence).spoken_text
    assert compile_spoken_text(once).spoken_text == once
    assert not re.search(r"\d", once), once


def test_compile_survives_absurd_digit_strings() -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    for digits in ("9" * 22, "1" + "0" * 40, "7" * 5000):
        assert compile_spoken_text(f"Giá {digits} đồng.").spoken_text


@pytest.mark.parametrize(
    "written, spoken",
    [
        ("Hỗ trợ 24/7.", "Hỗ trợ hai mươi bốn/bảy."),
        ("Dùng 1/2 viên.", "Dùng một/hai viên."),
        ("Mở bán ngày 1/10.", "Mở bán ngày một tháng mười."),
        (
            "Hết hạn 01/10/2026.",
            "Hết hạn ngày một tháng mười năm hai nghìn không trăm hai mươi sáu.",
        ),
        ("Gọi 1900 0123.", "Gọi một chín không không không một hai ba."),
        ("Gọi 1900 1234.", "Gọi một chín không không một hai ba bốn."),
        ("Gọi 1800 6868.", "Gọi một tám không không sáu tám sáu tám."),
        ("Gọi 0901234567.", "Gọi không chín không một hai ba bốn năm sáu bảy."),
    ],
)
def test_bare_slashes_and_spaced_hotlines(written, spoken) -> None:
    from backend.application.script_authoring.compile import compile_spoken_text

    once = compile_spoken_text(written).spoken_text
    assert once == spoken
    assert compile_spoken_text(once).spoken_text == once
