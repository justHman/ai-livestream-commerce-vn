"""Round-three regressions through authenticated HTTP and speech preparation."""

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated
from backend.application.script_authoring.answer_context import deflection_for, question_kind
from . import test_approved_speech_active as active
from .test_approved_speech_active import decision, prepare
from .test_comment_guard_round2 import REAL, variants
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory

SALES = variants(
    [
        "chị bán đồ ăn không?",
        "em lấy 2 bộ cho chồng được không?",
        "em tên Lan chốt 2 cái được không?",
        "bán đồ ăn không?",
    ]
    + [
        f"{buyer} {order} được không?"
        for buyer in ("em gửi cho chồng,", "em gửi cho vợ,", "em gửi cho con,", "em tên Lan,")
        for order in (
            "lấy 2 bộ",
            "chốt 3 cái",
            "đặt 1 hộp",
            "mua 2 chiếc",
            "lấy 4 chai",
            "đặt mã AB123",
            "lấy 2",
            "chốt đơn",
        )
    ]
    + [
        "em tên Lan lấy 2 được không?",
        "em lấy 2 cho vợ được không?",
        "em lấy cho chồng 2 bộ được không?",
        "em đặt cho vợ 3 cái được không?",
        "em tên Lan đặt giúp con 1 hộp được không?",
    ]
)
PERSONAL = variants(
    ["chị có chồng làm nghề giao hàng không?", "chị sinh năm bao nhiêu?"]
    + [
        f"{host} {predicate}"
        for host in ("chị", "anh", "em", "cô", "bạn")
        for predicate in (
            "có chồng làm nghề giao hàng không?",
            "có vợ làm nghề giao hàng không?",
            "có định lấy chồng không?",
            "có định lấy vợ không?",
            "có người yêu làm nghề bán hàng không?",
            "có chồng bán đồ ăn không?",
            "sinh năm bao nhiêu?",
        )
    ]
)

# ASCII vo plus a sales clause is ambiguous and cannot veto commerce fallback.
ASCII_VO_SALES = [text for text in PERSONAL if " co vo " in text]
PERSONAL = [text for text in PERSONAL if text not in ASCII_VO_SALES]
SALES += ASCII_VO_SALES

# Only four literal misses were supplied with the review; these fourteen cover
# those four and the corresponding curated phrase/homograph failure shapes.
REVIEW_MISSES = [
    "óc lợn",
    "cút mẹ đi",
    "may an cut",
    "con cac may",
    "oc lon",
    "cut me di",
    "não lợn",
    "nao lon",
    "óc cứt",
    "oc cut",
    "nhu cut",
    "an cut",
    "cut di",
    "cut me may di",
]
ABUSE = variants(
    REVIEW_MISSES
    + [
        "óc lợn",
        "cút mẹ",
        "đồ chó",
        "con cặc mày",
        "mày ăn cứt",
        "não lợn",
        "óc cứt",
        "như cứt",
        "ăn cứt",
        "cút đi",
        "cút mẹ mày đi",
        "đồ súc vật",
        "thằng mất dạy",
        "con mất dạy",
        "đồ rác rưởi",
        "đồ chó má",
        "thằng khốn",
        "con khốn",
        "câm mẹ mồm",
        "mày ngu",
        "thằng ngu",
        "mày điên",
    ]
)
ABUSE = list(dict.fromkeys(ABUSE + [s.replace(" ", " . ") for s in ABUSE]))
ABUSE += variants(
    [
        f"{subject} {insult}"
        for subject in ("mày là", "mày đúng là", "đúng là", "cái thứ", "cái đồ")
        for insult in (
            "đồ chó",
            "đồ khốn nạn",
            "đồ súc vật",
            "đồ mất dạy",
            "đồ rác rưởi",
            "đồ chó má",
        )
    ]
)
ABUSE = list(dict.fromkeys(ABUSE + ["ó.c l.ợ.n", "c.ú.t mẹ đi", "con c.a.c may", "may an c.u.t"]))
# ASCII cho phrases remain ambiguous regardless of targeting. Their accented
# originals remain abuse regressions; round five verifies ASCII admission.
ABUSE = [s for s in ABUSE if "cho" not in s.split() and s not in {"con khon", "con . khon"}]


def test_corpus_minimums():
    assert len(SALES) >= 60
    assert len(PERSONAL) >= 60
    assert len(ABUSE) >= 60
    assert len(REAL) >= 250


@pytest.mark.asyncio
@pytest.mark.parametrize("text", SALES)
async def test_purchase_wins_over_buyer_context(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["accepted"] == 1, result
    coordinator.assert_called_once()
    reducer.assert_called_once()
    assert coordinator.call_args.args[1] == text
    assert question_kind(text, []) in {"commerce", "current_product", "other_product"}
    assert deflection_for(text, [])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", PERSONAL)
async def test_personal_with_vague_sales_words_stays_silent(case_factory, monkeypatch, text):
    case = await case_factory()
    case.llm.output = ""  # Attempt approved facts before refusing a personal fallback.
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["accepted"] == 1, result
    coordinator.assert_called_once()
    reducer.assert_called_once()
    turn = decision(case, generated=True)
    turn.cluster_members = (text,)
    await prepare(case, turn)
    assert turn.is_cancelled
    assert case.llm.started.is_set() and not case.tts.calls
    assert not case.d.director.get_session(case.sid).director.state.topic_cooldown_until


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ABUSE)
async def test_curated_phrases_survive_homograph_protection(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["events"][0]["safety"]["accepted"] is False, result
    coordinator.assert_not_called()
    reducer.assert_not_called()
    assert not case.llm.started.is_set() and not case.tts.calls


@pytest.mark.parametrize(
    "text",
    REAL
    + variants(
        [
            "còn các màu nào?",
            "cái lớn còn không?",
            "vải lớn",
            "chị bán đồ ngủ không?",
        ]
    ),
)
def test_existing_benign_corpus_remains_safe(text):
    assert not match_curated(text, load_all_curated_patterns())
