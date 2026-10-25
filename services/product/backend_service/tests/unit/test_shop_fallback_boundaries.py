"""Positive commerce signals must survive; incidental fact words are not intent."""

import pytest

from approved_speech_helpers import Source
from backend.application.script_authoring.answer_context import question_kind
from . import test_approved_speech_active as active
from .test_safety_gate_active import canonical, post

case_factory = active.case_factory


@pytest.mark.parametrize(
    "question",
    [
        "còn hàng",
        "chất liệu",
        "chính sách đổi trả",
        "khuyến mãi",
        "bảo hành",
        "chính sách",
        "bao nhiêu tiền",
    ],
)
def test_short_shop_queries_remain_eligible(question):
    assert question_kind(question, []) in {"commerce", "current_product"}


@pytest.mark.parametrize("facts", [["Áo khoác chống nước"], ["Máy lọc nước nhập từ Pháp"]])
def test_incidental_fact_word_does_not_make_an_unrelated_question_commercial(facts):
    assert question_kind("thủ đô nước Pháp là gì?", facts) == "unclassified"


def test_explicit_approved_product_phrase_is_a_commerce_signal():
    assert question_kind("máy lọc nước có bền không?", ["Máy lọc nước"]) == "commerce"


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("chị thích ăn gì vậy ạ?", "not_answerable_non_commerce"),
        ("ngày mai thời tiết thế nào vậy?", "unclassified"),
    ],
)
def test_discourse_particle_does_not_become_a_skirt_question(question, expected):
    assert question_kind(question, ["Áo khoác denim"]) == expected


@pytest.mark.asyncio
async def test_intake_uses_approved_product_context_for_unlisted_product_types(case_factory):
    source = Source()
    source.set.brief.product_facts["product-1"]["product_name"] = "Máy lọc nước"
    case = await case_factory(source=source)
    await canonical(case)
    assert (await post(case, "máy lọc nước có bền không?"))["accepted"] == 1
