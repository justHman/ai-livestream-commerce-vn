"""Graceful degradation of script units: finish-aware closing, retry shaping, compact fallback, reason texts."""

from __future__ import annotations

from backend.application.script_authoring.generation.ordered_units import (
    BoundedLLM,
    ProductBrief,
    SessionBrief,
    UnitSpec,
    build_unit_repair_prompt,
    close_unfinished,
    compact_unit_text,
    fit_unit_claims,
    unit_max_tokens,
)
from backend.application.script_authoring.generation.reason_text import reason_text
from backend.application.script_authoring.generation.unit_brief import provenance
from backend.application.contracts.llm_engines import LLMResponse
from backend.engine_manager import EngineManager, LLMText

LONG_CLAIMS = tuple(
    f"Thông tin số {i} nói rõ về cách dùng sản phẩm này trong nhiều tình huống khác nhau hằng ngày."
    for i in range(6)
)
MUST = "Không đổi trả sau bảy ngày kể từ khi nhận hàng."
DURATION_PROBLEM = (
    "SPEECH_DURATION_SEGMENT: Segment is too long: estimated 87.2s spoken, target maximum is 60s."
)


class _Engine:
    def __init__(self, finish="stop"):
        self.finish = finish
        self.requests = []

    def generate(self, req):
        self.requests.append(req)
        return LLMResponse(text="xin chào", finish_reason=self.finish)


def _manager(engine):
    manager = EngineManager()
    manager._llm = engine
    manager._llm_cfg = {"engine": "echo", "max_tokens": 512, "temperature": 0.7}
    return manager


def test_a_reply_ending_on_a_word_gets_its_final_period() -> None:
    assert close_unfinished("Gói có ba mươi phút") == "Gói có ba mươi phút."


def test_a_reply_ending_on_a_comma_is_left_alone() -> None:
    assert close_unfinished("Gói có ba mươi phút,") == "Gói có ba mươi phút,"


def test_output_budget_for_a_sixty_second_unit() -> None:
    assert unit_max_tokens([UnitSpec("highlight")]) == 1065


def test_output_budget_for_a_closing_unit_is_not_below_the_floor() -> None:
    assert unit_max_tokens([UnitSpec("closing")]) == 879


def test_the_last_write_uses_the_final_function() -> None:
    llm = BoundedLLM(
        lambda p: "first", repair_fn=lambda p: "repair", final_fn=lambda p: "final", attempts=1
    )
    assert llm("x", repair=True, final=True) == "final"


def test_a_later_write_without_a_final_function_uses_the_repair_function() -> None:
    llm = BoundedLLM(lambda p: "first", repair_fn=lambda p: "repair", attempts=1)
    assert llm("x", repair=True, final=True) == "repair"


def test_the_first_write_uses_the_plain_function() -> None:
    llm = BoundedLLM(
        lambda p: "first", repair_fn=lambda p: "repair", final_fn=lambda p: "final", attempts=1
    )
    assert llm("x") == "first"


def test_fitting_keeps_the_must_keep_claim_that_comes_last() -> None:
    spec = UnitSpec("assurance", claims=(*LONG_CLAIMS, MUST), must=(MUST,))
    assert MUST in fit_unit_claims(spec, 20).claims


def test_fitting_drops_claims_that_do_not_fit() -> None:
    spec = UnitSpec("assurance", claims=(*LONG_CLAIMS, MUST), must=(MUST,))
    assert len(fit_unit_claims(spec, 20).claims) < len(spec.claims)


def test_fitting_always_keeps_the_first_claim() -> None:
    assert fit_unit_claims(UnitSpec("highlight", claims=LONG_CLAIMS), 1).claims == LONG_CLAIMS[:1]


def _repair_prompt(problem):
    spec = UnitSpec("highlight", claims=LONG_CLAIMS)
    return build_unit_repair_prompt(
        SessionBrief(),
        ProductBrief(product_id="p", name="Gói thử", claims=LONG_CLAIMS),
        [spec],
        0,
        failed_text="Bản trước dài.",
        problems=[problem],
    )


