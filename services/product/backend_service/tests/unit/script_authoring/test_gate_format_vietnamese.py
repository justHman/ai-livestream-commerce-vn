"""Table-driven rule tests (task 3.11): format + Vietnamese.

Clean, blocked, warning-only, and brand-allowlist cases for the FORMAT and
VN_SPELLING rule families. Every case asserts the exact expected violation
rule IDs so a rule-behavior change fails loudly.
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.gate.context import ScriptGateContext
from backend.application.script_authoring.gate.rules.format import (
    check_control_characters,
    check_em_dash,
    check_punctuation,
    check_whitespace,
)
from backend.application.script_authoring.gate.rules.vietnamese import (
    check_common_spelling,
    check_tense_spacing,
)

_CLEAN = "Kem ABC dưỡng ẩm sâu, giá 299.000đ. Mua ngay kẻo hết hàng nhé!"


def _ctx(**overrides) -> ScriptGateContext:
    return ScriptGateContext(**overrides)


# -- format family ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_rules"),
    [
        # clean text: no violations
        (_CLEAN, []),
        # repeated punctuation
        ("Sao lại thế này!!", ["FORMAT_PUNCTUATION"]),
        ("Thật không??", ["FORMAT_PUNCTUATION"]),
        ("Thật luôn?!", ["FORMAT_PUNCTUATION"]),
        ("Dấu ba chấm thì ok...", []),
        # whitespace
        ("Hai  khoảng trắng", ["FORMAT_WHITESPACE"]),
        ("Space  ,trước dấu phẩy", ["FORMAT_WHITESPACE"]),
        # em dash (default context forbids)
        ("Giá 299.000đ — chính hãng", ["STYLE_EM_DASH"]),
    ],
)
def test_format_rules(text: str, expected_rules: list[str]) -> None:
    violations = (
        check_control_characters(text, _ctx())
        + check_punctuation(text, _ctx())
        + check_whitespace(text, _ctx())
        + check_em_dash(text, _ctx())
    )
    assert sorted(v.rule_id for v in violations) == sorted(expected_rules)


def test_em_dash_allowed_when_configured() -> None:
    ctx = _ctx(allow_em_dash=True)
    assert check_em_dash("Giá 299.000đ — chính hãng", ctx) == []


def test_control_characters_are_errors() -> None:
    violations = check_control_characters("a​b‮c", _ctx())
    assert [v.rule_id for v in violations] == ["FORMAT_CONTROL", "FORMAT_CONTROL"]
    assert all(v.severity.value == "error" for v in violations)


# -- vietnamese family -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_rules"),
    [
        # clean text
        (_CLEAN, []),
        # gi/d confusion (error)
        ("di chuyển đến đây", ["VN_SPELLING_GI_D"]),
        # wrong tone slot (warning)
        ("tóan học", ["VN_SPELLING_TONE"]),
        ("hòan thành", ["VN_SPELLING_TONE"]),
        # legit Vietnamese never flagged
        ("toàn thời gian hoàn thành", []),
        ("thời gian tiền bạc", []),
    ],
)
def test_vietnamese_rules(text: str, expected_rules: list[str]) -> None:
    violations = check_common_spelling(text, _ctx()) + check_tense_spacing(text, _ctx())
    assert sorted(v.rule_id for v in violations) == sorted(expected_rules)


def test_brand_allowlist_exempts_spelling_check() -> None:
    ctx = _ctx(brand_allowlist=("Gucci",))
    assert check_common_spelling("Gucci diện đẹp", ctx) == []


def test_wrong_tone_is_warning_not_error() -> None:
    violations = check_tense_spacing("tóan học", _ctx())
    assert violations
    assert all(v.severity.value == "warning" for v in violations)


def test_double_tone_is_error() -> None:
    violations = check_tense_spacing("tròàng", _ctx())
    assert any(v.severity.value == "error" for v in violations)
