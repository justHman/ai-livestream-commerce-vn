"""Brand name in the script (display) and its owner-given pronunciation (spoken / TTS)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.application.script_authoring.approved_speech import ApprovedProduct
from backend.application.script_authoring.compile import (
    apply_brand_pronunciation,
    compile_spoken_text,
)
from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    SessionBrief,
    UnitSpec,
    build_unit_prompt,
    check_unit_text,
    retry_hint,
)
from backend.application.script_authoring.generation.unit_brief import fallback_reason_code
from backend.application.script_authoring.service_impl import _brand_allowlist

BRAND = {"Livento": "Lai-ven-to"}
PRODUCT = ProductBrief("P1", "Áo thun basic", brand="Livento", brand_pronunciation="Lai-ven-to")
SPECS = [UnitSpec("intro"), UnitSpec("highlight", claims=("Vải cotton mềm.",)), UnitSpec("offer")]


def test_brand_is_spoken_with_its_pronunciation_and_hyphens_become_spaces() -> None:
    text = compile_spoken_text("Mẫu này của Livento nhé.", brand_map=BRAND).spoken_text
    assert text == "Mẫu này của Lai ven to nhé."


def test_brand_match_ignores_case() -> None:
    assert (
        apply_brand_pronunciation("áo livento đây", "Livento", "Lai-ven-to") == "áo Lai ven to đây"
    )


def test_brand_match_ignores_diacritics() -> None:
    assert apply_brand_pronunciation("Đồ của Viet Tien nhé", "Việt Tiến", "Vi-ệt Ti-ến") == (
        "Đồ của Vi ệt Ti ến nhé"
    )


def test_brand_inside_another_word_is_untouched() -> None:
    text = "Liventos và xLivento"
    assert apply_brand_pronunciation(text, "Livento", "Lai-ven-to") == text


def test_all_caps_brand_is_not_spelled_letter_by_letter() -> None:
    text = compile_spoken_text("LIVENTO chào mọi người.", brand_map=BRAND).spoken_text
    assert text == "Lai ven to chào mọi người."


def test_compiling_with_a_brand_is_idempotent() -> None:
    once = compile_spoken_text("Livento giá 199.000đ.", brand_map=BRAND).spoken_text
    assert compile_spoken_text(once, brand_map=BRAND).spoken_text == once


def test_empty_brand_map_changes_nothing() -> None:
    text = "Livento giá 199.000đ."
    assert compile_spoken_text(text, brand_map={}) == compile_spoken_text(text)


def test_short_single_word_brand_needs_exact_case() -> None:
    assert apply_brand_pronunciation("mai mình live tiếp", "Mai", "Mai-i") == "mai mình live tiếp"


def test_pronunciation_containing_the_brand_is_ignored() -> None:
    assert apply_brand_pronunciation("Livento đây", "Livento", "Livento-oo") == "Livento đây"


def test_intro_is_invited_to_say_the_brand_in_real_spelling() -> None:
    prompt = build_unit_prompt(SessionBrief(), PRODUCT, SPECS, 0)
    assert "Nhắc tên thương hiệu đúng chính tả 'Livento'" in prompt


def test_highlight_is_told_not_to_say_the_brand() -> None:
    prompt = build_unit_prompt(SessionBrief(), PRODUCT, SPECS, 1)
    assert "Không nhắc tên thương hiệu trong phần này." in prompt


def test_offer_right_after_the_intro_is_not_invited() -> None:
    prompt = build_unit_prompt(SessionBrief(), PRODUCT, [UnitSpec("intro"), UnitSpec("offer")], 1)
    assert "Không nhắc tên thương hiệu trong phần này." in prompt


def test_the_pronunciation_never_reaches_the_model() -> None:
    prompts = [build_unit_prompt(SessionBrief(), PRODUCT, SPECS, i) for i in range(len(SPECS))]
    assert not any("Lai-ven-to" in p or "Lai ven to" in p for p in prompts)


def test_a_product_without_brand_gets_no_brand_line() -> None:
    prompt = build_unit_prompt(SessionBrief(), ProductBrief("P1", "Áo thun basic"), SPECS, 1)
    assert "thương hiệu" not in prompt.split("Tên sản phẩm:")[1]


@pytest.mark.parametrize(
    "text, role, nearby",
    [
        ("Mẫu áo Livento này mặc rất mát nhé.", "highlight", 0),
        ("Livento mang đến mẫu áo mới, Livento rất chăm chút nhé.", "intro", 0),
        ("Mẫu áo của Livento giá rất dễ chịu nhé.", "offer", 1),
    ],
)
def test_brand_said_in_the_wrong_place_or_too_often_is_rejected(text, role, nearby) -> None:
    assert check_unit_text(
        text, spec=UnitSpec(role), brand="Livento", brand_used_nearby=nearby
    ) == (None, "brand_repeat")


def test_brand_said_once_in_the_intro_is_kept() -> None:
    text = "Hôm nay mình giới thiệu mẫu áo của Livento nhé."
    assert check_unit_text(text, spec=UnitSpec("intro"), brand="Livento")[0] == text


def test_a_missing_brand_is_never_a_rejection() -> None:
    text = "Hôm nay mình giới thiệu một mẫu áo thật xinh nhé."
    assert check_unit_text(text, spec=UnitSpec("intro"), brand="Livento")[0] == text


def test_the_brand_inside_the_product_name_is_not_a_brand_mention() -> None:
    text = "Áo thun Livento mặc rất mát nhé."
    kwargs = {"spec": UnitSpec("highlight"), "product_name": "Áo thun Livento"}
    assert check_unit_text(text, brand="Livento", **kwargs)[0] == text


def test_brand_repeat_has_its_own_retry_hint() -> None:
    assert "thương hiệu" in retry_hint("brand_repeat", "")


def test_brand_repeat_is_a_wording_fallback_reason() -> None:
    assert fallback_reason_code(["guard:brand_repeat"]) == "wording_unsuitable"


def test_approved_answers_speak_the_brand_pronunciation() -> None:
    facts = {
        "product_name": "Áo thun Livento",
        "prices": ["199000 VND"],
        "product_info": {"brand": "Livento", "brand_pronunciation": "Lai-ven-to"},
    }
    product = ApprovedProduct(
        "p1", "v1", "Áo giá một trăm chín mươi chín nghìn đồng nhé.", "h", json.dumps(facts)
    )
    assert product.answers() == (
        "Áo thun Lai ven to giá một trăm chín mươi chín nghìn đồng nhé mọi người.",
    )


def test_gate_allowlist_carries_brand_and_pronunciation_words() -> None:
    brief = SimpleNamespace(
        product_facts={
            "P1": {"product_info": {"brand": "Livento", "brand_pronunciation": "Lai-ven-to"}}
        }
    )
    assert _brand_allowlist(brief, "P1") == ("Livento", "Lai", "ven")
