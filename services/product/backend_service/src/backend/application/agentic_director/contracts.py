"""Agent plan/result contracts for the bounded agentic director (C12).

These types sit ABOVE the model-agnostic LLM seam: the seam
(``OpenAICompatibleClient`` / ``LLMEngine``) is the single place that talks to
a model, while this module defines the typed plans the director decides on and
the typed results it acts on. All contracts are frozen/slots dataclasses so
plans are immutable once decided.

Untrusted-content safety: a plan may carry model-derived or viewer-derived
text, but no raw viewer text (or any full conversation content) is embedded in
telemetry-facing fields. Free-text fields are limited to one ``reason`` on
terminal results and the grounded fact on ``VerbalizationRequest``; anything
that needs to be logged must be logged as an identifier, never the payload.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.application.clients.llm.openai_compatible import ChatMessage  # noqa: F401


@dataclass(frozen=True, slots=True)
class FactualFastPlan:
    """Deterministic fast-path plan: one entity, one known fact selector.

    Eligible when the intent, target entity and requested selector are all
    known and no comparison/referential reasoning is required (design
    Decision 14, factual fast path). Answers come from evidence only; at most
    one verbalization generation is allowed.
    """

    target_entity_id: str
    fact_selector: str


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    """One typed evidence request item inside a ``ComplexPlan``.

    ``entity_id`` is optional because some evidence (e.g. campaign or
    cross-entity facts) is not tied to a single entity.
    """

    selector: str
    entity_id: str | None = None


@dataclass(frozen=True, slots=True)
class ComplexPlan:
    """Bounded complex-path plan: intent + entities + evidence requests.

    The model can request evidence only through these typed items; it never
    invokes functions directly (design Decision 13).
    """

    intent: str
    entities: tuple[str, ...]
    evidence_requests: tuple[EvidenceRequest, ...]
    reasoning_hint: str = ""


class PlanKind:
    """Discriminator values for ``PlanResult.kind``."""

    ANSWER = "answer"
    UNAVAILABLE = "unavailable"
    BUDGET_EXCEEDED = "budget_exceeded"


@dataclass(frozen=True, slots=True)
class AnswerText:
    """Terminal result: a grounded answer for the viewer."""

    text: str
    source_entity_id: str = ""
    source_selector: str = ""


@dataclass(frozen=True, slots=True)
class UnavailableAnswer:
    """Terminal result: the plan could not be answered from evidence."""

    reason: str


@dataclass(frozen=True, slots=True)
class BudgetExceeded:
    """Terminal result: the plan exceeded its execution budget.

    ``limit`` is the allowed budget unit count (e.g. evidence ops or LLM
    calls), ``used`` the actual count, ``op`` the op that crossed the budget.
    """

    limit: int
    used: int
    op: str = ""


@dataclass(frozen=True, slots=True)
class PlanResult:
    """Discriminated result of executing a plan.

    Exactly one of ``answer`` / ``unavailable`` / ``budget`` is set; the
    non-set alternatives stay ``None`` so the kind check is the single
    discriminator.
    """

    kind: str
    answer: AnswerText | None = None
    unavailable: UnavailableAnswer | None = None
    budget: BudgetExceeded | None = None


@dataclass(frozen=True, slots=True)
class VerbalizationRequest:
    """Request for the ONE grounded verbalization generation (task 12.4).

    Only used when a factual answer needs natural phrasing instead of an
    exact template. Carries the grounded fact, the question context and
    entity display info — never raw viewer text.
    """

    grounded_fact: str
    question_context: str
    entity_display_name: str
    entity_type: str = ""
