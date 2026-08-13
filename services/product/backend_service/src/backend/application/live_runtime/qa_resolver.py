"""Boundary Q&A resolution: deferred final generation (tasks 14.6, 14.8).

The speech arbiter NEVER spends the expensive final Agent generation while a
script sentence plays. At the safe sentence boundary (``QNA_PREPARING``) it
calls ``BoundaryQaResolver.resolve_qa``, which:

1. revalidates volatile evidence just-in-time (``revalidate_volatile`` consults
   the C10 ``EvidenceCache`` for ``VOLATILE_SELECTORS`` — stale/missing
   volatile entries are refreshed before any fact is spoken; a failed
   revalidation yields ``unavailable`` instead of stale speech),
2. runs the deterministic fast path (zero LLM calls) when eligible,
3. falls back to the bounded complex path (``ComplexPathExecutor.run_plan``)
   — the single expensive generation, invoked only at the boundary.

The resolver is the live default of the ``QaResolutionService`` protocol the
arbiter consumes; tests inject recording fakes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from backend.application.agentic_director.complex_path import (
    AgentBudgets,
    ComplexPathExecutor,
    PlanPlanner,
)
from backend.application.agentic_director.contracts import ComplexPlan, PlanKind
from backend.application.agentic_director.evidence_ops import EvidenceExecutor
from backend.application.agentic_director.fast_path import (
    FastPathConfig,
    FastPathExecutor,
    FactProvider,
    FactualFastPlan,
    Verbalizer,
    is_fast_path_eligible,
)
from backend.application.evidence.cache import CacheStatus, EvidenceCache
from backend.application.evidence.models import VOLATILE_SELECTORS

__all__ = ["BoundaryQaResolver", "QaResolution", "QaResolutionService", "VolatileEvidenceSource"]


@dataclass(frozen=True, slots=True)
class QaResolution:
    """Boundary result of one Q&A resolution.

    ``speech_text`` is the single combined turn (lead-in + grounded answer)
    when ``kind == "answer"``; ``unavailable``/``budget_exceeded`` carry an
    empty speech and the script simply continues.
    """

    kind: str
    speech_text: str = ""
    evidence_fresh: bool = True
    lead_in: str = ""

    @classmethod
    def answer(
        cls, speech_text: str, *, lead_in: str = "", evidence_fresh: bool = True
    ) -> "QaResolution":
        return cls(
            kind="answer",
            speech_text=speech_text,
            evidence_fresh=evidence_fresh,
            lead_in=lead_in,
        )

    @classmethod
    def unavailable(cls, reason: str = "") -> "QaResolution":
        return cls(kind="unavailable", evidence_fresh=False)

    @classmethod
    def budget_exceeded(cls) -> "QaResolution":
        return cls(kind="budget_exceeded", evidence_fresh=False)


class QaResolutionService(Protocol):
    """Boundary: resolve one pending Q&A candidate at the safe boundary."""

    async def resolve_qa(self, candidate: Any) -> QaResolution: ...

    def prefetch_stable_evidence(self, candidate: Any) -> None: ...


class VolatileEvidenceSource(Protocol):
    """Just-in-time volatile-evidence revalidation (task 14.8 hook)."""

    def revalidate(self, entity_id: str, selector: str) -> bool: ...


def _default_lead_in(candidate: Any) -> str:
    """Deterministic natural lead-in (design Decision 19), never raw viewer text."""
    topic = candidate.resolved_product_ids[0] if candidate.resolved_product_ids else ""
    return f"Em thấy nhiều anh chị đang hỏi về {topic}... " if topic else ""


class BoundaryQaResolver:
    """Live default resolver: fast path -> complex path at the boundary."""

    def __init__(
        self,
        *,
        fact_provider: FactProvider,
        planner: PlanPlanner,
        evidence_executor: EvidenceExecutor,
        final_generator: Any,
        evidence_cache: EvidenceCache | None = None,
        fast_path_config: FastPathConfig | None = None,
        budgets: AgentBudgets | None = None,
        metric_sink: Callable[[str, int | float], None] | None = None,
        verbalizer: Verbalizer | None = None,
        make_complex_plan: Callable[[Any], ComplexPlan] | None = None,
        max_speech_length: int = 400,
        lead_in_builder: Callable[[Any], str] = _default_lead_in,
    ) -> None:
        self._fact_provider = fact_provider
        self._planner = planner
        self._evidence_executor = evidence_executor
        self._final_generator = final_generator
        self._cache = evidence_cache
        self._fast_path_config = fast_path_config or FastPathConfig()
        self._budgets = budgets or AgentBudgets()
        self._metric_sink = metric_sink
        self._verbalizer = verbalizer
        self._make_complex_plan = make_complex_plan
        self._max_speech_length = max_speech_length
        self._lead_in_builder = lead_in_builder

    def prefetch_stable_evidence(self, candidate: Any) -> None:
        """Hook the arbiter MAY call while the sentence plays (default no-op)."""

    def revalidate_volatile(self, candidate: Any) -> bool:
        """Revalidate volatile facts for the candidate's entities (task 14.8).

        Consults the C10 ``EvidenceCache``: a volatile selector entry that is
        stale or missing must be refreshed before the fact is spoken. Returns
        False when any volatile entry is unusable — the caller then refuses to
        speak stale facts. The full live refresh path is later-cluster wiring;
        this hook is the deterministic boundary.
        """
        if self._cache is None:
            return True
        for entity_id in candidate.resolved_product_ids:
            for selector in VOLATILE_SELECTORS:
                status, _fact = self._cache.get(entity_id, selector, revision=None)
                if status != CacheStatus.HIT:
                    return False
        return True

    async def resolve_qa(self, candidate: Any) -> QaResolution:
        """Resolve one candidate at the boundary; never called mid-sentence."""
        if not self.revalidate_volatile(candidate):
            return QaResolution.unavailable(reason="volatile_evidence_unavailable")

        eligibility = is_fast_path_eligible(candidate, self._fast_path_config)
        if eligibility.eligible:
            result = FastPathExecutor().run_plan(
                FactualFastPlan(
                    target_entity_id=eligibility.entity_id,
                    fact_selector=eligibility.selector,
                ),
                candidate,
                self._fact_provider,
                self._verbalizer,
                self._fast_path_config,
                self._metric_sink,
            )
            if result.kind == PlanKind.ANSWER and result.answer is not None:
                text = result.answer.text[: self._max_speech_length]
                return QaResolution.answer(text, lead_in=self._lead_in_builder(candidate))

        if self._make_complex_plan is not None:
            plan = self._make_complex_plan(candidate)
            result = ComplexPathExecutor().run_plan(
                plan,
                candidate,
                self._planner,
                self._evidence_executor,
                self._final_generator,
                self._budgets,
                self._metric_sink,
            )
            if result.kind == PlanKind.ANSWER and result.answer is not None:
                text = result.answer.text[: self._max_speech_length]
                return QaResolution.answer(text, lead_in=self._lead_in_builder(candidate))
            if result.kind == PlanKind.BUDGET_EXCEEDED:
                return QaResolution.budget_exceeded()

        return QaResolution.unavailable(reason="no_answer")
