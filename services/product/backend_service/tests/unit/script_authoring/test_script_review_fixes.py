"""Review fixes for the filler gate, intake evidence minimum and the approved-side whole-word match."""

from __future__ import annotations

import json
import unicodedata

import pytest

from backend.application.product_intake import extract_facts
from backend.application.product_intake.extractor import build_prompt
from backend.application.script_authoring.generation.ordered_units import (
    check_unit_text,
)

NFD = lambda s: unicodedata.normalize("NFD", s)  # noqa: E731


@pytest.mark.parametrize(
    ("text", "approved", "reason"),
    [
        ("Bé chuyên tâm học tập.", ("Giúp bé tập trung học tập",), None),
        ("Mọi người cứ yên tâm nhé.", ("Giúp bé tập trung học tập",), "hard_sell"),
        ("Mọi người yên tâm đổi trả trong bảy ngày.", ("Yên tâm đổi trả 7 ngày",), None),
        ("Mọi người yên tâm đổi trả trong bảy ngày.", (NFD("Yên tâm đổi trả 7 ngày"),), None),
        (NFD("Mọi người yên tâm đổi trả trong bảy ngày."), ("Yên tâm đổi trả 7 ngày",), None),
        (NFD("Mọi người cứ yên tâm nhé."), (), "hard_sell"),
        ("Mọi người cứ yên tâm nhé.", ("Bé chuyên tâm học tập.",), "hard_sell"),
    ],
)
def test_filler_gate_uses_whole_words_and_unicode_normalisation(text, approved, reason):
    assert check_unit_text(text, approved=approved)[1] == reason


def test_intake_prompt_states_the_evidence_minimum_the_code_enforces():
    assert "TỐI THIỂU 12 ký tự và 3 từ" in build_prompt("abc")


def test_multi_feature_bottle_sentence_passes_the_code_floor():
    source = "Bình giữ nhiệt 500ml, giữ nóng 6 giờ, giữ lạnh 12 giờ, nắp xoay chống tràn, có 3 màu."
    facts = [
        ("Bình giữ nhiệt 500ml", "Bình giữ nhiệt 500ml"),
        ("Giữ nóng 6 giờ", "Bình giữ nhiệt 500ml, giữ nóng 6 giờ"),
        ("Giữ lạnh 12 giờ", "giữ nóng 6 giờ, giữ lạnh 12 giờ"),
        ("Nắp xoay chống tràn", "giữ lạnh 12 giờ, nắp xoay chống tràn"),
        ("Có 3 màu", "chống tràn, có 3 màu"),
    ]
    out = json.dumps({"facts": [{"type": "feature", "text": t, "evidence": e} for t, e in facts]})
    result = extract_facts(source, lambda _prompt: out)
    assert (len(result.facts), result.dropped) == (5, ())


def test_bare_short_evidence_is_still_dropped_by_the_safety_floor():
    out = json.dumps({"facts": [{"type": "feature", "text": "Có 3 màu", "evidence": "có 3 màu"}]})
    result = extract_facts("Bình giữ nhiệt, có 3 màu.", lambda _prompt: out)
    assert [d.reason for d in result.dropped] == ["evidence_too_short"]
