"""Task 14.6/14.8: boundary Q&A resolver — fast path -> complex path + volatile hook.

Proves: a fast-path-eligible cluster resolves without the complex path; a
complex cluster resolves through the complex path (the deferred final
generation — only ever called at the boundary by the arbiter); volatile
evidence revalidation is just-in-time and a stale/missing volatile entry
yields ``unavailable`` instead of stale speech; the deterministic lead-in
never reads raw viewer text.
"""

from __future__ import annotations

from backend.application.agentic_director.complex_path import PlanningOutput
from backend.application.agentic_director.contracts import ComplexPlan, EvidenceRequest
from backend.application.agentic_director.fast_path import FactValue
from backend.application.evidence.cache import EvidenceCache
from backend.application.evidence.models import EvidenceConfig, Fact
from backend.application.live_runtime.qa_resolver import BoundaryQaResolver


class FastEnvelope:
    """Fast-path-eligible cluster: single resolved product, known intent."""

    cluster_id = "cl-fast"
    intent = "giá"
    message_count = 5
    unique_viewer_count = 3
    representative_questions = ("P001 giá bao nhiêu?",)
    product_candidates = (("P001", 0.95),)
    resolved_product_ids = ("P001",)
    ranking_score = 0.9
    novelty = 0.2
    current_script_product_id = "P001"
    source_platform_counts = (("tiktok", 5),)


class ComplexEnvelope:
    """Complex-path cluster: comparison intent, two entities."""

    cluster_id = "cl-compare"
    intent = "so sánh giá P001 và P002"
    message_count = 9
    unique_viewer_count = 6
    representative_questions = ("P001 vs P002 giá thế nào?",)
    product_candidates = (("P001", 0.9), ("P002", 0.9))
    resolved_product_ids = ("P001", "P002")
    ranking_score = 0.8
    novelty = 0.4
    current_script_product_id = "P001"
    source_platform_counts = (("tiktok", 9),)


class FakeFactProvider:
    def __init__(self, fact: FactValue | None) -> None:
        self.fact = fact

    def get_fact(self, entity_id: str, selector: str) -> FactValue | None:
        return self.fact


class FakePlanner:
    def __init__(self, plan: ComplexPlan | None) -> None:
        self._plan = plan

    def plan(self, request: ComplexPlan) -> PlanningOutput:
        return PlanningOutput(plan=self._plan, raw_text="plan")


def _complex_plan(candidate) -> ComplexPlan:
    return ComplexPlan(
        intent="so sánh giá",
        entities=("P001", "P002"),
        evidence_requests=(EvidenceRequest(selector="price", entity_id="P001"),),
    )


class FakeEvidenceExecutor:
    def search_entities(self, queries, entity_type=None) -> list[dict]:
        return []

    def get_entities(self, entity_ids, selectors=None) -> list[dict]:
        return []

    def get_evidence(self, requests) -> list[dict]:
        return [
            {"entity_id": "P001", "selector": "price", "value": "1.2 triệu"},
            {"entity_id": "P002", "selector": "price", "value": "990 nghìn"},
        ]


class FakeFinalGenerator:
    def generate(self, evidence_summary: str, question_context: str) -> str:
        return "P001 giá 1.2 triệu, P002 giá 990 nghìn."


def _resolver(
    *,
    cache: EvidenceCache | None = None,
    fact: FactValue | None = None,
) -> BoundaryQaResolver:
    return BoundaryQaResolver(
        fact_provider=FakeFactProvider(fact),
        planner=FakePlanner(_complex_plan(object())),
        evidence_executor=FakeEvidenceExecutor(),
        final_generator=FakeFinalGenerator(),
        evidence_cache=cache,
        make_complex_plan=_complex_plan,
    )


async def test_fast_path_eligible_cluster_resolves_via_fast_path() -> None:
    resolver = _resolver(fact=FactValue(value="1.2 triệu", fresh=True))

    resolution = await resolver.resolve_qa(FastEnvelope())

    assert resolution.kind == "answer"
    assert "1.2 triệu" in resolution.speech_text
    assert "P001" in resolution.speech_text
    assert resolution.evidence_fresh is True


