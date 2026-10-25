"""Round-four common commerce homophones and host-private-life HTTP probes."""

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated
from backend.application.script_authoring.answer_context import deflection_for, question_kind
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare
from .test_comment_guard_round2 import variants
from .test_comment_guard_round3 import ABUSE
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory

BENIGN = variants(
    ["shop có đồ cho bé không?", "em mua đồ cho chồng"]
    + [
        f"{request} {recipient} {ending}"
        for request in ("shop có đồ cho", "em mua đồ cho", "cho em xin đồ cho", "mua cho")
        for recipient in ("bé", "chồng", "vợ", "mẹ", "con")
        for ending in ("được không?", "giá bao nhiêu?")
    ]
    + [
        "chị có đồ ăn giá bao nhiêu?",
        "cho em xin giá bộ đồ ngủ không?",
        "tháng cho bé mấy bộ?",
        "mẫu nào cho mẹ?",
        "đồ cho má còn không?",
        "ốc cho em hai hộp",
        "con khôn chọn đồ cho mẹ",
        "đồ khi nào giao?",
        "cho em xem đồ ăn",
        "cho xin đồ ngủ",
        "shop có bộ máy này không?",
        "cho em xin mã máy",
        "mua bộ máy cho mẹ",
        "bộ lao động giá bao nhiêu?",
        "con cho em xin giá",
    ]
)
SALES = variants(
    [
        "chị có đồ ăn giá bao nhiêu?",
        "cho em xin giá bộ đồ ngủ không?",
        "em tên Lan muốn lấy hàng được không?",
        "chị có áo này size M không?",
        "chị có bán áo không?",
    ]
    + [
        f"{buyer} {request} không?"
        for buyer in ("chị có", "em tên Lan muốn hỏi", "mình ở Hà Nội muốn hỏi")
        for request in (
            "đồ ăn giá bao nhiêu",
            "đồ ngủ xin giá",
            "lấy hàng được",
            "lấy 2 được",
            "đặt được",
            "chốt được",
            "size M còn",
            "ship về Hà Nội được",
            "giao hàng được",
            "còn hàng",
            "mã giảm",
            "đổi trả được",
            "bảo hành được",
            "order được",
        )
    ]
)
PERSONAL = variants(
    [
        "chị có thích mua sắm không?",
        "chị có chồng bán áo không?",
        "chị có người yêu chạy ship không?",
    ]
    + [
        f"{host} có {predicate} không?"
        for host in ("chị", "em", "anh", "cô", "bạn")
        for predicate in (
            "chồng bán áo",
            "vợ bán quần",
            "người yêu chạy ship",
            "con bán giày",
            "bạn trai bán túi",
            "bạn gái mua son",
            "thích mua sắm",
            "hay mua áo",
            "từng bán quần",
            "định mua giày",
        )
    ]
)
# Round five: privacy vetoes price/shipping too; ASCII con ban is uncertain
# stock context and keeps commerce eligibility. Accented con still means child.
PERSONAL += variants(
    [
        "ch\u1ecb c\u00f3 ch\u1ed3ng b\u00e1n \u00e1o gi\u00e1 bao nhi\u00eau?",
        "ch\u1ecb c\u00f3 th\u00edch \u00e1o size M kh\u00f4ng?",
        "ch\u1ecb c\u00f3 ng\u01b0\u1eddi y\u00eau ch\u1ea1y ship v\u1ec1 H\u00e0 N\u1ed9i kh\u00f4ng?",
        "ch\u1ecb c\u00f3 ch\u1ed3ng l\u00e0m ngh\u1ec1 giao h\u00e0ng, \u00e1o gi\u00e1 bao nhi\u00eau?",
        "ch\u1ecb sinh n\u0103m bao nhi\u00eau, \u00e1o size M c\u00f2n kh\u00f4ng?",
    ]
)
ASCII_STOCK = [s for s in PERSONAL if "co con ban" in s]
PERSONAL = [s for s in PERSONAL if s not in ASCII_STOCK]
SALES += ASCII_STOCK


def test_corpus_minimums():
    assert len(BENIGN) >= 80
    assert len(SALES) >= 60
    assert len(PERSONAL) >= 60


@pytest.mark.parametrize("text", BENIGN)
def test_benign_homophones(text):
    assert not match_curated(text, load_all_curated_patterns())


@pytest.mark.parametrize("text", ABUSE)
def test_existing_abuse_with_accents_or_targeting(text):
    assert match_curated(text, load_all_curated_patterns())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,personal", [(t, False) for t in SALES] + [(t, True) for t in PERSONAL]
)
async def test_http_intent(case_factory, monkeypatch, text, personal):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    if personal:
        assert result["accepted"] == 1, result
        coordinator.assert_called_once()
        reducer.assert_called_once()
        turn = decision(case, generated=True)
        turn.cluster_members = (text,)
        await prepare(case, turn)
        assert turn.is_cancelled
        assert not case.llm.started.is_set() and not case.tts.calls
        assert not case.d.director.get_session(case.sid).director.state.topic_cooldown_until
    else:
        assert result["accepted"] == 1, result
        coordinator.assert_called_once()
        reducer.assert_called_once()
        assert coordinator.call_args.args[1] == text
        assert question_kind(text, []) in {"commerce", "current_product", "other_product"}
        assert deflection_for(text, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", BENIGN)
async def test_http_benign_safety(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    probes(case, monkeypatch)
    result = await post(case, text)
    assert result["events"][0]["safety"]["accepted"], result
