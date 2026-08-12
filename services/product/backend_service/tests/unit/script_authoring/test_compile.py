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
    assert expand_vietnamese_number("1299000") == (
        "một triệu hai trăm chín mươi chín nghìn"
    )
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
        "Kem ABC chỉ hai trăm chín mươi chín nghìn đồng. "
        "Giảm hai mươi phần trăm hôm nay."
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
