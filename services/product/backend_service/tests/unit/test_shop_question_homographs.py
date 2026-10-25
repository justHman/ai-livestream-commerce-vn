"""Accent folding must not invent shopping intent from family or weather chat."""

import pytest

from approved_speech_helpers import CLAIM
from backend.application.script_authoring.answer_context import question_kind
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare

case_factory = active.case_factory


@pytest.mark.parametrize(
    "text",
    ["trời mưa ngày mai không?", "nhóm máu gì?", "quốc gia nào đông dân nhất?"],
)
def test_unrelated_homographs_do_not_authorize_a_shop_fallback(text):
    from backend.application.script_authoring.answer_context import deflection_for
    from backend.application.script_authoring.approved_speech import SpeechRejected

    assert question_kind(text, ["Áo khoác denim"]) == "unclassified"
    with pytest.raises(SpeechRejected):
        deflection_for(text, ["Áo khoác denim"])


def test_family_question_to_host_is_silent():
    assert (
        question_kind("anh có gia đình chưa?", ["Áo khoác denim"]) == "not_answerable_non_commerce"
    )


def test_weather_words_do_not_hide_a_real_product_question():
    assert question_kind("trời mưa áo này có chống nước không?", ["Áo khoác denim"]) == (
        "current_product"
    )


@pytest.mark.asyncio
async def test_non_commerce_cannot_receive_even_a_valid_natural_answer(case_factory):
    case = await case_factory()
    case.llm.output = CLAIM
    turn = decision(case, generated=True)
    turn.cluster_members = ("asdfgh",)
    turn.cluster_member_ids = ("nonsense",)
    await prepare(case, turn)
    assert turn.is_cancelled
    assert not case.llm.started.is_set()
    assert not case.tts.calls
    state = case.d.director.get_session(case.sid).director.state
    assert not state.topic_cooldown_until
    assert "nonsense" in state.skipped_comments


@pytest.mark.asyncio
async def test_shop_answers_remain_eligible_during_deflection_interval(case_factory):
    case = await case_factory()
    coordinator = case.d.coordinator
    coordinator._monotonic = lambda: 100.0
    case.llm.output = ""
    fallback = decision(case, generated=True)
    fallback.cluster_members = ("bảo hành bao lâu?",)
    await prepare(case, fallback)
    await coordinator._maybe_speak(case.sid, fallback)
    assert case.tts.calls
    before = len(case.tts.calls)
    case.llm.output = CLAIM
    answer = decision(case, generated=True)
    answer.cluster_members = ("shop giao hàng không?",)
    await prepare(case, answer)
    await coordinator._maybe_speak(case.sid, answer)
    assert len(case.tts.calls) > before
