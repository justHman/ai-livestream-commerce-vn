"""Opt-in gate relaxation keeps unsafe claims and wrong values blocking."""

import pytest

from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.relaxed import relaxed_gate_enabled
from backend.application.script_authoring.gate.results import Severity
from backend.application.script_authoring.gate.rules.commerce_claims import (
    check_factual_claims,
    check_price_claims,
)
from backend.application.script_authoring.gate.rules.full_script import check_contradictory_claims
from backend.application.script_authoring.generation.reason_text import reason_text
from backend.application.script_authoring.generation.unit_brief import fallback_reason_code


def _ctx(**facts) -> ScriptGateContext:
    return ScriptGateContext(facts=ProductFacts(**facts))


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "YeS", "on", "ON"])
def test_flag_is_read_at_call_time(monkeypatch, value):
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    assert not relaxed_gate_enabled()
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", value)
    assert relaxed_gate_enabled()
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "false")
    assert not relaxed_gate_enabled()


def test_descriptive_claim_defaults_to_error(monkeypatch):
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    violations = check_factual_claims("Máy dùng rất dễ dàng.", _ctx())
    assert len(violations) == 1
    assert violations[0].severity == Severity.ERROR


@pytest.mark.parametrize(
    ("text", "severity"),
    [
        ("Máy dùng rất dễ dàng.", Severity.WARNING),
        ("Sản phẩm giúp trị mụn hiệu quả", Severity.ERROR),
        ("cũng không cam kết độ bền", Severity.WARNING),
        ("Bảo hành trọn đời", Severity.ERROR),
        ("Sản phẩm chữa bệnh", Severity.ERROR),
        ("Sản phẩm đạt chuẩn FDA", Severity.ERROR),
        ("không cam kết nhưng đảm bảo hiệu quả", Severity.ERROR),
    ],
)
def test_relaxed_word_claim_severity(monkeypatch, text, severity):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(text, _ctx())
    assert len(violations) == 1
    assert violations[0].rule_id == "CLAIM_FACTUAL"
    assert violations[0].severity == severity


@pytest.mark.parametrize("prefix", ["không", "không hề", "chưa", "chẳng", "không phải"])
def test_strict_claim_negation(monkeypatch, prefix):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    violations = check_factual_claims(f"cũng {prefix} cam kết độ bền", _ctx())
    assert [v.severity for v in violations] == [Severity.WARNING]


def test_allowed_claim_overlap_still_exempt(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert (
        check_factual_claims("Bảo hành trọn đời", _ctx(allowed_claims=("Bảo hành trọn đời",))) == []
    )


def test_owner_number_is_not_a_price_only_when_relaxed(monkeypatch):
    context = _ctx(allowed_claims=("Pin 5.000 mAh dùng 2 ngày",), prices=("299.000",))
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    original = check_price_claims("Pin 5.000 mAh nhé", context)
    assert len(original) == 1
    assert original[0].severity == Severity.ERROR
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_price_claims("Pin 5.000 mAh nhé", context) == []
    wrong_price = check_price_claims("Giá chỉ 199.000đ", context)
    assert len(wrong_price) == 1
    assert wrong_price[0].severity == Severity.ERROR


@pytest.mark.parametrize(
    ("facts", "text", "flagged"),
    [
        ({"product_name": "Máy 1.200W"}, "Máy 1.200W", False),
        ({"allowed_claims": ("Công suất 1200W",)}, "Máy 1.200W", True),
        ({"allowed_claims": ("Pin 15.000 mAh",)}, "Pin 5.000 mAh", True),
        ({"allowed_claims": ("Pin 5.000 mAh",)}, "Giá 5.000đ", True),
        ({"allowed_claims": ("Giá 299.000 ĐỒNG",)}, "Giá 299.000   đồng", False),
    ],
)
def test_price_exemption_requires_exact_surface(monkeypatch, facts, text, flagged):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert bool(check_price_claims(text, _ctx(**facts))) == flagged


@pytest.mark.parametrize("prefix", ["không", "không hề", "không phải", "chẳng", "không có"])
@pytest.mark.parametrize("copula", ["", "là "])
@pytest.mark.parametrize(
    ("claim", "negative"),
    [("an toàn", "độc hại"), ("chính hãng", "hàng giả"), ("mới 100%", "đã qua sử dụng")],
)
def test_negated_negative_is_consistent(monkeypatch, prefix, copula, claim, negative):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_contradictory_claims([claim, f"{prefix} {copula}{negative} cho bé."], _ctx()) == []


def test_contradiction_flag_off_and_on(monkeypatch):
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    segments = ["Chất liệu an toàn.", "Không độc hại cho bé."]
    assert [v.severity for v in check_contradictory_claims(segments, _ctx())] == [Severity.ERROR]
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_contradictory_claims(segments, _ctx()) == []
    assert [v.severity for v in check_contradictory_claims(["an toàn", "độc hại"], _ctx())] == [
        Severity.WARNING
    ]


def test_warranty_denial_and_exception_remain_distinct(monkeypatch):
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert [
        v.severity
        for v in check_contradictory_claims(["Bảo hành chính hãng", "Không bảo hành"], _ctx())
    ] == [Severity.WARNING]
    assert (
        check_contradictory_claims(["Bảo hành chính hãng", "Không bảo hành khi rơi vỡ"], _ctx())
        == []
    )


@pytest.mark.parametrize(
    ("approved", "spoken"), [("12 tháng", "1 năm"), ("1 năm", "12 tháng"), ("18 tháng", "1,5 năm")]
)
def test_years_and_months_are_equivalent_only_when_relaxed(monkeypatch, approved, spoken):
    context = _ctx(allowed_claims=(f"Bảo hành {approved}",))
    monkeypatch.delenv("SCRIPT_GATE_RELAXED_ENABLED", raising=False)
    assert any(
        v.severity == Severity.ERROR for v in check_factual_claims(f"Bảo hành {spoken}", context)
    )
    monkeypatch.setenv("SCRIPT_GATE_RELAXED_ENABLED", "1")
    assert check_factual_claims(f"Bảo hành {spoken}", context) == []
    assert any(
        v.severity == Severity.ERROR for v in check_factual_claims("Bảo hành 6 tháng", context)
    )


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        (["CLAIM_PRICE", "semantic_budget_exhausted"], "facts_mismatch"),
        (["REPETITION_LOCAL", "semantic_budget_exhausted"], "wording_unsuitable"),
        (["guard:coverage", "semantic_budget_exhausted"], "coverage_missing"),
        (["CLAIM_PRICE", "guard:truncated", "semantic_budget_exhausted"], "unfinished_text"),
        (["semantic_budget_exhausted"], "time_budget_exhausted"),
        (["???", "semantic_budget_exhausted"], "time_budget_exhausted"),
        (["CLAIM_PRICE", "llm_failed:TimeoutError"], "time_budget_exhausted"),
        (["CLAIM_PRICE", "llm_failed:LLMDeadlineError"], "time_budget_exhausted"),
    ],
)
def test_budget_cap_preserves_specific_reason(reasons, expected):
    assert fallback_reason_code(reasons) == expected


def test_budget_cap_preserves_specific_display_text():
    assert reason_text(["CLAIM_PRICE", "semantic_budget_exhausted"]) == reason_text(["CLAIM_PRICE"])
    assert reason_text(["REPETITION_LOCAL", "semantic_budget_exhausted"]) == reason_text(
        ["REPETITION_LOCAL"]
    )
    assert reason_text(["CLAIM_PRICE", "llm_failed:TimeoutError"]) == reason_text(
        ["llm_failed:TimeoutError"]
    )
