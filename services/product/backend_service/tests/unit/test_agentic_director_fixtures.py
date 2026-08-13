"""Offline Q&A fixtures covering all five intent classes (task 12.11).

Ambiguous, comparative, multi-product, open-ended and referential clusters
drive the full fast-path/complex-path decision the way a live session would:
eligibility is asserted, then each path is executed against shared fakes with
exact telemetry checks. Behavior, not implementation — the fakes here are the
system boundaries (fact provider, planner, evidence executor, final
generator, verbalizer) whose live implementations arrive in later clusters.

NOTE on the referential gate: eligibility rejects a referential cluster only
when the referent is NOT resolved (referential_signal); once the product IS
resolved upstream, the follow-up is a plain factual question and is answered
through the fast path with zero LLM calls.
"""

from __future__ import annotations

import re

from backend.application.agentic_director.complex_path import (
    AgentBudgets,
    ComplexPathExecutor,
    PlanningOutput,
)
from backend.application.agentic_director.contracts import (
    ComplexPlan,
    EvidenceRequest,
    FactualFastPlan,
    PlanKind,
)
from backend.application.agentic_director.fast_path import (
    FastPathConfig,
    FastPathExecutor,
    FactValue,
    is_fast_path_eligible,
)

PRICE_PATTERN = re.compile(r"\d[\d.,]*\s*(?:k|đ|₫|nghìn|triệu|đồng)")


# --- shared fakes -----------------------------------------------------------


class FakeEnvelope:
    """A ClusterEnvelope-shaped envelope with exact Decision-9 attributes."""

    def __init__(
        self,
        intent: str,
        questions: tuple[str, ...],
        candidates: tuple[tuple[str, float], ...],
        resolved: tuple[str, ...],
        confidence: float,
    ) -> None:
        self.cluster_id = "cl-fixture"
        self.intent = intent
        self.message_count = 12
        self.unique_viewer_count = 8
        self.representative_questions = questions
        self.product_candidates = candidates
        self.resolved_product_ids = resolved
        self.ranking_score = 0.8
        self.novelty = 0.3
        self.current_script_product_id = resolved[0] if resolved else None
        self.source_platform_counts = (("tiktok", 10), ("shopee", 2))


def build_envelope(
    intent: str,
    questions: tuple[str, ...] = (),
    candidates: tuple[tuple[str, float], ...] = (("P001", 0.9),),
    resolved: tuple[str, ...] = ("P001",),
    confidence: float | None = None,
) -> FakeEnvelope:
    """Parameterized envelope builder: one entity with a single confidence."""
    if confidence is not None:
        candidates = tuple((entity_id, confidence) for entity_id, _ in candidates)
    return FakeEnvelope(intent, questions, candidates, resolved, confidence or 0.9)


class FakeFactProvider:
    """Dict-backed fact provider; records every get_fact call."""

    def __init__(self, facts: dict[tuple[str, str], FactValue | None]) -> None:
        self.facts = facts
        self.calls: list[tuple[str, str]] = []

    def get_fact(self, entity_id: str, selector: str) -> FactValue | None:
        self.calls.append((entity_id, selector))
        return self.facts.get((entity_id, selector))


class FakeEvidenceExecutor:
    """Records every call and serves canned result batches per round."""

    def __init__(self, round_results: list[list[dict]]) -> None:
        self.round_results = list(round_results)
        self.calls: list[tuple] = []

    def search_entities(
        self, queries: tuple[str, ...], entity_type: str | None = None
    ) -> list[dict]:
        self.calls.append(("search_entities", queries, entity_type))
        return self._next()

    def get_entities(
        self, entity_ids: tuple[str, ...], selectors: tuple[str, ...] | None = None
    ) -> list[dict]:
        self.calls.append(("get_entities", entity_ids, selectors))
        return self._next()

    def get_evidence(self, requests: tuple) -> list[dict]:
        self.calls.append(("get_evidence", requests))
        return self._next()

    def _next(self) -> list[dict]:
        return self.round_results.pop(0) if self.round_results else []


class FakePlanner:
    """Returns one fixed planning output and records every call."""

    def __init__(self, output: PlanningOutput) -> None:
        self.output = output
        self.calls: list[ComplexPlan] = []

    def plan(self, request: ComplexPlan) -> PlanningOutput:
        self.calls.append(request)
        return self.output


