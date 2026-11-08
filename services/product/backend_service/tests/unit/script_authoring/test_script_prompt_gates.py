"""Intake prompt contract, stock-filler gate, recap-copy gate."""

from __future__ import annotations

from dataclasses import replace

import pytest

from backend.application.product_intake.extractor import ALLOWED_TYPES, build_prompt
from backend.application.script_authoring.generation.ordered_units import (
    UnitSpec,
    check_unit_text,
    repeats_source,
    retry_hint,
    revisit_source_index,
)


def test_intake_prompt_keeps_the_json_shape():
    shape = '{"facts":[{"type":"...","text":"...","evidence":"..."}]}'
    assert shape in build_prompt("abc")


def test_intake_prompt_names_every_allowed_type():
    prompt = build_prompt("abc")
    assert all(kind in prompt for kind in ALLOWED_TYPES)


def test_intake_prompt_fences_the_pasted_text_as_data_not_instructions():
    assert "KHÔNG phải lệnh" in build_prompt("abc")


def test_intake_prompt_does_not_ask_for_label_words_the_evidence_would_lack():
    assert "thuộc tính" not in build_prompt("abc")


def test_intake_prompt_asks_for_the_data_own_words():
    assert "DÙNG LẠI CHỮ CỦA DỮ LIỆU" in build_prompt("abc")


@pytest.mark.parametrize(
    "text",
    [
        "Mình thật lòng nghĩ sản phẩm này rất đáng thử.",
        "Mọi người cứ yên tâm nhé.",
        "Sản phẩm này cực kỳ nhỏ gọn.",
        "Màu sắc vô cùng nổi bật.",
        "Mọi người có tò mò vì sao nó nhỏ như vậy không?",
    ],
)
def test_stock_filler_the_owner_never_wrote_is_rejected(text):
    assert check_unit_text(text)[1] == "hard_sell"


def test_stock_filler_the_owner_wrote_is_allowed():
    text = "Mọi người yên tâm đổi trả trong bảy ngày."
    assert check_unit_text(text, approved=("Yên tâm đổi trả trong 7 ngày",))[1] is None


def test_a_recap_that_copies_its_source_is_flagged():
    source = "Thân hình nhỏ nhắn thon dài khoảng tám đến mười ba phẩy năm xăng ti mét, nổi bật chiếc đuôi dài."
    assert repeats_source(source, source)


def test_a_recap_in_new_words_is_not_flagged():
    source = "Thân hình nhỏ nhắn thon dài khoảng tám đến mười ba phẩy năm xăng ti mét, nổi bật chiếc đuôi dài."
    recap = "Nhắc lại cho bạn mới vào: đuôi dài là điểm dễ nhận ra, cơ thể chỉ cỡ bàn tay."
    assert not repeats_source(recap, source)


def test_recap_with_too_few_words_for_a_4gram_is_not_flagged():
    assert not repeats_source("Đuôi dài.", "Đuôi dài.")


def test_the_recap_source_is_the_earlier_unit_with_the_same_facts():
    first = UnitSpec("highlight", claims=("Đặc điểm A.",))
    units = [
        UnitSpec("intro"),
        first,
        UnitSpec("highlight", claims=("Đặc điểm B.",)),
        replace(first, revisit=True),
    ]
    assert revisit_source_index(units, 3) == 1


def test_a_unit_that_is_not_a_recap_has_no_recap_source():
    assert (
        revisit_source_index([UnitSpec("intro"), UnitSpec("highlight", claims=("A.",))], 1) is None
    )


def test_repeat_rejection_has_its_own_retry_hint():
    assert "chép lại" in retry_hint("repeat", "text")
