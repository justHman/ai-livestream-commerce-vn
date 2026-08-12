"""Table-driven rule tests (task 3.11): TTS readiness + repetition + duration.

Covers TTS normalization cases (numbers, prices, markup, control chars,
acronyms), local repetition, CTA frequency, and the segment duration rule
(via Change A's estimator — parity with the upstream estimator is asserted).
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.rules.duration import check_segment_duration
from backend.application.script_authoring.gate.rules.repetition import (
    check_cta_frequency,
    check_local_repetition,
)
from backend.application.script_authoring.gate.rules.tts_readiness import (
    check_tts_acronyms,
    check_tts_control_chars,
    check_tts_markup,
    check_tts_numbers,
    normalize_tts_text,
)

_FACTS = ProductFacts(prices=("299.000",), skus=("SKU-P004",))


def _ctx(**overrides) -> ScriptGateContext:
    return ScriptGateContext(facts=_FACTS, **overrides)


# -- TTS readiness ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_rules"),
    [
        # clean
        ("Kem dưỡng ẩm sâu.", []),
        # raw number
        ("mua 3 cái nhé", ["TTS_NUMBER"]),
        # grouped price is NOT a bare number (commerce rule's concern)
        ("giá 299.000đ", []),
        ("giá 1.299.000đ", []),
        # percent is not a bare number
        ("giảm 20%", []),
        # markup (two tags -> two violations)
        ("<b>html</b>", ["TTS_MARKUP", "TTS_MARKUP"]),
        # control chars
        ("ab", ["TTS_CONTROL"]),
        # acronym: SKU in facts.skus -> exempt; other acronym flagged
        ("mã SKU-P004", []),
        ("dùng API mới", ["TTS_ACRONYM"]),
        # acronym "ABC" in a brand-ish name is still flagged for TTS review
        ("Kem ABC dưỡng ẩm sâu.", ["TTS_ACRONYM"]),
    ],
)
def test_tts_readiness_rules(text: str, expected_rules: list[str]) -> None:
    violations = (
        check_tts_numbers(text, _ctx())
        + check_tts_markup(text, _ctx())
        + check_tts_control_chars(text, _ctx())
        + check_tts_acronyms(text, _ctx())
    )
    assert sorted(v.rule_id for v in violations) == sorted(expected_rules)


def test_normalize_tts_text_strips_markup_and_control() -> None:
    normalized = normalize_tts_text("  **bold**  ab  ")
    assert normalized == "ab"


def test_normalize_tts_text_idempotent() -> None:
    text = "**bold**  a  b\tc"
    assert normalize_tts_text(normalize_tts_text(text)) == normalize_tts_text(text)


# -- repetition ------------------------------------------------------------


def test_local_repetition_flags_repeated_phrase() -> None:
    text = "mua ngay mua ngay mua ngay kem này tốt kem này tốt kem này tốt "
    violations = check_local_repetition(text, _ctx())
    assert any(v.rule_id == "REPETITION_LOCAL" for v in violations)


def test_local_repetition_clean() -> None:
    assert check_local_repetition("Kem này tốt, giá tốt, mua ngay nhé.", _ctx()) == []


def test_cta_frequency_exceeds_limit() -> None:
    text = "Mua ngay! Đặt ngay! Chốt đơn! Order ngay!"
    violations = check_cta_frequency(text, _ctx(max_cta_per_segment=3))
    assert any(v.rule_id == "REPETITION_CTA" for v in violations)


def test_cta_frequency_within_limit() -> None:
    text = "Mua ngay kẻo hết hàng!"
    assert check_cta_frequency(text, _ctx(max_cta_per_segment=3)) == []


# -- duration (task 3.9, via Change A estimator) ---------------------------


def test_segment_duration_too_short() -> None:
    violations = check_segment_duration("Ngắn quá.", _ctx(target_min_seconds=60))
    assert any(v.rule_id == "SPEECH_DURATION_SEGMENT" for v in violations)


def test_segment_duration_within_range() -> None:
    long_text = " ".join("Kem ABC dưỡng ẩm sâu cho làn da mềm mại." for _ in range(30))
    assert (
        check_segment_duration(long_text, _ctx(target_min_seconds=10, target_max_seconds=600)) == []
    )


def test_segment_duration_uses_change_a_estimator_parity() -> None:
    """The gate's duration check MUST call the upstream Change A estimator
    (tasks 4.2a/4.2b parity: no duplicate syllable/duration algorithm)."""
    from backend.application.text_chunker import SpeechDurationEstimator

    text = "Xin chào, hôm nay giảm giá 50% cho 299.000đ nhé!"
    upstream = SpeechDurationEstimator().estimate_ms(text)
    violations = check_segment_duration(
        text,
        _ctx(target_min_seconds=0, target_max_seconds=upstream / 1000.0),
    )
    assert violations == []
    violations = check_segment_duration(
        text,
        _ctx(target_min_seconds=0, target_max_seconds=upstream / 1000.0 - 0.001),
    )
    assert any(v.rule_id == "SPEECH_DURATION_SEGMENT" for v in violations)
