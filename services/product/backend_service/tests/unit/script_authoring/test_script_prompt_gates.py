"""Intake prompt contract, stock-filler gate, recap-copy gate."""

from __future__ import annotations


import pytest

from backend.application.product_intake.extractor import ALLOWED_TYPES, build_prompt
from backend.application.script_authoring.generation.ordered_units import (
    check_unit_text,
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
