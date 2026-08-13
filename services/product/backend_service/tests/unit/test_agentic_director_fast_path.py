"""Offline tests for the deterministic factual fast path (tasks 12.2-12.4).

The envelope, fact provider and verbalizer are duck-typed system boundaries;
they are faked here because live implementations arrive in later clusters.
"""

from __future__ import annotations

import re

import pytest

from backend.application.agentic_director.contracts import (
    FactualFastPlan,
    PlanKind,
    VerbalizationRequest,
)
from backend.application.agentic_director.fast_path import (
    FastPathConfig,
    FastPathEligibility,
    FastPathExecutor,
    FactValue,
    UntemplatedSelectorError,
    build_templated_answer,
    is_fast_path_eligible,
    select_fact_selector,
)

PRICE_PATTERN = re.compile(r"\d[\d.,]*\s*(?:k|đ|₫|nghìn|triệu|đồng)")


def make_envelope(
    intent: str = "giá",
    resolved: tuple[str, ...] = ("sp-001",),
    candidates: tuple[tuple[str, float], ...] = (("sp-001", 0.9),),
    questions: tuple[str, ...] = (),
) -> object:
    """Build a duck-typed envelope with the exact ClusterEnvelope attributes."""
    return type(
        "Envelope",
        (),
        {
            "cluster_id": "cl-1",
            "intent": intent,
            "message_count": 12,
            "unique_viewer_count": 8,
            "representative_questions": questions,
            "product_candidates": candidates,
            "resolved_product_ids": resolved,
            "ranking_score": 0.8,
            "novelty": 0.3,
            "current_script_product_id": "sp-001",
            "source_platform_counts": (("tiktok", 10), ("shopee", 2)),
        },
    )()


DEFAULT_CONFIG = FastPathConfig()


class FakeFactProvider:
    """Minimal fact provider recording its get_fact calls."""

    def __init__(self, fact: FactValue | None) -> None:
        self.fact = fact
        self.calls: list[tuple[str, str]] = []

    def get_fact(self, entity_id: str, selector: str) -> FactValue | None:
        self.calls.append((entity_id, selector))
        return self.fact


