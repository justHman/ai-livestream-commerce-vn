"""Asymmetric admission/speech policy, probed through authenticated ASGI HTTP."""

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated
from backend.application.script_authoring.answer_context import deflection_for, question_kind
from backend.application.script_authoring.approved_speech import SpeechRejected
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare
from .test_comment_guard_round2 import variants
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory

BENIGN = variants(
    ["cái đồ cho bé giá bao nhiêu?"]
    + [
        f"{goods} cho {recipient} {request}"
        for goods in ("cái đồ", "đồ", "bộ đồ", "mẫu đồ")
        for recipient in ("bé", "mẹ", "chồng", "vợ", "con")
        for request in ("giá bao nhiêu?", "có size M không?", "thanh toán COD được không?")
    ]
)
SALES = variants(
    ["chị có còn bán áo không?", "chị có đồ ăn thanh toán COD không?"]
    + [
        f"{host} có {request} không?"
        for host in ("chị", "anh", "em", "cô", "bạn")
        for request in (
            "còn bán áo",
            "còn hàng",
            "còn bán đồ ăn",
            "đồ ăn thanh toán COD",
            "đồ ăn giá bao nhiêu",
            "đồ cho bé giao hàng",
            "đồ ngủ màu xanh",
            "áo chất liệu cotton",
            "bộ máy bảo hành",
            "đồ khi nào ship",
        )
    ]
)
PERSONAL = variants(
    ["chị có bao nhiêu người yêu?", "chị có chồng giao hàng không?"]
    + [
        f"{host} có {predicate}{ending}"
        for host in ("chị", "anh", "em", "cô", "bạn")
        for predicate in (
            "bao nhiêu người yêu",
            "chồng giao hàng",
            "vợ mua áo",
            "con đặt hàng",
            "bạn trai thanh toán COD",
            "bạn gái bán hàng",
            "thích mua sắm",
            "từng giao hàng",
            "định mua áo",
            "bao nhiêu tuổi",
        )
        for ending in ("?", " vậy?")
    ]
)
ABUSE = variants(
    ["du ma may", "do khon nan", "do . khon . nan"]
    + [
        f"{prefix}{phrase}{suffix}"
        for prefix in ("", "đồ ", "cái đồ ", "mày là ")
        for phrase in ("khốn nạn", "khốn kiếp", "đụ má mày")
        for suffix in ("", "!", " quá")
    ]
)
ABUSE = list(dict.fromkeys(ABUSE + [s.replace(" ", " . ") for s in ABUSE]))

# ASCII vo + a sales clause is ambiguous; only explicit spouse contexts veto fallback.
ASCII_VO_SALES = [text for text in PERSONAL if " co vo " in text]
PERSONAL = [text for text in PERSONAL if text not in ASCII_VO_SALES]
SALES += ASCII_VO_SALES


def test_corpus_minimums():
    for group in (BENIGN, SALES, PERSONAL, ABUSE):
        assert len(set(group)) >= 84  # reviewer literals plus >=80 new cases per group


@pytest.mark.asyncio
@pytest.mark.parametrize("text", BENIGN + SALES)
async def test_buyer_reaches_runtime_and_has_commerce_fallback(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["accepted"] == 1, result
    coordinator.assert_called_once()
    reducer.assert_called_once()
    assert coordinator.call_args.args[1] == text
    assert deflection_for(text, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", PERSONAL)
async def test_personal_is_admitted_but_never_spoken(case_factory, monkeypatch, text):
    monkeypatch.setenv("RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED", "1")
    case = await case_factory()
    case.llm.output = ""  # Silence only after both approved-fact paths return empty.
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["accepted"] == 1, result
    coordinator.assert_called_once()
    reducer.assert_called_once()
    assert coordinator.call_args.args[1] == text
    assert question_kind(text, []) == "not_answerable_non_commerce"
    with pytest.raises(SpeechRejected, match="not_answerable_non_commerce"):
        deflection_for(text, [])
    await case.d.reducer.run_once(case.sid, reducer.call_args.kwargs["comment"].ts)
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    turn.cluster_member_ids = (reducer.call_args.kwargs["comment"].comment_id,)
    turn.source_event_ids = ("event-1",)
    await prepare(case, turn)
    assert turn.is_cancelled and not turn.prepared_script
    assert case.llm.started.is_set() and not case.tts.calls
    assert not case.d.director.get_session(case.sid).director.state.topic_cooldown_until
    await case.d.coordinator.live_follow.flush(case.sid)
    assert any(
        e["type"] == "live_follow.comment.skipped"
        and e["final"]
        and e["reason_code"] == "not_answerable_non_commerce"
        for e in case.events
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ABUSE)
async def test_strong_phrases_block_before_runtime(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["events"][0]["safety"]["accepted"] is False, result
    assert result["events"][0]["safety"]["resource_versions"]["toxicity"] == "6"
    coordinator.assert_not_called()
    reducer.assert_not_called()
    assert not case.llm.started.is_set() and not case.tts.calls


@pytest.mark.parametrize(
    "text",
    [
        "do cho",
        "cai do cho",
        "may la do cho",
        "cai do cho be gia bao nhieu?",
        "do c.h.o",
        "do c h o",
        "cai do c_h_o",
        "may la do ch.o",
    ],
)
def test_ascii_cho_never_infers_abusive_accent(text):
    assert not match_curated(text, load_all_curated_patterns())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", variants(["chị có còn bán áo không?", "chị có đồ ăn thanh toán COD không?"])
)
async def test_missing_approved_answer_can_prepare_commerce_fallback(case_factory, text):
    from backend.application.script_authoring.answer_context import is_deflection

    case = await case_factory()
    case.llm.output = ""  # No approved answer from either semantic path.
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    await prepare(case, turn)
    assert not turn.is_cancelled
    assert is_deflection(turn.prepared_script)


@pytest.mark.parametrize(
    "text",
    variants(
        [
            "có chống nước không?",
            "chị có chống nước không?",
            "chị có còn bán áo không?",
            "chị có đồ ăn thanh toán COD không?",
        ]
    ),
)
def test_stock_food_and_protection_are_not_private_life(text):
    assert question_kind(text, []) in {"commerce", "current_product", "other_product"}
    assert deflection_for(text, [])
