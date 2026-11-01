"""Brand name in the script (display) and its owner-given pronunciation (spoken / TTS)."""

from __future__ import annotations

import json
import unicodedata
from types import SimpleNamespace

import pytest

from backend.application.script_authoring.approved_speech import ApprovedProduct, ApprovedSpeech
from backend.application.script_authoring.compile import (
    apply_brand_pronunciation,
    compile_spoken_text,
)
from backend.application.script_authoring.gate.context import ScriptGateContext
from backend.application.script_authoring.gate.rules.profanity import (
    check_profanity,
    load_curated_lexicon,
)
from backend.application.script_authoring.gate.rules.vietnamese import check_common_spelling
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


def test_one_word_brand_in_other_case_is_not_the_brand() -> None:
    assert apply_brand_pronunciation("áo livento đây", "Livento", "Lai-ven-to") == "áo livento đây"


def test_multi_word_brand_match_ignores_case_and_diacritics() -> None:
    assert apply_brand_pronunciation("Đồ của viet tien nhé", "Việt Tiến", "Vi-ệt Ti-ến") == (
        "Đồ của Vi ệt Ti ến nhé"
    )


def test_ordinary_lowercase_word_is_not_substituted() -> None:
    assert apply_brand_pronunciation("Mẫu áo rất xinh.", "Xinh", "Xi-nh") == "Mẫu áo rất xinh."


def test_capitalized_one_word_brand_is_substituted() -> None:
    assert apply_brand_pronunciation("Áo Xinh rất đẹp.", "Xinh", "Xi-nh") == "Áo Xi nh rất đẹp."


def test_currency_word_is_not_a_brand_called_dong() -> None:
    text = "Giá một trăm nghìn đồng, combo hai trăm nghìn đồng nhé."
    assert apply_brand_pronunciation(text, "Đồng", "Đông-ng") == text


def test_brand_called_dong_is_substituted_when_written_as_the_brand() -> None:
    assert apply_brand_pronunciation("Nhà Đồng có mẫu mới.", "Đồng", "Đông-ng") == (
        "Nhà Đông ng có mẫu mới."
    )


def _nfd(text: str) -> str:
    return unicodedata.normalize("NFD", text)


def test_nfd_brand_matches_nfc_text() -> None:
    assert apply_brand_pronunciation("Hàng Mỹ đây", _nfd("Mỹ"), "Mi-i") == "Hàng Mi i đây"


def test_nfc_brand_matches_nfd_text() -> None:
    assert apply_brand_pronunciation(_nfd("Hàng Mỹ đây"), "Mỹ", "Mi-i") == "Hàng Mi i đây"


def test_nfd_short_brand_still_needs_exact_case() -> None:
    assert apply_brand_pronunciation("đỗ xe ở đây", _nfd("Đỗ"), "Đô-ô") == "đỗ xe ở đây"


def test_nfd_short_brand_matches_its_own_spelling() -> None:
    assert apply_brand_pronunciation("Nhà Đỗ đây", _nfd("Đỗ"), "Đô-ô") == "Nhà Đô ô đây"


@pytest.mark.parametrize(
    "text, brand, pronunciation",
    [
        ("Lai Lai Lai", "Lai Lai", "Lai"),
        ("Bo Bo Bo Bo", "Bo Bo", "Bo"),
        ("Ana Ana", "Ana", "Ana-Ana"),
        ("Kem Ka Ka nhé", "Ka Ka", "Ka"),
    ],
)
def test_substitution_is_a_fixed_point_even_when_the_pronunciation_overlaps(
    text, brand, pronunciation
) -> None:
    once = apply_brand_pronunciation(text, brand, pronunciation)
    assert apply_brand_pronunciation(once, brand, pronunciation) == once


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


def test_lowercase_ordinary_word_is_not_a_brand_mention() -> None:
    text = "Mẫu áo rất xinh, mặc lên rất xinh nhé."
    assert check_unit_text(text, spec=UnitSpec("highlight"), brand="Xinh")[0] == text


def test_currency_words_are_not_mentions_of_a_brand_called_dong() -> None:
    text = "Giá một trăm nghìn đồng, combo hai trăm nghìn đồng nhé."
    assert check_unit_text(text, spec=UnitSpec("offer"), brand="Đồng")[0] == text


def test_capitalized_one_word_brand_in_a_highlight_is_rejected() -> None:
    text = "Mẫu áo của Xinh mặc rất mát nhé."
    assert check_unit_text(text, spec=UnitSpec("highlight"), brand="Xinh") == (None, "brand_repeat")


BRANDED_FACTS = {
    "product_name": "Áo thun basic",
    "prices": ["199000 VND"],
    "allowed_claims": ["Livento dùng vải cotton thoáng mát."],
    "product_info": {"brand": "Livento", "brand_pronunciation": "Lai-ven-to"},
}
# The approved script as stored: its spoken units carry the pronunciation.
BRANDED_SCRIPT = (
    "Lai ven to dùng vải cotton thoáng mát nhé.\n\nÁo giá một trăm chín mươi chín nghìn đồng nhé."
)


def test_approved_answers_keep_the_brand_spelling_for_model_prompts() -> None:
    product = ApprovedProduct("p1", "v1", BRANDED_SCRIPT, "h", json.dumps(BRANDED_FACTS))
    assert "Livento dùng vải cotton thoáng mát." in product.answers()


def test_a_claim_with_the_brand_is_found_in_a_pronounced_script() -> None:
    facts = {**BRANDED_FACTS, "allowed_claims": ["Việt Tiến dùng vải cotton."]}
    facts["product_info"] = {"brand": "Việt Tiến", "brand_pronunciation": "Vi-ệt Ti-ến"}
    product = ApprovedProduct("p1", "v1", "Vi ệt Ti ến dùng vải cotton.", "h", json.dumps(facts))
    assert product.answers() == ("Việt Tiến dùng vải cotton.",)


