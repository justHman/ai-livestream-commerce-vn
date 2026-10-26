"""Abuse and questions about another item are settled BEFORE any model call or fixed price/size answer."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.application.script_authoring.answer_context import (
    _OTHER_PRODUCT_DEFLECTION,
    answer_scope,
)
from backend.application.script_authoring.approved_speech import (
    ApprovedProduct,
    ApprovedSpeech,
    SpeechRejected,
)

FACTS = ["Áo khoác denim", "Chất liệu denim dày dặn.", "Có ba màu trắng, xanh đậm và đen."]
ABUSE = "ngu nhu bo"  # in the curated patterns (also checked by the release probe)


@pytest.mark.parametrize(
    "question",
    ["quần này giá bao nhiêu", "quan nay gia bao nhieu", "shop có quần không"],
)
def test_a_question_about_another_item_is_answered_with_the_fixed_reply(question: str) -> None:
    assert answer_scope(question, FACTS) == ("other_product", question)


@pytest.mark.parametrize(
    "question", ["áo này giá bao nhiêu", "áo có size L không", "còn hàng không"]
)
def test_a_question_about_this_item_is_answered_normally(question: str) -> None:
    assert answer_scope(question, FACTS) == ("answer", question)


def test_only_abusive_comments_are_skipped() -> None:
    assert answer_scope(ABUSE, FACTS) == ("skip", "")
    assert answer_scope(f"{ABUSE} | {ABUSE}", FACTS) == ("skip", "")


def test_an_abusive_comment_does_not_take_a_valid_question_of_the_cluster_with_it() -> None:
    assert answer_scope(f"{ABUSE} | áo này giá bao nhiêu", FACTS) == (
        "answer",
        "áo này giá bao nhiêu",
    )


def test_a_cluster_mixing_this_item_and_another_is_still_answered() -> None:
    mixed = "áo này giá bao nhiêu | quần này giá bao nhiêu"
    assert answer_scope(mixed, FACTS) == ("answer", mixed)


@pytest.mark.parametrize("question", ["", None])
def test_no_question_is_left_to_the_normal_path(question) -> None:
    assert answer_scope(question, FACTS) == ("answer", question or "")


def test_a_question_without_a_known_item_noun_is_not_called_another_item() -> None:
    assert answer_scope("shop ơi cho hỏi", ["Tên", "Chất liệu dày."]) == (
        "answer",
        "shop ơi cho hỏi",
    )


class _Llm:
    name = "controlled"

    def __init__(self) -> None:
        self.calls = 0

    def stream_chunks(self, request, **kwargs):
        self.calls += 1
        yield SimpleNamespace(text="Áo khoác denim có ba màu trắng, xanh đậm và đen nhé.")


def _speech(monkeypatch: pytest.MonkeyPatch) -> ApprovedSpeech:
    facts = {
        "product_name": "Áo khoác denim",
        "allowed_claims": ["Có ba màu trắng, xanh đậm và đen."],
        "prices": ["180000.00 VND"],
    }
    product = ApprovedProduct(
        "p1", "v1", "Áo khoác denim có ba màu nhé.", "h", json.dumps(facts, ensure_ascii=False)
    )
    envelope = SimpleNamespace(product=lambda pid: product, products=(product,))

    async def resolve(self, session_id):
        return envelope

    async def revalidate(self, speech, live=None):
        return None

    monkeypatch.setattr(ApprovedSpeech, "resolve", resolve)
    monkeypatch.setattr(ApprovedSpeech, "revalidate", revalidate)
    return ApprovedSpeech(None, lambda: None)


async def _prepare(speech: ApprovedSpeech, llm: _Llm, question: str):
    return await speech.prepare(
        "s1",
        question,
        llm=llm,
        generate=True,
        product_id="p1",
        route="director",
        question_text=question,
    )


@pytest.mark.asyncio
async def test_an_abusive_comment_is_rejected_without_a_model_call(monkeypatch) -> None:
    llm = _Llm()
    with pytest.raises(SpeechRejected) as rejected:
        await _prepare(_speech(monkeypatch), llm, ABUSE)
    assert (rejected.value.code, llm.calls) == ("safety_filtered", 0)


@pytest.mark.asyncio
async def test_a_question_about_another_item_gets_the_fixed_reply_without_a_model_call(
    monkeypatch,
) -> None:
    llm = _Llm()
    spoken = await _prepare(_speech(monkeypatch), llm, "quần này giá bao nhiêu")
    assert (spoken.text, llm.calls) == (_OTHER_PRODUCT_DEFLECTION, 0)


@pytest.mark.asyncio
async def test_a_question_about_this_item_never_gets_the_other_item_reply(monkeypatch) -> None:
    spoken = await _prepare(_speech(monkeypatch), _Llm(), "áo có những màu nào")
    assert spoken.text != _OTHER_PRODUCT_DEFLECTION
