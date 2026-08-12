"""Table-driven rule tests (task 3.11): profanity + commerce claims.

Covers clean, blocked, brand-allowlist, teencode/obfuscation, factual-claim
mismatch, and false-positive cases for the PROFANITY and CLAIM families.
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.rules.commerce_claims import (
    check_discount_claims,
    check_factual_claims,
    check_identity_claims,
    check_price_claims,
)
from backend.application.script_authoring.gate.rules.profanity import (
    ProfanityLexicon,
    check_profanity,
    load_curated_lexicon,
)

_FACTS = ProductFacts(
    product_name="Kem ABC",
    prices=("299.000", "199.000"),
    discounts=("giảm 20%", "giảm 10%"),
    skus=("SKU-P004",),
    allowed_claims=("kem dưỡng ẩm sâu", "không chứa paraben"),
)


def _ctx(**overrides) -> ScriptGateContext:
    return ScriptGateContext(facts=_FACTS, **overrides)


# -- profanity -------------------------------------------------------------

_LEXICON = load_curated_lexicon()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # clean
        ("Kem ABC chỉ 299.000đ, giảm 20% nhé.", 0),
        # direct profanity
        ("cái dmm này", 1),
        # teencode (digit-for-letter substitution)
        ("sh1t thật", 1),
        # obfuscated with separators
        ("d.m.m", 1),
        ("d_m_m", 1),
    ],
)
def test_profanity_rules(text: str, expected: int) -> None:
    violations = check_profanity(text, _ctx(), _LEXICON)
    assert len(violations) == expected
    if expected:
        assert all(v.rule_id == "PROFANITY_OFFENSIVE" for v in violations)


def test_profanity_brand_allowlist() -> None:
    ctx = _ctx(brand_allowlist=("Saigon",))
    assert check_profanity("Bia Saigon vị đặc biệt", ctx, _LEXICON) == []


def test_profanity_lexicon_provenance() -> None:
    assert _LEXICON.version == "1"
    assert _LEXICON.license
    assert _LEXICON.source
    assert _LEXICON.curated_by


def test_profanity_lexicon_rejects_incomplete_provenance() -> None:
    with pytest.raises(ValueError, match="provenance"):
        ProfanityLexicon.from_resource({"words": ["x"]})


# -- commerce claims -------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_rules"),
    [
        # clean: all claims authorized
        ("Kem ABC giá 299.000đ, giảm 20% hôm nay.", []),
        # unverified price
        ("giá 99.000đ thôi", ["CLAIM_PRICE"]),
        ("giá 1.299.000đ", ["CLAIM_PRICE"]),
        # unverified discount
        ("giảm 50%", ["CLAIM_DISCOUNT"]),
        # bare percent warning (not tied to a discount verb)
        ("chỉ còn 15% hàng", ["CLAIM_DISCOUNT"]),
        # unverified SKU (dash form)
        ("mã SKU-X999", ["CLAIM_IDENTITY"]),
    ],
)
def test_claim_rules(text: str, expected_rules: list[str]) -> None:
    violations = (
        check_price_claims(text, _ctx())
        + check_discount_claims(text, _ctx())
        + check_identity_claims(text, _ctx())
    )
    assert sorted(v.rule_id for v in violations) == sorted(expected_rules)


def test_authorized_discount_bare_percent_not_double_flagged() -> None:
    # "giảm 20%" is authorized; the bare "20%" inside must not be flagged.
    assert check_discount_claims("giảm 20% hôm nay", _ctx()) == []


def test_factual_claim_authorized_and_unauthorized() -> None:
    ctx = _ctx()
    assert check_factual_claims("Kem này có kem dưỡng ẩm sâu.", ctx) == []
    violations = check_factual_claims("Kem này giúp trắng da 10 tông.", ctx)
    assert any(v.rule_id == "CLAIM_FACTUAL" for v in violations)


def test_price_sentence_not_double_flagged_as_claim() -> None:
    # A price-only sentence must not be re-flagged by the factual-claim
    # heuristic (price claims are covered by CLAIM_PRICE).
    ctx = _ctx()
    assert check_factual_claims("Kem ABC chỉ còn 299.000đ, giảm 20% nhé.", ctx) == []
