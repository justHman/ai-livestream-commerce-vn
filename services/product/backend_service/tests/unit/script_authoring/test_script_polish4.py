"""Polish 2026-11-11 (4): gate false alarms, hype adjectives, repeated greeting in the intro."""

from __future__ import annotations

import pytest

from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.rules.commerce_claims import check_identity_claims
from backend.application.script_authoring.gate.rules.full_script import check_contradictory_claims
from backend.application.script_authoring.generation.ordered_units import (
    UnitSpec,
    check_unit_text,
    salvage_unit_text,
)

_SPEC = UnitSpec("highlight", claims=("Giao hàng nhanh trong ngày", "Máy nhẹ, cầm một tay"))


@pytest.mark.parametrize(
    "tail",
    [
        "Không bảo hành pin phồng do để nơi nóng.",
        "Không bảo hành đối với hàng rơi vỡ.",
        "Không bảo hành khi rơi vỡ.",
        "Không bảo hành đối với hàng đã qua sử dụng.",
        "Không bảo hành vì lỗi người dùng.",
    ],
)
def test_warranty_exclusion_with_a_cause_is_a_condition(tail):
    assert check_contradictory_claims(["Bảo hành 12 tháng.", tail], ScriptGateContext()) == []


def test_bare_warranty_denial_is_still_a_contradiction():
    segments = ["Bảo hành 12 tháng.", "Sản phẩm không bảo hành."]
    assert check_contradictory_claims(segments, ScriptGateContext()) != []


def test_code_in_product_name_is_not_a_foreign_identity_without_skus():
    ctx = ScriptGateContext(facts=ProductFacts(product_name="Gạo ST25 túi 5kg"))
    assert check_identity_claims("Gạo ST25 hạt dài.", ctx) == []


def test_code_absent_from_name_claims_and_skus_is_still_flagged():
    ctx = ScriptGateContext(facts=ProductFacts(product_name="Gạo ST25 túi 5kg"))
    assert [v.rule_id for v in check_identity_claims("Mã ABC123 nhé.", ctx)] == ["CLAIM_IDENTITY"]


@pytest.mark.parametrize(
    "text",
    [
        "Shop rất uy tín nhé.",
        "Sạc cực nhanh cho mọi người.",
        "Máy siêu nhẹ cầm một tay.",
        "Pin trâu dùng cả ngày.",
        "Mức giá tốt cho mọi người.",
        "Cam kết chất lượng ổn định.",
    ],
)
def test_hype_wording_the_owner_never_wrote_is_rejected(text):
    assert check_unit_text(text, spec=_SPEC, approved=_SPEC.claims)[1] == "hard_sell"


def test_plain_nhanh_and_nhe_are_not_blocked():
    text = "Giao hàng nhanh trong ngày. Máy nhẹ, cầm một tay."
    assert check_unit_text(text, spec=_SPEC, approved=_SPEC.claims)[1] is None


def test_hype_the_owner_wrote_is_allowed():
    approved = (*_SPEC.claims, "Pin trâu dùng cả ngày")
    assert check_unit_text("Pin trâu dùng cả ngày.", spec=_SPEC, approved=approved)[1] is None


def test_hype_sentence_is_cut_and_the_rest_kept():
    raw = "Shop rất uy tín nhé. Giao hàng nhanh trong ngày. Máy nhẹ, cầm một tay."
    out = salvage_unit_text(raw, "hard_sell", spec=_SPEC, approved=_SPEC.claims)
    assert out == "Giao hàng nhanh trong ngày. Máy nhẹ, cầm một tay."


_INTRO = UnitSpec("intro", claims=("Hạt dài thơm",))
_NAME = "Gạo ST25"


def test_intro_that_greets_again_is_rejected():
    raw = "Chào mọi người, mình giới thiệu gạo ST25 hạt dài thơm."
    got = check_unit_text(raw, spec=_INTRO, approved=_INTRO.claims, product_name=_NAME)
    assert got[1] == "greeting"


def test_greeting_clause_is_cut_and_the_name_kept():
    raw = "Xin chào cả nhà, mình giới thiệu gạo ST25 hạt dài thơm."
    kw = {"spec": _INTRO, "approved": _INTRO.claims, "product_name": _NAME}
    assert salvage_unit_text(raw, "greeting", **kw) == "Mình giới thiệu gạo ST25 hạt dài thơm."


def test_intro_without_greeting_passes():
    raw = "Mình giới thiệu gạo ST25 hạt dài thơm."
    got = check_unit_text(raw, spec=_INTRO, approved=_INTRO.claims, product_name=_NAME)
    assert got[1] is None


@pytest.mark.parametrize(
    "tail",
    [
        "Không bảo hành với mọi sản phẩm.",
        "Sản phẩm không bảo hành, liên hệ với shop nhé.",
        "Không bảo hành, mua với giá tốt.",
    ],
)
def test_loose_exception_words_do_not_hide_a_real_contradiction(tail):
    assert check_contradictory_claims(["Bảo hành 12 tháng.", tail], ScriptGateContext()) != []


def test_hype_words_inside_the_owner_product_name_or_brand_are_allowed():
    spec = UnitSpec("highlight", claims=("Dùng cả ngày",))
    got = check_unit_text(
        "Máy siêu nhẹ này dùng cả ngày.",
        spec=spec,
        approved=spec.claims,
        product_name="Máy siêu nhẹ X",
    )
    assert got[1] is None
    opening = UnitSpec("opening")
    got = check_unit_text("Uy Tín Store xin chào mọi người.", spec=opening, brand="Uy Tín Store")
    assert got[1] is None


def test_hype_next_to_an_owner_named_product_is_still_cut():
    spec = UnitSpec("highlight", claims=("Dùng cả ngày",))
    got = check_unit_text(
        "Máy siêu nhẹ này pin trâu.", spec=spec, approved=spec.claims, product_name="Máy siêu nhẹ X"
    )
    assert got[1] == "hard_sell"


def test_product_name_starting_with_chao_is_not_a_greeting():
    spec = UnitSpec("intro", claims=("Hót hay",))
    got = check_unit_text(
        "Chào Mào hót hay lắm.", spec=spec, approved=spec.claims, product_name="Chào Mào"
    )
    assert got[1] is None
