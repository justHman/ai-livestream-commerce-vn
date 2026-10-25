"""Round two corpus: real authenticated HTTP intake, plus the speech boundary."""

import pytest

from backend.application.safety_gate.text_matching import fold_text
from backend.application.script_authoring.answer_context import deflection_for, question_kind
from backend.application.script_authoring.approved_speech import SpeechRejected
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare
from .test_comment_guard_review import BENIGN, COMMERCE, ABUSE as REVIEW_ABUSE
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory


def variants(texts):
    return list(dict.fromkeys(text for s in texts for text in (s, fold_text(s))))


REAL = variants(
    BENIGN
    + COMMERCE
    + [
        "địa chỉ số điện thoại",
        "giảm chút nhé",
        "xin mã",
        "gửi Nguyễn Thị Lan",
        "admin ơi",
        "acc",
        "đẹp điên",
        "nhắn riêng giúp mình",
        "đợi em chút",
        "cho xem mặt sau",
        "quay gần hơn",
        "đọc bình luận giúp em",
        "tăng âm lượng",
        "em chưa nghe rõ",
        "đứng xa một chút",
        "mình đang xem",
        "đang chọn đây",
        "để em suy nghĩ",
        "mình quay lại sau",
        "em nhận rồi",
        "đúng rồi đó",
        "gói kỹ giúp em",
        "thêm ảnh nhé",
        "chụp chi tiết",
        "xem đường may",
        "có bớt chút nào không",
        "hẹn tối mai",
        "gửi về cơ quan",
        "lấy 3 chai",
        "áo đẹp quá",
        "xinh quá mua ở đâu",
        "hello xin mã",
        "like mẫu này",
        "thả tim rồi mua thế nào",
        "nghe rõ rồi",
        "em theo dõi lâu rồi",
        "mình cần thêm thông tin",
        "đặt giúp mẹ em",
        "kiểm tra tin nhắn nhé",
        "chờ mình chuyển khoản",
        "đơn trước chưa tới",
        "cho em xem thử",
    ]
)
SOCIAL = variants(
    [
        "hahaha",
        "hihi",
        "hehe",
        "kkkk",
        "555",
        "lol",
        ":))",
        "😂",
        "👍",
        "!!!",
        "123456",
        "ok",
        "oke",
        "ừ",
        "uh",
        "hay quá",
        "tuyệt",
        "đẹp quá",
        "xinh",
        "like",
        "thả tim",
        "xin chào",
        "chào",
        "hello",
        "hi",
        "aaaaa",
        "zzzzz",
        "asdfgh",
        "qwerty",
        "hihi 😂",
        "ok!",
        "tuyệt vời",
        "cảm ơn",
    ]
)
PERSONAL = variants(
    [
        "chị có định lấy chồng không?",
        "chị quê Cần Thơ à?",
        "chị bao nhiêu tuổi",
        "chị thích ăn gì",
        "anh có người yêu không?",
        "em có định kết hôn không?",
        "cô quê Hà Nội à?",
        "bạn sống ở Đà Nẵng à?",
        "chị nhà ở đâu vậy?",
        "anh làm nghề gì vậy?",
        "chị còn độc thân không?",
        "em uống gì vậy?",
        "anh ngủ chưa?",
        "chị yêu ai vậy?",
        "cô thích ăn gì vậy?",
        "bạn có thích uống trà không?",
        "chị có hay ngủ muộn không?",
        "anh có vợ chưa?",
        "em có bạn trai chưa?",
        "chị có gia đình chưa?",
        "chị có định lấy chồng ở Hà Nội không?",
        "chị quê ở Bùi Viện à?",
        "chị thích ăn cơm hay phở vậy?",
        "anh bao nhiêu tuổi vậy?",
        "bạn có sống ở Cần Thơ không?",
        "cô có yêu ai không?",
        "chị có thích hát không?",
        "anh có biết nhảy không?",
    ]
)
ABUSE = variants(
    REVIEW_ABUSE
    + [
        "óc chó",
        "súc vật",
        "thằng chó",
        "con mẹ mày",
        "mẹ mày",
        "mất dạy",
        "địt mẹ mày",
        "đụ má",
        "địt con mẹ mày",
        "vcl mày",
        "đmm mày",
        "cc mày",
        "đéo mua",
        "vkl shop",
        "địt địt",
        "cặcc",
        "địttt",
        "ngu u u",
    ]
)
# ASCII homographs without an insult address intentionally favour the buyer.
ABUSE = [
    s
    for s in ABUSE
    if s
    not in {
        "lon",
        "cac",
        "deo",
        "do ngu",
        "cacc ao mau gi?",
        "cac cac",
        "do dien",
        "du ma",
        "deo mua",
        "cacc",
    }
]
ABUSE = [s for s in ABUSE if s not in {"oc cho", "thang cho"}]


