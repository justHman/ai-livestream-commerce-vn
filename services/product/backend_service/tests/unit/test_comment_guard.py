"""Intake regressions: abuse never reaches speech; social chat never becomes Q&A."""

import unicodedata

import pytest

from backend.application.safety_gate.resources import load_all_curated_patterns, match_curated
from . import test_safety_gate_active as active
from .test_safety_gate_active import canonical, post, probes

case_factory = active.case_factory


@pytest.mark.parametrize(
    "text",
    [
        "đm",
        "đmm",
        "dm",
        "địt",
        "lồn",
        "cặc",
        "buồi",
        "đụ",
        "vl",
        "vcl",
        "vkl",
        "cc",
        "cl",
        "đéo",
        "đ*t",
        "con mẹ mày",
        "mẹ mày",
        "óc chó",
        "súc vật",
        "thằng chó",
        "đ.m",
        "đ-m",
        "d_m",
        "đ m",
        "v c l",
        "d1t",
        "địtttt",
        "ngu uuu",
        "ngu u u",
        "m@t d@y",
        "c@c",
        "l0n",
        "đ!t",
        unicodedata.normalize("NFD", "địt mẹ mày"),
    ],
)
def test_abuse_matches_at_token_boundaries(text):
    assert "toxicity" in match_curated(text, load_all_curated_patterns())


@pytest.mark.parametrize(
    "text",
    [
        "ngủ",
        "đồ ngủ",
        "nguyên liệu",
        "admin",
        "account",
        "acc",
        "vlog",
        "đỉnh",
        "điệt",
        "size lớn",
        "các màu",
        "đủ size",
        "buổi tối ship nhé",
        "vải co giãn dẻo",
        "khung áo",
        "mã acc123",
        "giá bao nhiêu",
        "ship về HN mấy ngày",
        "chất liệu gì vậy ad",
        "áo màu nâu",
        "màu da",
    ],
)
def test_benign_fashion_words_do_not_match(text):
    assert match_curated(text, load_all_curated_patterns()) == []


def test_non_question_fallback_does_not_claim_the_viewer_asked_a_question():
    from backend.application.script_authoring.answer_context import deflection_for
    from backend.application.script_authoring.approved_speech import SpeechRejected

    with pytest.raises(SpeechRejected, match="non_question"):
        deflection_for("\u0111\u1eb9p qu\u00e1", ["\u0111\u1eb9p"])


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["đ.m", "con mẹ mày", "v c l", "d1t"])
async def test_abuse_stops_before_reducer_and_speech(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["rejected"] == 1
    assert result["events"][0]["safety"]["accepted"] is False
    meta = await case.d.store.get(case.sid)
    assert text not in str(meta)
    coordinator.assert_not_called()
    reducer.assert_not_called()
    assert not case.llm.started.is_set()
    assert not case.tts.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "hahaha",
        "haha 😂",
        "kkkk",
        "555",
        "ok",
        "👍",
        "hay quá",
        "xin chào",
        "hehe",
        "lol",
        ":))",
        "aaaaa",
        "đẹp quá",
        "thả tim",
    ],
)
async def test_social_chat_skipped_before_director(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    result = await post(case, text)
    assert result["events"][0]["reason"] == "non_question"
    assert result["rejected"] == 1
    coordinator.assert_not_called()
    reducer.assert_not_called()
    assert not case.llm.started.is_set()
    assert not case.tts.calls
    assert not (await case.d.store.get(case.sid)).get("pending_platform_chat")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "giá bao nhiêu",
        "có size L không",
        "ship về HN mấy ngày",
        "áo này màu gì",
        "chất liệu gì vậy ad",
        "xin chào, còn hàng không",
        "haha giá sao",
        "mua",
        "bảo hành mấy năm",
        "đổi trả thế nào",
    ],
)
async def test_real_question_keeps_original_text(case_factory, monkeypatch, text):
    case = await case_factory()
    await canonical(case)
    coordinator, reducer = probes(case, monkeypatch)
    assert (await post(case, text))["accepted"] == 1
    coordinator.assert_called_once()
    reducer.assert_called_once()
    assert coordinator.call_args.args[1] == text


@pytest.mark.parametrize(
    "text",
    ["mau \u0111\u1ec1u \u0111\u1eb9p", "\u0111\u1ecba ch\u1ec9 s\u1ed1 \u0111i\u1ec7n tho\u1ea1i"],
)
def test_additional_commerce_homographs(text):
    assert match_curated(text, load_all_curated_patterns()) == []


def test_unsafe_content_symbol_patterns_keep_their_original_meaning():
    sets = load_all_curated_patterns()
    assert "unsafe_content" not in match_curated("comment 18", sets)
    assert "unsafe_content" in match_curated("18+", sets)
