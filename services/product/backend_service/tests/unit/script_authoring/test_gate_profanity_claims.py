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
    # Task 3.6 completion: the curated resource carries the full provenance
    # block (version, source, license, curation) and every field is recorded.
    assert _LEXICON.version == "2"
    assert _LEXICON.license
    assert _LEXICON.source
    assert _LEXICON.curated_by


def test_profanity_lexicon_activation_guard() -> None:
    # Task 3.6 activation guard: a lexicon resource whose provenance is
    # incomplete OR not marked active is refused for runtime use — an
    # external dataset-derived lexicon can only be activated after human
    # curation + false-positive review are recorded.
    with pytest.raises(ValueError, match="provenance"):
        ProfanityLexicon.from_resource({"words": ["x"]})
    inactive = {
        "provenance": {
            "version": "9",
            "source": "s",
            "license": "l",
            "curated_by": "c",
            "activation_status": "draft",
        },
        "words": ["dmm"],
    }
    with pytest.raises(ValueError, match="not activated"):
        ProfanityLexicon.from_resource(inactive)


def test_profanity_lexicon_false_positive_review() -> None:
    # Task 3.6: the activated resource records where the false-positive
    # review lives, and the review actually passes — common Vietnamese
    # market/beauty words must never trip the curated lexicon, while the
    # curated entries (including diacritic forms) still match.
    from backend.application.script_authoring.gate.rules.profanity import load_curated_lexicon

    import json
    from pathlib import Path

    resource_path = Path(
        Path(__file__).resolve().parents[3] / "resources" / "profanity" / "curated_lexicon_v2.json"
    )
    resource = json.loads(resource_path.read_text(encoding="utf-8"))
    review = resource["provenance"].get("false_positive_review", "")
    assert "test_gate_profanity" in review, "false-positive review must be recorded in provenance"

    lexicon = load_curated_lexicon(resource_path)
    clean_words = [
        "kem",
        "dưỡng",
        "mềm",
        "mại",
        "trắng",
        "da",
        "sáng",
        "đều",
        "màu",
        "các",
        "đi",
        "điểm",
        "buổi",
        "dẻo",
        "lon",
        "cá",
        "má",
        "con",
        "mua",
        "ngay",
        "chị",
        "em",
        "đẹp",
        "xinh",
        "thơm",
    ]
    for word in clean_words:
        assert not lexicon.is_offensive(word), f"false positive: {word!r}"

    offensive = [
        "dmm",
        "d.m.m",
        "sh1t",
        "b1tch",
        "vcl",
        "loz",
        "đm",
        "lồn",
        "địt",
        "cặc",
        "cặk",
        "chịch",
        "đéo",
        "đụ",
        "đĩ",
        "điếm",
        "buồi",
        "fuck",
        "địt mẹ",
    ]
    for word in offensive:
        assert lexicon.is_offensive(word), f"missed offensive word: {word!r}"


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
    violations = check_factual_claims("Kem này làm trắng da 10 tông.", ctx)
    assert any(v.rule_id == "CLAIM_FACTUAL" for v in violations)


def test_factual_claim_scene_setting_not_a_claim() -> None:
    # Scene-setting/transition sentences reuse common Vietnamese verbs
    # ("làm", "chứa", "tăng", "giúp", "có") without a benefit signal — they
    # are not product claims and must never be flagged (15.4 real-LLM E2E
    # redesign: the old single-verb substring trigger rejected natural prose
    # every run and blocked REVIEWABLE deterministically).
    ctx = _ctx()
    assert check_factual_claims("Mỗi khi đi làm về mệt mỏi, bạn chỉ việc rót nước.", ctx) == []
    assert check_factual_claims("Đừng tích trữ bình chứa cồng kềnh trong bếp.", ctx) == []
    assert check_factual_claims("Nhu cầu giải khát tăng cao vào ngày oi bức.", ctx) == []
    assert check_factual_claims("Kem này giúp da mềm mại mỗi ngày.", ctx) == []
    assert check_factual_claims("Thiết bị này giúp gian bếp gọn gàng.", ctx) == []


def test_factual_claim_scene_setting_prose_passes_without_claim_signal() -> None:
    # Pure scene-setting/transition prose carries no claim signal, so it is
    # never a claim and is never flagged (15.4 real-LLM E2E). Signal-free
    # prose about the home/space stays safe under the product-agnostic rule.
    ctx = _ctx()
    assert check_factual_claims("Không gian sống hiện đại thoáng đãng cho cả gia đình.", ctx) == []
    assert (
        check_factual_claims(
            "Việc bài trí các vật dụng khoa học giúp căn nhà luôn ngăn nắp và rộng rãi.", ctx
        )
        == []
    )
    assert (
        check_factual_claims(
            "Bạn sẽ thấy mọi góc nhỏ trong căn nhà đều được sắp xếp gọn gàng.", ctx
        )
        == []
    )