def test_a_duration_retry_states_the_measured_seconds() -> None:
    assert "87 giây, giới hạn 60 giây" in _repair_prompt(DURATION_PROBLEM)


def test_a_duration_retry_states_a_hard_sentence_limit() -> None:
    assert "tối đa 4 câu ngắn" in _repair_prompt(DURATION_PROBLEM)


def test_a_duration_retry_offers_fewer_facts() -> None:
    assert LONG_CLAIMS[-1] not in _repair_prompt(DURATION_PROBLEM)


def test_another_rejection_keeps_every_fact_in_the_retry() -> None:
    assert LONG_CLAIMS[-1] in _repair_prompt("CLAIM_FACTUAL: Claim 'ổn định' is not approved.")


def test_the_compact_unit_drops_the_faq_lead_in() -> None:
    claim = "Có đổi size không? Có, trong 15 ngày."
    assert compact_unit_text(UnitSpec("assurance", claims=(claim,))) == claim


def test_the_compact_unit_is_one_paragraph() -> None:
    spec = UnitSpec("highlight", claims=("Hai màu xanh", "Có túi sâu"))
    assert compact_unit_text(spec) == "Hai màu xanh. Có túi sâu."


def test_more_than_three_claims_are_left_to_the_verbatim_paragraphs() -> None:
    assert (
        compact_unit_text(UnitSpec("highlight", claims=("a b c", "d e f", "g h i", "j k l")))
        is None
    )


def test_an_offer_unit_is_never_compacted() -> None:
    assert compact_unit_text(UnitSpec("offer", promos=("Tặng quà nhỏ.",))) is None


def test_a_duration_failure_has_a_plain_reason() -> None:
    assert reason_text(["SPEECH_DURATION_SEGMENT"]).startswith("Phần này dài quá giới hạn")


def test_a_truncated_reply_has_a_plain_reason() -> None:
    assert "viết dở câu" in reason_text(["guard:truncated"])


def test_the_last_recognised_reason_decides_the_text() -> None:
    assert "bỏ sót" in reason_text(["CLAIM_PRICE", "guard:coverage"])


def test_a_provider_timeout_is_a_time_reason() -> None:
    assert reason_text(["llm_failed:TimeoutError"]).startswith("Hết thời gian")


def test_an_unknown_reason_has_no_text() -> None:
    assert reason_text(["something odd"]) == ""


def test_a_fallback_provenance_carries_the_plain_text() -> None:
    assert provenance("fallback", ["guard:foreign_script"])["fallback_reason_text"].startswith(
        "AI lẫn chữ"
    )


def test_a_model_written_part_has_no_reason_text() -> None:
    assert "fallback_reason_text" not in provenance("llm")


def test_the_default_llm_function_still_returns_plain_text() -> None:
    reply = _manager(_Engine()).get_llm_fn()("hi")
    assert type(reply) is str


def test_a_finish_aware_function_reports_why_the_provider_stopped() -> None:
    reply = _manager(_Engine("length")).get_llm_fn(with_finish=True)("hi")
    assert isinstance(reply, LLMText) and reply.finish_reason == "length"


def test_the_temperature_override_reaches_the_request() -> None:
    engine = _Engine()
    _manager(engine).get_llm_fn(temperature=0.3)("hi")
    assert engine.requests[0].temperature == 0.3


def test_the_configured_output_budget_is_never_lowered() -> None:
    engine = _Engine()
    _manager(engine).get_llm_fn(max_tokens=100)("hi")
    assert engine.requests[0].max_tokens == 512


def test_another_model_is_ignored_when_the_engine_cannot_switch_models() -> None:
    engine = _Engine()
    assert _manager(engine)._engine_for_model("bigger") is engine