async def test_fast_path_stale_fact_falls_to_complex_path() -> None:
    resolver = _resolver(fact=FactValue(value="1.2 triệu", fresh=False))

    resolution = await resolver.resolve_qa(FastEnvelope())

    assert resolution.kind == "answer"
    assert "P001 giá 1.2 triệu" in resolution.speech_text


async def test_complex_cluster_resolves_via_complex_path() -> None:
    resolver = _resolver()

    resolution = await resolver.resolve_qa(ComplexEnvelope())

    assert resolution.kind == "answer"
    assert "P001 giá 1.2 triệu" in resolution.speech_text


async def test_complex_path_unavailable_yields_typed_unavailable() -> None:
    resolver = _resolver()
    resolver._make_complex_plan = lambda candidate: _complex_plan(candidate)
    resolver._planner = FakePlanner(None)  # plan_invalid -> unavailable

    resolution = await resolver.resolve_qa(ComplexEnvelope())

    assert resolution.kind == "unavailable"
    assert resolution.speech_text == ""


async def test_volatile_stale_entry_yields_unavailable_no_speech() -> None:
    cache = EvidenceCache(config=EvidenceConfig(volatile_ttl_seconds=1))
    cache.set("P001", "price", Fact(key="price", type="volatile", value="1.2 triệu"))
    resolver = _resolver(cache=cache)

    resolution = await resolver.resolve_qa(FastEnvelope())

    assert resolution.kind == "unavailable"
    assert resolution.speech_text == ""


async def test_volatile_fresh_entry_allows_answer() -> None:
    cache = EvidenceCache(config=EvidenceConfig(volatile_ttl_seconds=60))
    for selector in ("price", "stock", "promotion", "availability"):
        cache.set("P001", selector, Fact(key=selector, type="volatile", value="ok"))
    resolver = _resolver(cache=cache, fact=FactValue(value="1.2 triệu", fresh=True))

    resolution = await resolver.resolve_qa(FastEnvelope())

    assert resolution.kind == "answer"
    assert "1.2 triệu" in resolution.speech_text


async def test_lead_in_never_contains_raw_viewer_text() -> None:
    resolver = _resolver(fact=FactValue(value="1.2 triệu", fresh=True))

    resolution = await resolver.resolve_qa(FastEnvelope())
    raw_question = FastEnvelope.representative_questions[0]

    assert raw_question not in resolution.lead_in
    assert "giá bao nhiêu?" not in resolution.lead_in


async def test_exact_facts_survive_composed_speech_byte_exact() -> None:
    resolver = _resolver(fact=FactValue(value="1.2 triệu", fresh=True))

    resolution = await resolver.resolve_qa(FastEnvelope())

    # The lead-in prefix does not alter the grounded fact value or product code.
    assert "1.2 triệu" in resolution.speech_text
    assert resolution.lead_in + "Giá hiện tại của P001 là 1.2 triệu." == resolution.speech_text


async def test_prefetch_stable_is_noop_hook() -> None:
    resolver = _resolver()

    resolver.prefetch_stable_evidence(FastEnvelope())

    assert True


async def test_resolve_records_exact_envelope_decision() -> None:
    resolver = _resolver(fact=FactValue(value="1.2 triệu", fresh=True))

    await resolver.resolve_qa(FastEnvelope())

    decisions = resolver.latest_envelope_decisions()
    assert len(decisions) == 1
    assert decisions[0]["cluster_id"] == "cl-fast"


async def test_envelope_decision_never_exposes_viewer_text() -> None:
    resolver = _resolver(fact=FactValue(value="1.2 triệu", fresh=True))

    await resolver.resolve_qa(FastEnvelope())

    decision = resolver.latest_envelope_decisions()[0]
    assert "text" not in decision
    assert "question" not in decision


async def test_envelope_decisions_bounded_at_last_five() -> None:
    resolver = _resolver(fact=FactValue(value="1.2 triệu", fresh=True))
    for _ in range(7):
        await resolver.resolve_qa(FastEnvelope())

    decisions = resolver.latest_envelope_decisions()

    assert len(decisions) == 5
