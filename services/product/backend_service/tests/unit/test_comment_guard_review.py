"""Reviewer reproductions through matching, authenticated ASGI intake and speech."""

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated
from backend.application.safety_gate.text_matching import fold_text
from backend.application.script_authoring.answer_context import deflection_for, question_kind
from backend.application.script_authoring.approved_speech import SpeechRejected
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory

BENIGN = [
    "đồ ngủ có size nào",
    "size lớn còn không",
    "các màu nào",
    "đủ size không",
    "buổi tối ship nhé",
    "vải dẻo không",
    "ship về Bùi Thị Xuân",
    "đẹp điên",
    "rẻ điên",
    "shop có dao kéo nhà bếp không",
    "đồ ngủ nam",
    "đồ ngủ nữ",
    "ngủ ngon với gối này",
    "ngôn ngữ trên hộp là gì",
    "size lớn nhất bao nhiêu",
    "các mẫu mới về",
    "các chị cho em xem màu",
    "đủ màu cho bé không",
    "dư size đổi được không",
    "buổi sáng nhận hàng được không",
    "vải đeo có bền không",
    "dây đeo túi còn không",
    "vải mềm dẻo",
    "gửi chị Bùi Thị Lan",
    "giao đường Bùi Viện",
    "đào màu hồng còn không",
    "đi dạo mặc đẹp không",
    "dao nhà bếp giá sao",
    "kéo cắt vải giá bao nhiêu",
    "điện thoại chống nước không",
    "địa chỉ số điện thoại",
    "màu đều đẹp",
    "khung áo chắc không",
    "bảo hành bao lâu",
    "dù che nắng giá sao",
    "hàm lượng nguyên liệu",
    "admin tư vấn em",
    "account mua hàng",
    "mã acc123",
    "áo màu nâu",
    "màu da còn hàng",
    "đồ bơi có size lớn",
    "đủ hai cái không",
    "ship buổi trưa nhé",
    "dây đeo màu nào",
    "các loại vải",
    "đẹp điên luôn shop",
    "rẻ điên luôn",
    "cắt chỉ bằng kéo này được không",
    "lớn hơn một size",
    "các bạn cho hỏi giá",
    "em đủ tiền mua hai cái",
    "còn đủ size không",
    "vitamin C",
    "vitamin B bổ sung",
    "đồ điện gia dụng",
]
BENIGN = list(dict.fromkeys(BENIGN + [fold_text(s) for s in BENIGN]))

COMMERCE = [
    "chốt đơn",
    "em lấy 2 cái",
    "nguyên liệu gì",
    "kích thước bao nhiêu",
    "giao về Cần Thơ không",
    "có co giãn không",
    "có nước hoa không",
    "giữ em 1 bộ",
    "để mình 3 chiếc",
    "cho em 2 cái",
    "cho mình hai bộ",
    "ib em",
    "inbox mình nhé",
    "order 1 hộp",
    "đặt hai bộ",
    "mua",
    "mặc có nóng không",
    "dùng được bao lâu",
    "loại nào",
    "còn chưa",
    "bao giờ có",
    "ở đâu",
    "mấy cái",
    "2 hộp",
    "Cần Thơ",
    "có không?",
    "có tốt không",
    "what is blorpt?",
    "bền？",
    "có bền không",
    "giao về Bùi Thị Xuân",
    "vải đeo không",
]
COMMERCE = list(dict.fromkeys(COMMERCE + [fold_text(s) for s in COMMERCE]))
ABUSE = [
    "đm",
    "đmm",
    "dm",
    "địt",
    "lồn",
    "cặc",
    "vcl",
    "vkl",
    "cc",
    "đéo",
    "đ.m",
    "v c l",
    "ngu uuu",
    "đồ ngu",
    "thằng ngu",
    "mày ngu",
    "ngu quá",
    "địtt giá bao nhiêu?",
    "nguu shop có ship không?",
    "cặcc áo màu gì?",
    "v.v.c.l",
    "địtttt",
    "cặc cặc",
    "đồ điên",
    "thằng điên",
    "mày điên",
    "may dien",
    "thang dien",
    "cầm dao",
    "mang dao",
    "chém chết",
]
PERSONAL = [
    "chị thích ăn gì vậy ạ?",
    "chi thich an gi vay a?",
    "anh có chồng chưa?",
    "chị bao nhiêu tuổi",
    "bạn quê ở đâu?",
    "em yêu ai?",
    "anh uống gì?",
    "chị ngủ chưa?",
    "anh có gia đình chưa?",
    "anh co gia dinh chua?",
]


