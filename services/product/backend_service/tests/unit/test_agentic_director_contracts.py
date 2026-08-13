"""Offline tests for the agentic director plan/result contracts (task 12.1)."""

from __future__ import annotations

import pytest

from backend.application.agentic_director import (
    AnswerText,
    BudgetExceeded,
    ComplexPlan,
    EvidenceRequest,
    FactualFastPlan,
    PlanKind,
    PlanResult,
    UnavailableAnswer,
    VerbalizationRequest,
)


def test_factual_fast_plan_holds_entity_and_selector():
    assert FactualFastPlan("sp-123", "price").target_entity_id == "sp-123"


def test_complex_plan_holds_intent_entities_and_typed_evidence_requests():
    req = EvidenceRequest(selector="price", entity_id="sp-123")
    plan = ComplexPlan(
        intent="compare_prices", entities=("sp-123", "sp-456"), evidence_requests=(req,)
    )
    assert plan.evidence_requests == (req,)


def test_evidence_request_allows_entityless_evidence():
    EvidenceRequest(selector="campaign_status")


def test_plans_are_frozen():
    with pytest.raises(Exception):
        FactualFastPlan("sp-123", "price").fact_selector = "stock"
    with pytest.raises(Exception):
        ComplexPlan("intent", ("sp-1",), ()).intent = "other"


def test_answer_result_kind_is_answer():
    assert (
        PlanResult(kind=PlanKind.ANSWER, answer=AnswerText(text="29.000 đồng")).answer.text
        == "29.000 đồng"
    )


def test_unavailable_result_kind_is_unavailable():
    result = PlanResult(
        kind=PlanKind.UNAVAILABLE, unavailable=UnavailableAnswer(reason="no evidence")
    )
    assert result.unavailable.reason == "no evidence"


def test_budget_exceeded_result_holds_limit_and_used():
    result = PlanResult(
        kind=PlanKind.BUDGET_EXCEEDED, budget=BudgetExceeded(limit=8, used=9, op="get_evidence")
    )
    assert result.budget.limit == 8
    assert result.budget.used == 9


def test_results_are_frozen():
    with pytest.raises(Exception):
        PlanResult(kind=PlanKind.ANSWER, answer=AnswerText("x")).kind = PlanKind.UNAVAILABLE


def test_verbalization_request_holds_grounded_fact_and_display_info():
    req = VerbalizationRequest(
        grounded_fact="giá 29.000 đồng",
        question_context="sản phẩm này giá bao nhiêu",
        entity_display_name="Áo thun cotton",
    )
    assert req.entity_display_name == "Áo thun cotton"