class FakeVerbalizer:
    """Canned-string verbalizer; optionally raises like a broken live impl."""

    def __init__(self, text: str = "Verbalized answer", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.requests: list[VerbalizationRequest] = []

    def verbalize(self, request: VerbalizationRequest) -> str:
        self.requests.append(request)
        if self.fail:
            raise RuntimeError("verbalizer failure")
        return self.text


class CollectingSink:
    """Collects telemetry records instead of forwarding them."""

    def __init__(self) -> None:
        self.records: dict[str, int | float] = {}

    def __call__(self, name: str, value: int | float) -> None:
        self.records[name] = value


def executor() -> FastPathExecutor:
    return FastPathExecutor()


# --- 12.2 eligibility -------------------------------------------------------


def test_eligible_when_intent_known_product_resolved_high_confidence():
    result = is_fast_path_eligible(make_envelope(), DEFAULT_CONFIG)
    assert result == FastPathEligibility(
        True, entity_id="sp-001", selector="commerce.price.current"
    )


def test_ineligible_when_intent_unknown():
    result = is_fast_path_eligible(make_envelope(intent=""), DEFAULT_CONFIG)
    assert result.eligible is False


def test_ineligible_when_confidence_below_threshold():
    result = is_fast_path_eligible(make_envelope(candidates=(("sp-001", 0.5),)), DEFAULT_CONFIG)
    assert result.eligible is False


def test_ineligible_when_no_resolved_product():
    result = is_fast_path_eligible(make_envelope(resolved=()), DEFAULT_CONFIG)
    assert result.eligible is False


def test_ineligible_on_comparison_intent():
    result = is_fast_path_eligible(
        make_envelope(intent="so sánh giá sp-001 và sp-002"), DEFAULT_CONFIG
    )
    assert result.eligible is False
    assert result.reason == "comparison_signal"


def test_ineligible_on_referential_question_without_resolved_product():
    result = is_fast_path_eligible(
        make_envelope(resolved=(), questions=("cái đó giá bao nhiêu?",)),
        DEFAULT_CONFIG,
    )
    assert result.eligible is False
    assert result.reason == "referential_signal"


def test_eligible_on_resolved_referential_question_answers_with_zero_llm_calls():
    envelope = make_envelope(questions=("vậy cái đó giá bao nhiêu?",))
    result = is_fast_path_eligible(envelope, DEFAULT_CONFIG)
    assert result.eligible is True
    assert result.selector == "commerce.price.current"
    assert result.entity_id == "sp-001"
    sink = CollectingSink()
    plan_result = executor().run_plan(
        FactualFastPlan("sp-001", "commerce.price.current"),
        envelope,
        FakeFactProvider(FactValue("299.000đ", fresh=True)),
        None,
        DEFAULT_CONFIG,
        metric_sink=sink,
    )
    assert plan_result.kind == PlanKind.ANSWER
    assert plan_result.answer.text == "Giá hiện tại của sp-001 là 299.000đ."
    assert sink.records["llm_calls"] == 0


def test_eligible_when_multiple_candidates_resolve_to_same_id():
    result = is_fast_path_eligible(
        make_envelope(
            candidates=(("sp-001", 0.9), ("sp-001", 0.95)),
            resolved=("sp-001",),
        ),
        DEFAULT_CONFIG,
    )
    assert result.eligible is True


def test_ineligible_when_intent_maps_to_unknown_selector():
    result = is_fast_path_eligible(make_envelope(intent="chất lượng vải"), DEFAULT_CONFIG)
    assert result.eligible is False


# --- selector mapping -------------------------------------------------------


def test_select_fact_selector_maps_known_intents():
    assert select_fact_selector("giá") == "commerce.price.current"
    assert select_fact_selector("còn hàng") == "commerce.stock.available"
    assert select_fact_selector("Giá Gốc") == "commerce.price.original"


def test_select_fact_selector_returns_none_for_unknown_intent():
    assert select_fact_selector("chất lượng vải") is None


# --- 12.3 zero-LLM templated answers ----------------------------------------


def test_fresh_fact_returns_exact_templated_answer_with_zero_llm_calls():
    sink = CollectingSink()
    result = executor().run_plan(
        FactualFastPlan("sp-001", "commerce.price.current"),
        make_envelope(),
        FakeFactProvider(FactValue("299.000đ", fresh=True)),
        None,
        DEFAULT_CONFIG,
        metric_sink=sink,
    )
    assert result.kind == PlanKind.ANSWER
    assert result.answer.text == "Giá hiện tại của sp-001 là 299.000đ."
    assert result.answer.source_entity_id == "sp-001"
    assert result.answer.source_selector == "commerce.price.current"
    assert sink.records["llm_calls"] == 0


def test_missing_fact_returns_unavailable_without_invented_price():
    result = executor().run_plan(
        FactualFastPlan("sp-001", "commerce.price.current"),
        make_envelope(),
        FakeFactProvider(None),
        None,
        DEFAULT_CONFIG,
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable.reason == "evidence_unavailable"
    assert PRICE_PATTERN.search(result.unavailable.reason) is None


def test_stale_volatile_fact_returns_unavailable():
    result = executor().run_plan(
        FactualFastPlan("sp-001", "commerce.price.current"),
        make_envelope(),
        FakeFactProvider(FactValue("299.000đ", fresh=False)),
        None,
        DEFAULT_CONFIG,
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable.reason == "evidence_unavailable"


def test_stock_template():
    text = build_templated_answer("Áo thun sp-001", "còn hàng", "commerce.stock.available")
    assert text == "Hiện tại Áo thun sp-001 còn hàng."


def test_unknown_selector_raises_typed_error():
    with pytest.raises(UntemplatedSelectorError):
        build_templated_answer("sp-001", "x", "commerce.material")


# --- 12.4 one-generation verbalization --------------------------------------


def test_verbalizer_called_once_with_grounded_fact_and_text_used():
    sink = CollectingSink()
    verbalizer = FakeVerbalizer("Giá 299 nghìn đồng nhé cả nhà")
    config = FastPathConfig(verbalize_where_appropriate=True)
    result = executor().run_plan(
        FactualFastPlan("sp-001", "commerce.price.current"),
        make_envelope(),
        FakeFactProvider(FactValue("299.000đ", fresh=True)),
        verbalizer,
        config,
        metric_sink=sink,
    )
    assert result.kind == PlanKind.ANSWER
    assert result.answer.text == "Giá 299 nghìn đồng nhé cả nhà"
    assert len(verbalizer.requests) == 1
    request = verbalizer.requests[0]
    assert request.grounded_fact == "commerce.price.current: 299.000đ"
    assert request.entity_display_name == "sp-001"
    assert sink.records["llm_calls"] == 1


def test_verbalizer_failure_falls_back_to_exact_template():
    sink = CollectingSink()
    verbalizer = FakeVerbalizer(fail=True)
    config = FastPathConfig(verbalize_where_appropriate=True)
    result = executor().run_plan(
        FactualFastPlan("sp-001", "commerce.price.current"),
        make_envelope(),
        FakeFactProvider(FactValue("299.000đ", fresh=True)),
        verbalizer,
        config,
        metric_sink=sink,
    )
    assert result.kind == PlanKind.ANSWER
    assert result.answer.text == "Giá hiện tại của sp-001 là 299.000đ."
    assert sink.records["llm_calls"] == 0


def test_no_verbalizer_when_not_configured():
    result = executor().run_plan(
        FactualFastPlan("sp-001", "commerce.price.current"),
        make_envelope(),
        FakeFactProvider(FactValue("299.000đ", fresh=True)),
        FakeVerbalizer("would never be used"),
        DEFAULT_CONFIG,
    )
    assert result.answer.text == "Giá hiện tại của sp-001 là 299.000đ."


# --- telemetry --------------------------------------------------------------


def test_unavailable_records_zero_llm_and_evidence_ops():
    sink = CollectingSink()
    executor().run_plan(
        FactualFastPlan("sp-001", "commerce.price.current"),
        make_envelope(),
        FakeFactProvider(None),
        None,
        DEFAULT_CONFIG,
        metric_sink=sink,
    )
    assert sink.records["llm_calls"] == 0
    assert sink.records["evidence_ops"] == 1
    assert sink.records["prompt_tokens"] == 0
    assert sink.records["generated_tokens"] == 0
    assert sink.records["latency_ms"] >= 0
