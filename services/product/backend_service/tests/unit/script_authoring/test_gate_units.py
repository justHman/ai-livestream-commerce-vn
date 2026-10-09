"""Per-unit factual gate on manual/approved text and currency normalisation."""

from __future__ import annotations

import pytest

from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.engine import (
    ScriptGate,
    default_full_script_rules,
    default_segment_rules,
)
from backend.application.script_authoring.gate.registry import ScriptRuleRegistry
from backend.application.script_authoring.gate.rules.commerce_claims import (
    _vnd_amount,
    check_price_claims,
)
from backend.application.script_authoring.units import split_units

FACTS = ProductFacts(product_name="Kem ABC", prices=("299.000đ",), allowed_claims=())


def _gate() -> ScriptGate:
    seg, full = default_segment_rules(), default_full_script_rules()
    return ScriptGate(ScriptRuleRegistry([*seg.rules, *full.rules]), seg, full)


def _ctx() -> ScriptGateContext:
    return ScriptGateContext(facts=FACTS, total_min_seconds=1.0)


def test_split_units_is_exact_paragraphs() -> None:
    text = "Một.\n\nHai.  \n \nBa.\r\n\r\nBốn."
    assert split_units(text) == ("Một.", "Hai.", "Ba.", "Bốn.")
    assert split_units("Chỉ một đoạn.\nvẫn cùng đoạn.") == ("Chỉ một đoạn.\nvẫn cùng đoạn.",)


@pytest.mark.parametrize(
    "written, amount",
    [
        ("299.000đ", 299000),
        ("299,000 đồng", 299000),
        ("299000 VND", 299000),
        ("299k", 299000),
        ("2,5 triệu", 2500000),
        ("2 triệu", 2000000),
        ("giảm 20%", None),
    ],
)
def test_vnd_amount_normalises_currency_forms(written, amount) -> None:
    assert _vnd_amount(written) == amount


def test_price_written_differently_but_equal_is_not_flagged() -> None:
    assert check_price_claims("Giá chỉ 299k thôi nhé.", _ctx()) == []
    assert check_price_claims("Chỉ 299.000 đồng.", _ctx()) == []


def test_wrong_price_is_still_flagged() -> None:
    assert check_price_claims("Giá chỉ 199k thôi nhé.", _ctx())


def test_factual_rule_runs_on_each_manual_unit_and_names_the_unit() -> None:
    manual = "Chào cả nhà, mình giới thiệu Kem ABC.\n\nGiá chỉ 199.000đ hôm nay."
    result = _gate().run_full_script([manual], _ctx())
    errors = [v for v in result.violations if v.rule_id == "CLAIM_PRICE"]
    assert [v.segment_index for v in errors] == [1]
    assert not result.passed


def test_clean_manual_units_pass() -> None:
    manual = "Chào cả nhà, mình giới thiệu Kem ABC.\n\nGiá chỉ 299.000đ hôm nay."
    assert _gate().run_full_script([manual], _ctx()).passed


@pytest.mark.parametrize(
    "written", ["299.000k", "299.000 nghìn", "2.5", "299k đồng", "1.000 triệu"]
)
def test_ambiguous_prices_are_unparseable(written) -> None:
    assert _vnd_amount(written) is None


def test_multiplier_suffix_cannot_launder_a_price() -> None:
    facts = ProductFacts(prices=("299000 VND",))
    ctx = ScriptGateContext(facts=facts)
    assert check_price_claims("Giá 299.000k hôm nay.", ctx)  # not 299000
    assert check_price_claims("Giá 199000 VND hôm nay.", ctx)  # undetected before
    assert check_price_claims("Giá 299k.", ctx) == []
    assert check_price_claims("Giá 299.000đ.", ctx) == []
    assert check_price_claims("Giá 299.000 VND.", ctx) == []


NUM_FACTS = ProductFacts(
    product_name="Kem ABC",
    prices=("299000 VND",),
    discounts=("giảm 20%",),
    allowed_claims=("Bảo hành 1 năm.", "Dung tích 50 ml."),
)


def _numeric(text):
    from backend.application.script_authoring.gate.rules.commerce_claims import (
        check_factual_claims,
    )

    return check_factual_claims(text, ScriptGateContext(facts=NUM_FACTS))


@pytest.mark.parametrize(
    "text",
    [
        "Bảo hành 100 năm cho cả nhà.",
        "Sản phẩm bảo hành 100 luôn.",
        "Dung tích tới 500 ml nhé.",
        "Đạt 98% khách hàng hài lòng.",
        "Dùng liên tục 30 ngày là thấy hiệu quả.",
    ],
)
def test_unapproved_numbers_in_claims_fail_closed(text) -> None:
    assert [v for v in _numeric(text) if "Number" in v.message]


@pytest.mark.parametrize(
    "text",
    [
        "Bảo hành 1 năm cho cả nhà.",
        "Dung tích 50 ml dùng rất tiện.",
        "Hôm nay shop có 2 món cho cả nhà.",
        "Món thứ 3 là Kem ABC nhé.",
        "Giá chỉ 299k thôi.",
        "Giảm 20% chỉ trong hôm nay.",
        "Mình giới thiệu top 3 sản phẩm đây.",
    ],
)
def test_harmless_or_approved_numbers_are_not_flagged(text) -> None:
    assert not [v for v in _numeric(text) if "Number" in v.message]


@pytest.mark.parametrize("written", ["299.000kđ", "299.000 k đ", "299.000 k", "299.000k₫"])
def test_attached_multiplier_variants_are_priced_closed(written) -> None:
    facts = ProductFacts(prices=("299000 VND",))
    assert check_price_claims(f"Giá {written}.", ScriptGateContext(facts=facts))


def test_attached_multiplier_with_plain_number_is_a_known_price() -> None:
    facts = ProductFacts(prices=("299000 VND",))
    assert check_price_claims("Giá 299kđ nhé.", ScriptGateContext(facts=facts)) == []