def test_factual_claim_signal_bearing_unauthorized_prose_fails() -> None:
    # Reviewer HIGH C: the OLD product-reference guard skipped any sentence
    # that did not name a hardcoded product noun, letting unsupported claims
    # escape when their nouns fell outside that vocabulary. A signal-bearing
    # clause that is not authorized by an allowed claim is now flagged
    # regardless of product wording — product-agnostic, no noun enumeration.
    ctx = _ctx()
    assert any(
        v.rule_id == "CLAIM_FACTUAL"
        for v in check_factual_claims("Không gian sống hiện đại được tối ưu diện tích.", ctx)
    )


# --- CLAIM_FACTUAL product-agnostic, clause-level (reviewer R9.3) -------------

_PARAPHRASE_FACTS = ProductFacts(
    product_name="Máy lọc nước NanoFresh",
    allowed_claims=(
        "thiết kế gọn nhẹ",
        "bộ lọc loại bỏ tạp chất",
        "không dùng điện trong quá trình lọc",
    ),
)


def _paraphrase_ctx() -> ScriptGateContext:
    return ScriptGateContext(facts=_PARAPHRASE_FACTS)


def test_claim_factual_supported_paraphrase_passes() -> None:
    # Reviewer test 1: a natural paraphrase of an allowed claim is authorized.
    ctx = _paraphrase_ctx()
    assert (
        check_factual_claims("Thiết bị này thiết kế tinh tế, gọn gàng cho mọi không gian.", ctx)
        == []
    )


def test_claim_factual_unsupported_claim_without_product_noun_fails() -> None:
    # Reviewer test 2: an unsupported claim escapes the OLD hardcoded
    # product-noun vocabulary when its nouns belong to another category.
    # The product-agnostic gate must flag it regardless of wording.
    ctx = _paraphrase_ctx()
    violations = check_factual_claims(
        "Khả năng hút bụi siêu mạnh với công suất 1500W mỗi lần sử dụng.", ctx
    )
    assert any(v.rule_id == "CLAIM_FACTUAL" for v in violations)


def test_claim_factual_supported_fragment_does_not_authorize_appendage() -> None:
    # Reviewer test 3: the supported fragment must not authorize an invented
    # factual extension in the same sentence ("thiết kế gọn nhẹ và bảo hành
    # 10 năm" — the warranty claim is not in the allowed set).
    ctx = _paraphrase_ctx()
    violations = check_factual_claims("Thiết kế gọn nhẹ và bảo hành 10 năm.", ctx)
    assert any(v.rule_id == "CLAIM_FACTUAL" for v in violations)


def test_claim_factual_invented_spec_warranty_performance_fails() -> None:
    # Reviewer test 4: invented numeric/spec/warranty/performance claims fail.
    ctx = _paraphrase_ctx()
    for claim in (
        "Công suất lọc lên tới 500 lít mỗi giờ.",
        "Sản phẩm được bảo hành tới 5 năm.",
        "Tuổi thọ lõi lọc lên tới 10 năm.",
        "Động cơ vận hành êm ái, tiết kiệm điện đến 90%.",
    ):
        assert any(v.rule_id == "CLAIM_FACTUAL" for v in check_factual_claims(claim, ctx)), (
            f"unsupported claim not flagged: {claim!r}"
        )


def test_claim_factual_verdict_independent_of_product_name() -> None:
    # Reviewer test 5: correctness does not depend on the live-test product
    # name/category — the same supported/unsupported pair behaves identically
    # for a water filter and a skincare product.
    for facts in (
        _PARAPHRASE_FACTS,
        ProductFacts(product_name="Kem ABC", allowed_claims=_PARAPHRASE_FACTS.allowed_claims),
    ):
        ctx = ScriptGateContext(facts=facts)
        assert check_factual_claims("Thiết kế gọn nhẹ cho mọi không gian.", ctx) == []
        assert any(
            v.rule_id == "CLAIM_FACTUAL"
            for v in check_factual_claims("Thiết kế gọn nhẹ và bảo hành 10 năm.", ctx)
        )


def test_factual_claim_benefit_signal_without_allowed_claim() -> None:
    # A sentence asserting a specific product benefit/capability that is NOT
    # in the allowed set is an unsupported claim and is flagged.
    ctx = _ctx()
    assert any(
        v.rule_id == "CLAIM_FACTUAL"
        for v in check_factual_claims("Máy này công suất lọc 500 lít mỗi giờ.", ctx)
    )
    assert any(
        v.rule_id == "CLAIM_FACTUAL"
        for v in check_factual_claims("Kem này bảo hành lên tới 5 năm.", ctx)
    )
    # A benefit sentence carrying an allowed claim substring passes.
    assert check_factual_claims("Kem này không chứa paraben nên an toàn.", ctx) == []


def test_price_sentence_not_double_flagged_as_claim() -> None:
    # A price-only sentence must not be re-flagged by the factual-claim
    # heuristic (price claims are covered by CLAIM_PRICE).
    ctx = _ctx()
    assert check_factual_claims("Kem ABC chỉ còn 299.000đ, giảm 20% nhé.", ctx) == []
