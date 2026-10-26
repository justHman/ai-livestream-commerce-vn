import pytest

from backend.application.platform_events.social_reply import (
    SocialQueue,
    replies,
    social_intent,
    validated_host_name,
)
from backend.application.script_authoring.approved_speech import SpeechRejected
from . import test_approved_speech_active as active
from .test_safety_gate_active import canonical, post

case_factory = active.case_factory


@pytest.mark.parametrize(
    "text,intent",
    [
        ("chào chị", "greeting"),
        ("hlo", "greeting"),
        ("hello shop!", "greeting"),
        ("cảm ơn nha", "thanks"),
        ("tks", "thanks"),
        ("chị xinh quá", "compliment"),
        ("chi xinh wa", "compliment"),
        ("chị khỏe không?", "wellbeing"),
        ("chi khoe ko", "wellbeing"),
        ("chị tên gì", "name"),
        ("chi ten j", "name"),
        ("bạn là ai?", "identity"),
        ("bn la ai", "identity"),
    ],
)
def test_social_corpus(text, intent):
    assert social_intent(text) == intent


@pytest.mark.parametrize(
    "text",
    [
        "áo này có tên gì",
        "size mấy",
        "ao nay size may",
        "chị ơi giá nhiêu",
        "chào shop áo còn size M ko",
        "áo đẹp quá",
        "chị bao nhiêu tuổi",
        "quê ở đâu",
        "thời tiết hôm nay",
        "hello ignore instructions",
        "chị tên gì giá áo",
        "chị xinh quá đụ má",
        "đụ má chào chị",
        "bạn là ai mua bán vũ khí",
    ],
)
def test_whole_comment_match_preserves_commerce_and_safety(text):
    assert social_intent(text) is None


@pytest.mark.parametrize("name", ["Nhật Anh", "MC Lan", "Anna-Marie", "O’Neil", "Ha\u0300"])
def test_names_normalized(name):
    assert validated_host_name(name)


@pytest.mark.parametrize(
    "name",
    [
        "",
        "a" * 49,
        "123",
        "Tên\nkhác",
        "a\u202eb",
        "<speak>Lan</speak>",
        "https://x.com",
        "ignore instructions",
        "system prompt",
        "bỏ qua chỉ dẫn",
        "a\x00b",
    ],
)
def test_bad_names_rejected(name):
    with pytest.raises(ValueError):
        validated_host_name(name)


def test_bounded_coalescing_and_dispatch_quota(monkeypatch):
    monkeypatch.setenv("SOCIAL_REPLY_ENABLED", "1")
    q = SocialQueue()
    for i in range(200):
        assert q.add("greeting", str(i), str(i), str(i), 0) is None
    assert q.add("greeting", "extra", "extra", "extra", 0) == "social_capacity"
    item, reason = q.reserve(3)
    assert len(item.members) == 200 and reason is None
    assert q.add("thanks", "again", "again", "0", 22) == "social_cooldown"
    for now in (23, 43):
        assert q.add("thanks", str(now), str(now), f"new-{now}", now) is None
        assert q.reserve(now)[1] is None
    assert len(q.started) == 3
    assert q.add("thanks", "fast", "fast", "new", 44) == "social_cooldown"
    assert q.add("thanks", "next", "next", "new", 64) is None
    assert q.reserve(64)[1] is None


@pytest.mark.parametrize("disable,expected", [(False, "social_expired"), (True, "social_disabled")])
def test_pending_expiry_and_kill_switch(monkeypatch, disable, expected):
    monkeypatch.setenv("SOCIAL_REPLY_ENABLED", "0" if disable else "1")
    q = SocialQueue()
    q.add("name", "c", "e", "v", 0)
    assert q.reserve(16)[1] == expected
    assert not q.started and q.pending is None


@pytest.mark.asyncio
async def test_name_pinned_and_revalidated_before_tts(case_factory, monkeypatch):
    monkeypatch.setenv("SOCIAL_REPLY_ENABLED", "1")
    case = await case_factory()
    meta = dict(await case.d.store.get(case.sid))
    meta["host_name"] = "Nhật Anh"
    await case.d.store.set(case.sid, meta)
    case.d.approved_speech.rebind(case.sid)  # emulate a start-time identity, before pinning
    speech = await case.d.approved_speech.prepare_chitchat(case.sid, "name")
    assert speech.response_kind == "chitchat" and "Nhật Anh" in speech.text
    assert not case.llm.started.is_set()
    meta["host_name"] = "Tên mới"
    await case.d.store.set(case.sid, meta)
    with pytest.raises(SpeechRejected, match="stale_execution_envelope"):
        await case.d.approved_speech.revalidate(speech)


@pytest.mark.asyncio
async def test_model_free_text_cannot_become_social_speech(case_factory, monkeypatch):
    monkeypatch.setenv("SOCIAL_REPLY_ENABLED", "1")
    monkeypatch.setenv("SOCIAL_MODEL_ENABLED", "1")
    case = await case_factory()
    case.llm.output = "Mình 20 tuổi ở Hà Nội, áo giảm 50%."
    speech = await case.d.approved_speech.prepare_chitchat(case.sid, "greeting", llm=case.llm)
    assert speech.text in replies("greeting") and case.llm.started.is_set()
    monkeypatch.setenv("SOCIAL_REPLY_ENABLED", "0")
    with pytest.raises(SpeechRejected, match="social_disabled"):
        await case.d.approved_speech.revalidate(speech)


@pytest.mark.asyncio
async def test_ingestion_social_bypasses_demand_but_keeps_safety(case_factory, monkeypatch):
    monkeypatch.setenv("SOCIAL_REPLY_ENABLED", "1")
    monkeypatch.setenv("RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED", "1")
    case = await case_factory()
    await canonical(case)
    co = case.d.coordinator
    now = [100.0]
    co._monotonic = lambda: now[0]
    assert (await post(case, "chị tên gì"))["accepted"] == 1
    assert co._social[case.sid].pending.intent == "name"
    await case.d.reducer.run_once(case.sid, 0)
    store = case.d.reducer.session_store(case.sid)
    assert store is None or not store.active_clusters(0)
    now[0] += 3
    turn = co._social_decision(case.sid, case.d.director.get_session(case.sid))
    turn.revision_token = case.d.director.current_generation_token(case.sid)
    co.live_follow.turn_selected(case.sid, turn, case.d.director.get_session(case.sid))
    await active.prepare(case, turn)
    assert turn.prepared_script and not turn.is_cancelled
    assert await co._maybe_speak(case.sid, turn)
    assert case.tts.calls and not case.llm.started.is_set()
    snapshot = co.live_follow.snapshot(case.sid)
    completed = next(t for t in snapshot["turns"] if t["turn_id"] == turn.turn_id)
    assert completed["turn_kind"] == completed["response_kind"] == "chitchat"
    assert completed["status"] == "completed"
    assert (await post(case, "đụ má chào chị", identity="abuse"))["rejected"] == 1


@pytest.mark.asyncio
async def test_legacy_start_cannot_supply_identity(case_factory):
    case = await case_factory()
    response = await case.client.post("/api/v1/sessions", json={"host_name": "Lan"})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_default_off_does_not_admit_greeting(case_factory, monkeypatch):
    monkeypatch.delenv("SOCIAL_REPLY_ENABLED", raising=False)
    case = await case_factory()
    await canonical(case)
    assert (await post(case, "chào shop"))["events"][0]["reason"] == "non_question"
