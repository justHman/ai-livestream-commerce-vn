"""Fallback eligibility, neutral wording and dispatch-time session throttling."""

import pytest
import time

from approved_speech_helpers import Source
from backend.application.script_authoring.answer_context import deflection_for, is_deflection
from backend.application.script_authoring.approved_speech import SpeechRejected
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory
FACTS = ["Áo khoác denim", "Có size M, L."]


@pytest.mark.parametrize(
    "text", ["asdfgh", "zzz", "hahaha", "đm", "thủ đô nước Pháp là gì?", "what is blorpt", "ok?"]
)
def test_non_commerce_has_no_fallback(text):
    with pytest.raises(SpeechRejected):
        deflection_for(text, FACTS)


@pytest.mark.parametrize("text", ["quần jean còn không", "có giày không", "giày size 40"])
def test_other_goods_fallback_is_distinct_and_never_echoes_comment(text):
    said = deflection_for(text, FACTS)
    assert "mặt hàng này" in said
    assert is_deflection(said)
    assert text not in said
    assert "rất cụ thể" not in said and "rất chi tiết" not in said


def test_current_and_uncertain_product_fallbacks_are_neutral():
    current = deflection_for("áo này có chống nước không", FACTS)
    neutral = deflection_for("có chống nước không", FACTS)
    assert "sản phẩm đang giới thiệu" in current
    for text in (current, neutral):
        assert is_deflection(text) and "nhắn tin cho shop" in text
        assert "rất cụ thể" not in text and "rất chi tiết" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["asdfgh", "zzz", "thủ đô nước Pháp là gì?", "what is blorpt"])
async def test_non_commerce_skips_before_reducer(case_factory, monkeypatch, text):
    monkeypatch.setenv("RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED", "1")
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["rejected"] == 1
    assert result["events"][0]["reason"] in {"non_question", "not_answerable_non_commerce"}
    coordinator.assert_not_called()
    reducer.assert_not_called()
    await case.d.coordinator.live_follow.flush(case.sid)
    skipped = [e for e in case.events if e["type"] == "live_follow.comment.skipped"]
    assert skipped and skipped[-1]["final"]
    assert not case.tts.calls and not case.llm.started.is_set()


@pytest.mark.asyncio
async def test_other_goods_never_receives_a_current_product_fact(case_factory):
    source = Source()
    source.set.brief.product_facts["product-1"]["product_name"] = "Áo khoác denim"
    case = await case_factory(source=source)
    turn = decision(case, generated=True)
    turn.cluster_members = ("giày size 40",)
    await prepare(case, turn)
    assert is_deflection(turn.prepared_script)
    assert "mặt hàng này" in turn.prepared_script
    assert not case.llm.started.is_set()


@pytest.mark.asyncio
async def test_approved_product_name_is_available_at_intake(case_factory, monkeypatch):
    source = Source()
    source.set.brief.product_facts["product-1"]["product_name"] = "Máy lọc nước"
    case = await case_factory(source=source)
    await canonical(case)
    coordinator, _ = probes(case, monkeypatch)
    result = await post(case, "máy lọc nước có bền không?")
    assert result["rejected"] == 0
    coordinator.assert_called_once()


@pytest.mark.asyncio
async def test_dispatch_limit_covers_prefetch_cluster_and_resets_after_90_seconds(
    case_factory, monkeypatch
):
    monkeypatch.setenv("RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED", "1")
    case = await case_factory()
    case.llm.output = ""  # forces the validated fallback
    coordinator = case.d.coordinator
    now = [100.0]
    coordinator._monotonic = lambda: now[0]
    ds = case.d.director.get_session(case.sid)
    coordinator.live_follow.sync_membership(
        case.sid,
        {f"c{i}": (f"cl-{i}", "unknown") for i in range(1, 6)},
        lambda cid: (f"evt-{cid}", {}, time.time()),
        now=time.time(),
        horizon_sec=75.0,
        force=True,
    )

    async def fallback(ids):
        turn = decision(case, generated=True)
        turn.cluster_members = ("áo này có chống nước không",)
        turn.cluster_member_ids = ids
        await prepare(case, turn)
        assert is_deflection(turn.prepared_script)
        return turn

    first = await fallback(("c1", "c2"))
    second = await fallback(("c3",))  # both prepared before the first one plays
    await coordinator._maybe_speak(case.sid, first)
    before = list(case.tts.calls)
    cooldown = dict(ds.director.state.topic_cooldown_until)
    await coordinator._maybe_speak(case.sid, second)
    assert case.tts.calls == before
    assert ds.director.state.topic_cooldown_until == cooldown
    await coordinator.live_follow.flush(case.sid)
    skipped = [e for e in case.events if e["type"] == "live_follow.comment.skipped"]
    assert any(e["reason_code"] == "deflection_rate_limited" and e["final"] for e in skipped)
    assert "c3" not in ds.director.state.answered_comments
    assert "c3" in ds.director.state.skipped_comments
    assert ds.director.state.answered_comments >= {"c1", "c2"}
    now[0] = 189.99
    third = await fallback(("c4",))
    await coordinator._maybe_speak(case.sid, third)
    assert case.tts.calls == before
    now[0] = 190.0
    fourth = await fallback(("c5",))
    await coordinator._maybe_speak(case.sid, fourth)
    assert len(case.tts.calls) > len(before)


def test_deflection_interval_env_is_validated(monkeypatch):
    from backend.application.director.config import StreamConfig

    assert StreamConfig().deflection_interval_sec == 90.0
    monkeypatch.setenv("DIRECTOR_DEFLECTION_INTERVAL_SEC", "120")
    assert StreamConfig.from_env().deflection_interval_sec == 120.0
    for bad in ("oops", "nan", "-5", "0"):
        monkeypatch.setenv("DIRECTOR_DEFLECTION_INTERVAL_SEC", bad)
        assert StreamConfig.from_env().deflection_interval_sec == 90.0