def test_the_spoken_answer_carries_the_pronunciation() -> None:
    product = ApprovedProduct("p1", "v1", BRANDED_SCRIPT, "h", json.dumps(BRANDED_FACTS))
    assert product.speak("Livento dùng vải cotton thoáng mát.") == (
        "Lai ven to dùng vải cotton thoáng mát."
    )


class _RecordingLlm:
    name = "controlled"

    def __init__(self) -> None:
        self.requests: list[str] = []

    def stream_chunks(self, request, **kwargs):
        sent = json.dumps(request.messages, ensure_ascii=False)
        self.requests.append(sent)
        judge = "giám khảo" in sent
        yield SimpleNamespace(text="OK" if judge else "Livento dùng vải cotton thoáng mát nhé.")


def _speech(monkeypatch, facts=BRANDED_FACTS) -> ApprovedSpeech:
    product = ApprovedProduct(
        "p1", "v1", BRANDED_SCRIPT, "h", json.dumps(facts, ensure_ascii=False)
    )
    envelope = SimpleNamespace(product=lambda pid: product, products=(product,))

    async def resolve(self, session_id):
        return envelope

    async def revalidate(self, speech, live=None):
        return None

    monkeypatch.setattr(ApprovedSpeech, "resolve", resolve)
    monkeypatch.setattr(ApprovedSpeech, "revalidate", revalidate)
    return ApprovedSpeech(None, lambda: None)


async def _answer(monkeypatch, llm):
    question = "áo dùng vải gì vậy"
    return await _speech(monkeypatch).prepare(
        "s1",
        question,
        llm=llm,
        generate=True,
        product_id="p1",
        route="director",
        question_text=question,
    )


async def _replay(speech: ApprovedSpeech, text: str):
    return await speech.prepare("s1", text, generate=False, product_id="p1", route="director")


@pytest.mark.asyncio
async def test_accepted_display_answer_is_spoken_with_the_pronunciation(monkeypatch) -> None:
    speech = await _replay(_speech(monkeypatch), "Livento dùng vải cotton thoáng mát.")
    assert speech.text == "Lai ven to dùng vải cotton thoáng mát."


@pytest.mark.asyncio
async def test_approved_unit_already_in_spoken_form_is_unchanged(monkeypatch) -> None:
    unit = BRANDED_SCRIPT.split("\n\n")[0]
    speech = await _replay(_speech(monkeypatch), unit)
    assert speech.text == unit


@pytest.mark.asyncio
async def test_answer_without_a_configured_pronunciation_keeps_its_bytes(monkeypatch) -> None:
    facts = {**BRANDED_FACTS, "product_info": {"brand": "Livento"}}
    speech = await _replay(_speech(monkeypatch, facts), "Livento dùng vải cotton thoáng mát.")
    assert speech.text == "Livento dùng vải cotton thoáng mát."


def test_private_use_characters_typed_in_a_claim_are_kept() -> None:
    claim = "LIVENTO cotton  ."
    facts = {**BRANDED_FACTS, "allowed_claims": [claim]}
    product = ApprovedProduct("p1", "v1", BRANDED_SCRIPT, "h", json.dumps(facts))
    assert claim in product.answers()


@pytest.mark.parametrize("brand", ["Livento", "LIVENTO"])
def test_an_all_caps_brand_in_a_claim_survives_answer_compilation(brand) -> None:
    facts = {
        **BRANDED_FACTS,
        "allowed_claims": ["LIVENTO dùng vải cotton thoáng mát."],
        "product_info": {"brand": brand, "brand_pronunciation": "Lai-ven-to"},
    }
    product = ApprovedProduct("p1", "v1", BRANDED_SCRIPT, "h", json.dumps(facts))
    spoken = [product.speak(a) for a in product.answers()]
    assert "Lai ven to dùng vải cotton thoáng mát." in spoken


@pytest.mark.asyncio
async def test_model_requests_carry_the_brand_never_the_pronunciation(monkeypatch) -> None:
    llm = _RecordingLlm()
    await _answer(monkeypatch, llm)
    sent = " ".join(llm.requests)
    assert "Livento" in sent and "Lai ven to" not in sent and "Lai-ven-to" not in sent


@pytest.mark.asyncio
async def test_the_validated_answer_is_spoken_with_the_pronunciation(monkeypatch) -> None:
    speech = await _answer(monkeypatch, _RecordingLlm())
    assert "Lai ven to" in speech.text and "Livento" not in speech.text


def test_gate_allowlist_carries_every_brand_and_pronunciation_word() -> None:
    brief = SimpleNamespace(
        product_facts={
            "P1": {"product_info": {"brand": "Livento", "brand_pronunciation": "Lai-ven-to"}}
        }
    )
    assert _brand_allowlist(brief, "P1") == ("Livento", "Lai", "ven", "to")


_SHISEIDO = ScriptGateContext(brand_allowlist=("Shiseido", "Shi", "sei", "do"))


def test_a_brand_syllable_does_not_exempt_a_profane_word_containing_it() -> None:
    assert check_profanity("shit", _SHISEIDO, load_curated_lexicon())


def test_the_pronounced_brand_is_not_flagged_as_profanity() -> None:
    assert check_profanity("Mẫu này của Shi sei do nhé", _SHISEIDO, load_curated_lexicon()) == []


def test_a_brand_syllable_only_exempts_the_whole_spelling_token() -> None:
    ctx = ScriptGateContext(brand_allowlist=("Dila",))
    assert check_common_spelling("da mềm", ctx)