def test_corpus_minimums():
    assert len(REAL) >= 200
    assert len(SOCIAL) >= 30
    assert len(PERSONAL) >= 40
    assert len(ABUSE) >= 60
    assert len(set(REAL + SOCIAL + PERSONAL + ABUSE)) >= 330


@pytest.mark.asyncio
@pytest.mark.parametrize("text", REAL)
async def test_real_comments_default_to_runtime(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["accepted"] == 1, result
    assert result["events"][0]["safety"]["accepted"]
    coordinator.assert_called_once()
    reducer.assert_called_once()
    assert coordinator.call_args.args[1] == text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,reason",
    [(s, "non_question") for s in SOCIAL] + [(s, "not_answerable_non_commerce") for s in PERSONAL],
)
async def test_social_only_skips_intake_personal_silence_is_at_speech(
    case_factory, monkeypatch, text, reason
):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    if reason == "non_question":
        assert result["events"][0]["reason"] == reason
        coordinator.assert_not_called()
        reducer.assert_not_called()
    else:
        assert result["accepted"] == 1
        coordinator.assert_called_once()
        reducer.assert_called_once()
    with pytest.raises(SpeechRejected, match=reason):
        deflection_for(text, ["Áo khoác denim"])
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    await prepare(case, turn)
    assert turn.is_cancelled
    assert not case.llm.started.is_set() and not case.tts.calls
    assert not case.d.director.get_session(case.sid).director.state.topic_cooldown_until


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ABUSE)
async def test_abuse_never_reaches_runtime(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["events"][0]["safety"]["accepted"] is False
    coordinator.assert_not_called()
    reducer.assert_not_called()
    assert not case.llm.started.is_set() and not case.tts.calls


@pytest.mark.parametrize(
    "text",
    variants(
        [
            "admin ơi",
            "acc",
            "đẹp điên",
            "gửi Nguyễn Thị Lan",
            "Cần Thơ",
            "what is blorpt?",
            "có không?",
            "chị có định lấy chồng không?",
            "chị quê Cần Thơ à?",
            "thủ đô nước Pháp là gì?",
        ]
    ),
)
def test_uncertain_text_cannot_authorize_a_shop_fallback(text):
    with pytest.raises(SpeechRejected):
        deflection_for(text, ["Áo khoác denim"])


@pytest.mark.parametrize(
    "text",
    variants(
        [
            "xin mã",
            "giảm chút nhé",
            "địa chỉ số điện thoại",
            "lấy 2 cái",
            "chị mặc áo này size nào",
            "em dùng nước hoa gì",
            "anh có bán dao không",
            "giao về Cần Thơ không",
            "ship về Hà Nội nhé",
        ]
    ),
)
def test_clear_commerce_retains_fallback(text):
    assert question_kind(text, []) in {"commerce", "current_product", "other_product"}
    assert deflection_for(text, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", variants(["admin ơi", "acc", "đẹp điên", "gửi Nguyễn Thị Lan"]))
async def test_unclassified_speech_requires_positive_commerce_evidence(case_factory, text):
    case = await case_factory()
    case.llm.output = ""  # Normal answer path has nothing approved to say.
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    await prepare(case, turn)
    assert turn.is_cancelled
    assert not turn.prepared_script
    assert not case.llm.started.is_set() and not case.tts.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    variants(
        [
            "chị có định lấy chồng không? | áo size nào",
            "giá bao nhiêu | chị quê Cần Thơ à?",
        ]
    ),
)
async def test_personal_cluster_member_cannot_leak_to_model(case_factory, text):
    case = await case_factory()
    turn = decision(case, generated=True)
    turn.cluster_members = tuple(text.split(" | "))
    await prepare(case, turn)
    assert turn.is_cancelled
    assert not case.llm.started.is_set() and not case.tts.calls