@pytest.mark.parametrize("text", BENIGN)
def test_benign_commerce_homographs(text):
    assert len(BENIGN) >= 80
    assert not match_curated(text, load_all_curated_patterns())


@pytest.mark.parametrize("text", ABUSE)
def test_required_abuse_and_double_elongations(text):
    assert match_curated(text, load_all_curated_patterns())


@pytest.mark.asyncio
@pytest.mark.parametrize("text", COMMERCE)
async def test_real_commerce_reaches_reducer_via_asgi(case_factory, monkeypatch, text):
    assert len(COMMERCE) >= 40
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    assert (await post(case, text))["accepted"] == 1
    coordinator.assert_called_once()
    reducer.assert_called_once()
    assert coordinator.call_args.args[1] == text


@pytest.mark.asyncio
@pytest.mark.parametrize("text", PERSONAL)
async def test_personal_questions_are_admitted_then_silent_at_speech(
    case_factory, monkeypatch, text
):
    monkeypatch.setenv("RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED", "1")
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["accepted"] == 1
    coordinator.assert_called_once()
    reducer.assert_called_once()
    assert question_kind(text, ["Áo khoác denim"]) == "not_answerable_non_commerce"
    with pytest.raises(SpeechRejected, match="not_answerable_non_commerce"):
        deflection_for(text, ["Áo khoác denim"])
    await case.d.reducer.run_once(case.sid, reducer.call_args.kwargs["comment"].ts)
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    turn.cluster_member_ids = (reducer.call_args.kwargs["comment"].comment_id,)
    turn.source_event_ids = ("event-1",)
    await prepare(case, turn)
    assert turn.is_cancelled
    await case.d.coordinator.live_follow.flush(case.sid)
    skipped = [e for e in case.events if e["type"] == "live_follow.comment.skipped"]
    assert any(e["final"] and e["reason_code"] == "not_answerable_non_commerce" for e in skipped)
    assert not case.tts.calls and not case.llm.started.is_set()
    assert not case.d.director.get_session(case.sid).director.state.topic_cooldown_until


@pytest.mark.parametrize(
    "text", ["chị mặc áo này size nào", "em dùng nước hoa gì", "anh có bán dao không"]
)
def test_personal_pronouns_do_not_hide_commerce(text):
    assert question_kind(text, []) in {"commerce", "current_product", "other_product"}


def test_incidental_fact_word_does_not_authorize_a_personal_fallback():
    assert (
        question_kind("chị thích ăn gì?", ["Áo thích hợp mùa hè"]) == "not_answerable_non_commerce"
    )


@pytest.mark.parametrize("text", ["chốt đơn", "em lấy 2 cái", "chot don", "em lay 2 cai"])
def test_new_purchase_event_survives_topic_cooldown(text):
    from backend.application.director.clustering import Comment
    from .test_session_voice_and_unknown_cooldown import _director

    director = _director()

    def comment(identity, at):
        return Comment(
            id=identity, text=text, embedding=[1.0, 0.0], t=at, intent="buy_intent", product_id="P1"
        )

    first = comment("buyer-1", 10.0)
    spoken = director.decide([first], now=10.5)
    assert spoken.action in {"answer_fact", "answer_cluster"}
    director.mark_spoken(spoken)
    again = director.decide([first, comment("buyer-2", 11.0)], now=11.5)
    assert again.action in {"answer_fact", "answer_cluster"}


@pytest.mark.parametrize("text", ["chốt đơn", "em lấy 2 cái", "chot don", "em lay 2 cai"])
def test_purchase_intent_is_routed_with_or_without_accents(text):
    from backend.application.director.routing import route_hints

    assert route_hints(text, []).intent_candidates == ["buy_intent"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", BENIGN[:10] + [fold_text(s) for s in BENIGN[:10]])
async def test_buyer_homographs_pass_real_safety_gate(case_factory, text):
    case = await case_factory()
    await canonical(case)
    result = await post(case, text)
    assert result["events"][0]["safety"]["accepted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ABUSE)
async def test_obfuscated_abuse_stops_before_runtime(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["events"][0]["safety"]["accepted"] is False
    coordinator.assert_not_called()
    reducer.assert_not_called()
    assert not case.tts.calls and not case.llm.started.is_set()
