"""Abuse and questions about another item are settled BEFORE any model call or fixed price/size answer."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.application.script_authoring.answer_context import answer_scope
from backend.application.script_authoring.approved_speech import (
    ApprovedProduct,
    ApprovedSpeech,
    SpeechRejected,
)

FACTS = ["Áo khoác denim", "Chất liệu denim dày dặn.", "Có ba màu trắng, xanh đậm và đen."]
NAMES = FACTS[:1]
ABUSE = "ngu nhu bo"  # in the curated patterns (also checked by the release probe)


@pytest.mark.parametrize(
    "question",
    ["quần này giá bao nhiêu", "quan nay gia bao nhieu", "shop có quần không"],
)
def test_a_question_about_another_item_is_answered_with_the_fixed_reply(question: str) -> None:
    assert answer_scope(question, FACTS, NAMES) == ("other_product", question)


@pytest.mark.parametrize(
    "question", ["áo này giá bao nhiêu", "áo có size L không", "còn hàng không"]
)
def test_a_question_about_this_item_is_answered_normally(question: str) -> None:
    assert answer_scope(question, FACTS, NAMES) == ("answer", question)


def test_only_abusive_comments_are_skipped() -> None:
    assert answer_scope(ABUSE, FACTS, NAMES) == ("skip", "")
    assert answer_scope(f"{ABUSE} | {ABUSE}", FACTS, NAMES) == ("skip", "")


def test_an_abusive_comment_does_not_take_a_valid_question_of_the_cluster_with_it() -> None:
    assert answer_scope(f"{ABUSE} | áo này giá bao nhiêu", FACTS, NAMES) == (
        "answer",
        "áo này giá bao nhiêu",
    )


def test_a_cluster_mixing_this_item_and_another_is_still_answered() -> None:
    mixed = "áo này giá bao nhiêu | quần này giá bao nhiêu"
    assert answer_scope(mixed, FACTS, NAMES) == ("answer", mixed)


@pytest.mark.parametrize("question", ["", None])
def test_no_question_is_left_to_the_normal_path(question) -> None:
    assert answer_scope(question, FACTS, NAMES) == ("answer", question or "")


def test_a_question_without_a_known_item_noun_is_not_called_another_item() -> None:
    assert answer_scope("shop ơi cho hỏi", ["Tên", "Chất liệu dày."], ["Tên"]) == (
        "answer",
        "shop ơi cho hỏi",
    )


def test_a_detail_question_names_the_sold_item_so_it_is_never_another_item() -> None:
    # "túi" is not in the facts, but "áo" is the sold item: the color question must not be lost
    for question in ("áo có túi không", "áo có túi không và có những màu nào"):
        assert answer_scope(question, FACTS, NAMES) == ("answer", question)


def test_a_fact_that_mentions_trousers_does_not_make_trousers_the_sold_item() -> None:
    facts = [*FACTS, "Dễ phối cùng quần jeans."]
    question = "quần này giá bao nhiêu"
    assert answer_scope(question, facts, NAMES) == ("other_product", question)


def test_a_sold_item_without_a_known_noun_is_never_called_another_item() -> None:
    names = ["Kem dưỡng ABC"]
    facts = [*names, "Dưỡng ẩm."]
    question = "quần này giá bao nhiêu"
    assert answer_scope(question, facts, names) == ("answer", question)


class _Llm:
    name = "controlled"

    def __init__(self) -> None:
        self.calls = 0

    def stream_chunks(self, request, **kwargs):
        self.calls += 1
        yield SimpleNamespace(text="Áo khoác denim có ba màu trắng, xanh đậm và đen nhé.")


def _speech(monkeypatch: pytest.MonkeyPatch, extra_claims: tuple[str, ...] = ()) -> ApprovedSpeech:
    facts = {
        "product_name": "Áo khoác denim",
        "allowed_claims": ["Có ba màu trắng, xanh đậm và đen.", *extra_claims],
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
async def test_a_question_about_another_item_is_off_topic_without_a_model_call(
    monkeypatch,
) -> None:
    llm = _Llm()
    with pytest.raises(SpeechRejected) as rejected:
        await _prepare(_speech(monkeypatch), llm, "quần này giá bao nhiêu")
    assert (rejected.value.code, llm.calls) == ("not_answerable_non_commerce", 0)


@pytest.mark.asyncio
async def test_a_question_about_this_item_is_not_skipped_as_off_topic(monkeypatch) -> None:
    spoken = await _prepare(_speech(monkeypatch), _Llm(), "áo có những màu nào")
    assert spoken.text


@pytest.mark.asyncio
async def test_trousers_in_a_fact_are_still_off_topic_without_a_model_call(monkeypatch) -> None:
    llm = _Llm()
    speech = _speech(monkeypatch, extra_claims=("Dễ phối cùng quần jeans.",))
    with pytest.raises(SpeechRejected) as rejected:
        await _prepare(speech, llm, "quần này giá bao nhiêu")
    assert (rejected.value.code, llm.calls) == ("not_answerable_non_commerce", 0)