class FakeFinalGenerator:
    """Returns canned grounded text and records every call."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def generate(self, evidence_summary: str, question_context: str) -> str:
        self.calls.append((evidence_summary, question_context))
        return self.text


class CollectingMetricSink:
    """Collects telemetry records keyed by canonical metric name."""

    def __init__(self) -> None:
        self.records: dict[str, int | float] = {}

    def __call__(self, name: str, value: int | float) -> None:
        self.records[name] = value


def run_complex(
    planner_output: PlanningOutput,
    round_results: list[list[dict]],
    final_text: str,
    sink: CollectingMetricSink,
) -> None:
    """Execute one complex-path Q&A and assert its exact terminal answer."""
    plan = planner_output.plan
    assert plan is not None
    planner = FakePlanner(planner_output)
    evidence = FakeEvidenceExecutor(round_results)
    final = FakeFinalGenerator(final_text)
    result = ComplexPathExecutor().run_plan(
        plan,
        envelope=object(),
        planner=planner,
        evidence_executor=evidence,
        final_generator=final,
        budgets=AgentBudgets(),
        metric_sink=sink,
    )
    assert result.kind == PlanKind.ANSWER
    assert result.answer is not None
    assert result.answer.text == final_text
    assert len(final.calls) == 1


# --- 12.11 AMBIGUOUS --------------------------------------------------------


def test_ambiguous_low_confidence_never_uses_fast_path():
    envelope = build_envelope(intent="giá", confidence=0.5)
    eligibility = is_fast_path_eligible(envelope, FastPathConfig())
    assert eligibility.eligible is False
    assert eligibility.reason == "product_confidence_low"


def test_ambiguous_path_with_missing_evidence_returns_unavailable():
    sink = CollectingMetricSink()
    plan = ComplexPlan(
        intent="giá",
        entities=("P001",),
        evidence_requests=(EvidenceRequest(selector="commerce.price.current", entity_id="P001"),),
    )
    planner = FakePlanner(PlanningOutput(plan=plan, raw_text="price unclear"))
    evidence = FakeEvidenceExecutor([[]])
    final = FakeFinalGenerator("never called")
    result = ComplexPathExecutor().run_plan(
        plan,
        envelope=object(),
        planner=planner,
        evidence_executor=evidence,
        final_generator=final,
        budgets=AgentBudgets(),
        metric_sink=sink,
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable is not None
    assert result.unavailable.reason == "evidence_unavailable"
    assert final.calls == []
    assert PRICE_PATTERN.search(result.unavailable.reason) is None
    assert sink.records["planning_generations"] == 1
    assert sink.records["evidence_rounds"] == 1
    assert sink.records["evidence_ops"] == 1
    assert sink.records["final_generations"] == 0
    assert sink.records["llm_calls"] == 1


# --- 12.11 COMPARATIVE ------------------------------------------------------


def test_comparative_intent_is_not_fast_path_eligible():
    envelope = build_envelope(intent="so sánh P001 và P002")
    eligibility = is_fast_path_eligible(envelope, FastPathConfig())
    assert eligibility.eligible is False
    assert eligibility.reason == "comparison_signal"


def test_comparative_answer_grounds_both_exact_price_values():
    sink = CollectingMetricSink()
    plan = ComplexPlan(
        intent="so sánh P001 và P002",
        entities=("P001", "P002"),
        evidence_requests=(
            EvidenceRequest(selector="commerce.price.current", entity_id="P001"),
            EvidenceRequest(selector="commerce.price.current", entity_id="P002"),
        ),
    )
    final_text = "P001 giá 299.000đ, P002 giá 350.000đ."
    run_complex(
        PlanningOutput(plan=plan, raw_text="compare prices"),
        [
            [
                {"entity_id": "P001", "selector": "commerce.price.current", "value": "299.000đ"},
                {"entity_id": "P002", "selector": "commerce.price.current", "value": "350.000đ"},
            ]
        ],
        final_text,
        sink,
    )
    assert "299.000đ" in final_text
    assert "350.000đ" in final_text
    assert sink.records["planning_generations"] == 1
    assert sink.records["evidence_rounds"] == 1
    assert sink.records["final_generations"] == 1
    assert sink.records["llm_calls"] == 2
    assert sink.records["evidence_ops"] == 1


# --- 12.11 MULTI-PRODUCT ----------------------------------------------------


def test_multi_product_is_not_fast_path_eligible():
    envelope = build_envelope(
        intent="giá", candidates=(("P001", 0.9), ("P002", 0.9)), resolved=("P001", "P002")
    )
    eligibility = is_fast_path_eligible(envelope, FastPathConfig())
    assert eligibility.eligible is False
    assert eligibility.reason == "multiple_entities"


def test_multi_product_answer_uses_both_entities():
    sink = CollectingMetricSink()
    plan = ComplexPlan(
        intent="giá",
        entities=("P001", "P002"),
        evidence_requests=(
            EvidenceRequest(selector="commerce.price.current", entity_id="P001"),
            EvidenceRequest(selector="commerce.price.current", entity_id="P002"),
        ),
    )
    final_text = "P001 là 299.000đ, P002 là 350.000đ."
    run_complex(
        PlanningOutput(plan=plan, raw_text="both products"),
        [
            [
                {"entity_id": "P001", "selector": "commerce.price.current", "value": "299.000đ"},
                {"entity_id": "P002", "selector": "commerce.price.current", "value": "350.000đ"},
            ]
        ],
        final_text,
        sink,
    )
    assert "299.000đ" in final_text
    assert "350.000đ" in final_text
    assert sink.records["llm_calls"] == 2


# --- 12.11 OPEN-ENDED -------------------------------------------------------


def test_open_ended_unknown_selector_is_not_fast_path_eligible():
    envelope = build_envelope(intent="công dụng")
    eligibility = is_fast_path_eligible(envelope, FastPathConfig())
    assert eligibility.eligible is False
    assert eligibility.reason == "selector_unknown"


def test_open_ended_grounded_answer_contains_no_fabricated_numbers():
    sink = CollectingMetricSink()
    plan = ComplexPlan(
        intent="công dụng",
        entities=("P001",),
        evidence_requests=(EvidenceRequest(selector="commerce.description", entity_id="P001"),),
    )
    final_text = "P001 dùng để chống nắng phổ rộng SPF50+."
    run_complex(
        PlanningOutput(plan=plan, raw_text="what is it for"),
        [[{"entity_id": "P001", "selector": "commerce.description", "value": "chống nắng SPF50+"}]],
        final_text,
        sink,
    )
    assert PRICE_PATTERN.search(final_text) is None
    assert sink.records["llm_calls"] == 2


def test_open_ended_without_evidence_returns_unavailable_never_fabricates():
    sink = CollectingMetricSink()
    plan = ComplexPlan(
        intent="công dụng",
        entities=("P001",),
        evidence_requests=(EvidenceRequest(selector="commerce.description", entity_id="P001"),),
    )
    planner = FakePlanner(PlanningOutput(plan=plan, raw_text="unknown"))
    evidence = FakeEvidenceExecutor([[]])
    final = FakeFinalGenerator("never called")
    result = ComplexPathExecutor().run_plan(
        plan,
        envelope=object(),
        planner=planner,
        evidence_executor=evidence,
        final_generator=final,
        budgets=AgentBudgets(),
        metric_sink=sink,
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable is not None
    assert result.unavailable.reason == "evidence_unavailable"
    assert final.calls == []
    assert PRICE_PATTERN.search(result.unavailable.reason) is None


# --- 12.11 REFERENTIAL ------------------------------------------------------


def test_referential_question_with_resolved_product_is_fast_path_eligible():
    envelope = build_envelope(
        intent="giá",
        questions=("vậy cái đó giá bao nhiêu?",),
        resolved=("P001",),
        candidates=(("P001", 0.9),),
    )
    eligibility = is_fast_path_eligible(envelope, FastPathConfig())
    assert eligibility.eligible is True
    assert eligibility.selector == "commerce.price.current"
    assert eligibility.entity_id == "P001"


def test_referential_resolved_runs_fast_path_with_zero_llm_calls():
    sink = CollectingMetricSink()
    envelope = build_envelope(
        intent="giá",
        questions=("vậy cái đó giá bao nhiêu?",),
        resolved=("P001",),
        candidates=(("P001", 0.9),),
    )
    result = FastPathExecutor().run_plan(
        FactualFastPlan("P001", "commerce.price.current"),
        envelope,
        FakeFactProvider({("P001", "commerce.price.current"): FactValue("299.000đ", fresh=True)}),
        None,
        FastPathConfig(),
        metric_sink=sink,
    )
    assert result.kind == PlanKind.ANSWER
    assert result.answer is not None
    assert result.answer.text == "Giá hiện tại của P001 là 299.000đ."
    assert sink.records["llm_calls"] == 0


def test_referential_question_without_resolved_product_is_not_eligible():
    envelope = build_envelope(
        intent="sạc nhanh", questions=("vậy cái đó có sạc nhanh không?",), resolved=()
    )
    eligibility = is_fast_path_eligible(envelope, FastPathConfig())
    assert eligibility.eligible is False
    assert eligibility.reason == "referential_signal"


def test_referential_without_resolved_product_runs_complex_path():
    sink = CollectingMetricSink()
    plan = ComplexPlan(
        intent="sạc nhanh",
        entities=("P001",),
        evidence_requests=(EvidenceRequest(selector="commerce.shipping", entity_id="P001"),),
    )
    run_complex(
        PlanningOutput(plan=plan, raw_text="referential follow-up"),
        [[{"entity_id": "P001", "selector": "commerce.shipping", "value": "có sạc nhanh"}]],
        "P001 có sạc nhanh.",
        sink,
    )
    assert sink.records["llm_calls"] == 2


# --- shared fake usage sanity ------------------------------------------------


def test_fake_envelope_implements_the_cluster_envelope_protocol():
    from backend.application.agentic_director.fast_path import ClusterEnvelope

    envelope = build_envelope(intent="giá")
    assert isinstance(envelope, ClusterEnvelope)
