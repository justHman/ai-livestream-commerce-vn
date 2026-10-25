"""Fallback classification must never preempt approved-fact answering."""

import pytest

from backend.application.script_authoring.answer_context import (
    deflection_for,
    is_deflection,
    question_kind,
)
from backend.application.script_authoring.approved_speech import SpeechRejected
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare
from .test_comment_guard_round2 import variants
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory

SALES = variants(
    ["chị bán bộ đồ ngủ không?", "chị bán váy không?", "cho em xem dao bếp"]
    + [
        f"{request} {goods}{ending}"
        for request in ("chị bán", "cho em xem", "xem hàng", "chị có")
        for goods in ("váy", "dao bếp", "vỏ gối", "bộ đồ ngủ", "đồ ăn", "quần", "giày")
        for ending in (" không?", " màu xanh không?")
    ]
)
PRODUCTS = variants(
    ["chị có con dao giá bao nhiêu?", "chị có vỏ gối màu xanh không?"]
    + [
        f"{host} có {goods} {request}?"
        for host in ("chị", "chị chủ", "chủ shop", "admin")
        for goods in ("con dao", "con búp bê", "con gấu bông", "vỏ gối", "vỏ chăn")
        for request in ("không", "màu xanh không", "giá bao nhiêu")
    ]
)
PERSONAL = variants(
    ["chị chủ có chồng bán áo không?", "chị chủ có người yêu chạy ship không?"]
    + [
        f"{host} có {private} {sales} không?"
        for host in ("chị chủ", "chủ shop", "admin")
        for private in ("chồng", "vợ", "người yêu", "bạn trai", "bạn gái", "con cái")
        for sales in ("bán áo", "chạy ship", "mua áo", "bán váy")
    ]
)
# ASCII vo followed by a sales clause is ambiguous, so it cannot veto fallback.
AMBIGUOUS = [text for text in PERSONAL if " co vo " in text]
PERSONAL = [text for text in PERSONAL if text not in AMBIGUOUS]
PRODUCTS += AMBIGUOUS


def test_corpus_minimums():
    for group in (SALES, PRODUCTS, PERSONAL):
        assert len(set(group)) >= 104


@pytest.mark.parametrize("text", SALES + PRODUCTS)
def test_commerce_fallback(text):
    assert deflection_for(text, [])


@pytest.mark.parametrize("text", PERSONAL + variants(["chị có con không?", "chị có mấy con?"]))
def test_private_fallback_veto(text):
    assert question_kind(text, []) == "not_answerable_non_commerce"
    with pytest.raises(SpeechRejected, match="not_answerable_non_commerce"):
        deflection_for(text, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", SALES + PRODUCTS + PERSONAL + ["what is blorpt?"])
@pytest.mark.parametrize("answered", [True, False])
async def test_http_ingestion_prepare_dispatch(case_factory, monkeypatch, text, answered):
    monkeypatch.setenv("RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED", "1")
    case = await case_factory()
    if not answered:
        case.llm.output = ""
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["accepted"] == 1
    coordinator.assert_called_once()
    reducer.assert_called_once()
    await case.d.reducer.run_once(case.sid, reducer.call_args.kwargs["comment"].ts)
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    turn.cluster_member_ids = (reducer.call_args.kwargs["comment"].comment_id,)
    turn.source_event_ids = ("event-1",)
    await prepare(case, turn)
    assert case.llm.started.is_set()  # Classification cannot cancel before answering.
    silent = not answered and (text in PERSONAL or text == "what is blorpt?")
    assert turn.is_cancelled == silent
    if silent:
        assert not turn.prepared_script and not case.tts.calls
        assert not case.d.director.get_session(case.sid).director.state.topic_cooldown_until
        await case.d.coordinator.live_follow.flush(case.sid)
        assert any(
            e["type"] == "live_follow.comment.skipped"
            and e["final"]
            and e["reason_code"] == "not_answerable_non_commerce"
            for e in case.events
        )
    else:
        assert turn.prepared_script
        assert is_deflection(turn.prepared_script) == (not answered)
        assert await case.d.coordinator._maybe_speak(case.sid, turn)
        assert case.tts.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,commerce",
    [(SALES[0], True), (PRODUCTS[0], True), (PERSONAL[0], False), ("what is blorpt?", False)],
)
async def test_invalid_model_answer_only_allows_commerce_fallback(case_factory, text, commerce):
    case = await case_factory()
    case.llm.output = "Bảo hành trọn đời miễn phí."
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    await prepare(case, turn)
    assert case.llm.started.is_set()
    assert turn.is_cancelled == (not commerce)
    if commerce:
        assert is_deflection(turn.prepared_script)
        assert await case.d.coordinator._maybe_speak(case.sid, turn)
        assert case.tts.calls
    else:
        assert not turn.prepared_script and not case.tts.calls


@pytest.mark.parametrize(
    "text,private",
    [
        ("chị có vợ bán áo không?", True),
        ("chi co vo chong ban ao khong?", True),
        ("chi co vo khong?", True),
        ("chi co vo goi khong?", False),
        ("chị có vỏ gối không?", False),
        ("chị có con búp bê không?", False),
        ("bạn có hiểu không?", False),
    ],
)
def test_narrow_privacy_boundaries(text, private):
    from backend.application.script_authoring.answer_context import is_off_topic

    assert is_off_topic(text, []) == private
