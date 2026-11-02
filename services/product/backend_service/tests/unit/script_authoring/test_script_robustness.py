"""Graceful degradation of script units: finish-aware closing, retry shaping, compact fallback, reason texts."""

from __future__ import annotations

import pytest

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
from backend.application.clients.composition import ensure_remote_engines_registered
from backend.application.contracts.llm_engines import LLMEngine, LLMResponse, register_engine
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
    assert reason_text(["SPEECH_DURATION_SEGMENT"]).startswith("Phần này dài hơn giới hạn")


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


def _failing(_prompt):
    raise LookupError("HTTP 404 model not found")


def _timing_out(_prompt):
    raise TimeoutError("slow")


def test_a_failing_retry_model_falls_back_to_the_normal_model() -> None:
    llm = BoundedLLM(lambda p: "first", repair_fn=lambda p: "repair", final_fn=_failing, attempts=1)
    assert llm("x", repair=True, final=True) == "repair"


def test_a_failing_retry_model_without_a_repair_function_uses_the_plain_one() -> None:
    llm = BoundedLLM(lambda p: "first", final_fn=_failing, attempts=1)
    assert llm("x", repair=True, final=True) == "first"


def test_a_retry_model_timeout_is_not_retried_on_another_model() -> None:
    llm = BoundedLLM(
        lambda p: "first", repair_fn=lambda p: "repair", final_fn=_timing_out, attempts=1
    )
    with pytest.raises(TimeoutError):
        llm("x", repair=True, final=True)


def test_a_reply_ending_on_a_conjunction_is_not_closed() -> None:
    assert close_unfinished("Áo làm bằng chất liệu cotton và") == "Áo làm bằng chất liệu cotton và"


def test_a_reply_ending_on_a_preposition_is_not_closed() -> None:
    assert close_unfinished("Mua thêm để") == "Mua thêm để"


def test_a_reply_ending_on_a_bare_number_is_not_closed() -> None:
    assert close_unfinished("Áo này giá 299") == "Áo này giá 299"


def test_a_number_followed_by_a_unit_word_still_closes() -> None:
    assert close_unfinished("Gói dùng trong 7 ngày") == "Gói dùng trong 7 ngày."


class _Spy:
    def __init__(self):
        self.unloaded = 0

    def unload(self):
        self.unloaded += 1


@register_engine("robust_fake")
class _RegisteredEngine(LLMEngine):
    @classmethod
    def from_config(cls, cfg):
        return cls()

    def generate(self, req):
        return LLMResponse(text="ok")


def _remote_manager(**cfg):
    ensure_remote_engines_registered()
    manager = EngineManager()
    manager._llm = object()
    manager._llm_cfg = {
        "engine": "openai_compat",
        "model": "flash",
        "base_url": "http://localhost:1",
        "api_key": "x",
        **cfg,
    }
    return manager


def test_a_retry_engine_is_built_once_and_reused() -> None:
    manager = _remote_manager()
    assert manager._engine_for_model("pro") is manager._engine_for_model("pro")


def test_the_retry_engine_runs_the_requested_model() -> None:
    assert _remote_manager()._engine_for_model("pro")._client.model == "pro"


def test_a_retry_engine_that_cannot_be_built_falls_back_to_the_active_engine(monkeypatch) -> None:
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    manager = _remote_manager(base_url="")
    assert manager._engine_for_model("pro") is manager._llm


def test_the_configured_model_is_the_active_engine_not_a_new_one() -> None:
    manager = _remote_manager()
    assert manager._engine_for_model("flash") is manager._llm


def test_reloading_the_llm_unloads_the_retry_engines() -> None:
    manager = _remote_manager()
    manager._llm = _Spy()
    spy = _Spy()
    manager._model_engines["pro"] = spy
    manager.load_llm({"engine": "robust_fake"})
    assert spy.unloaded == 1


def test_reloading_the_llm_clears_the_retry_engine_cache() -> None:
    manager = _remote_manager()
    manager._llm = _Spy()
    manager._model_engines["pro"] = _Spy()
    manager.load_llm({"engine": "robust_fake"})
    assert manager._model_engines == {}


def test_a_retry_engine_built_during_a_reload_is_not_cached(monkeypatch) -> None:
    manager = _remote_manager()
    built = _Spy()

    def build_while_reloading(_cfg):
        manager._llm_generation += 1  # load_llm ran while this engine was being built
        return built

    monkeypatch.setattr("backend.engine_manager.load_llm_engine", build_while_reloading)
    manager._engine_for_model("pro")
    assert manager._model_engines == {}


def test_a_retry_engine_built_during_a_reload_is_unloaded(monkeypatch) -> None:
    manager = _remote_manager()
    built = _Spy()

    def build_while_reloading(_cfg):
        manager._llm_generation += 1
        return built

    monkeypatch.setattr("backend.engine_manager.load_llm_engine", build_while_reloading)
    manager._engine_for_model("pro")
    assert built.unloaded == 1


def test_a_reload_during_the_build_returns_the_active_engine(monkeypatch) -> None:
    manager = _remote_manager()

    def build_while_reloading(_cfg):
        manager._llm_generation += 1
        return _Spy()

    monkeypatch.setattr("backend.engine_manager.load_llm_engine", build_while_reloading)
    assert manager._engine_for_model("pro") is manager._llm
