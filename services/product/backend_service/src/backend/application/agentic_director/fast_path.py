"""Deterministic factual fast path for the bounded agentic director (C12).

Eligibility and answer construction are pure, code-owned and deterministic
(design Decision 14): when the cluster intent is known, the target product is
resolved with high confidence, and the intent maps to exactly one known fact
selector, the runtime MAY answer with zero LLM calls from fresh authoritative
evidence only — never from model claims (tasks 12.2-12.4).

Trust boundary: the answer text is built ONLY from grounded evidence (entity
display name + exact fact value from ``FactProvider.get_fact``). Missing or
stale evidence yields a typed ``UnavailableAnswer``, never an invented value.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from backend.application.agentic_director.contracts import (
    AnswerText,
    FactualFastPlan,
    PlanKind,
    PlanResult,
    UnavailableAnswer,
    VerbalizationRequest,
)

# Comparative/referential signals that rule the fast path out. A cluster whose
# intent or representative questions carry any of these needs real reasoning.
COMPARISON_MARKERS: tuple[str, ...] = ("so sánh", "vs", "hay là", "hay la")
REFERENTIAL_MARKERS: tuple[str, ...] = ("cái đó", "cái đo", "nó", "con đó", "con đo")

# Intent -> fact selector mapping. All selectors must be in the type registry
# (design Decision 11); unknown intents map to None and fall to the complex path.
INTENT_SELECTOR_MAP: dict[str, str] = {
    "giá": "commerce.price.current",
    "giá gốc": "commerce.price.original",
    "còn hàng": "commerce.stock.available",
    "warranty": "commerce.warranty",
    "bảo hành": "commerce.warranty",
    "giao hàng": "commerce.shipping",
    "shipping": "commerce.shipping",
}

# Volatile facts (price/stock) must be fresh; stable facts may be used stale.
_VOLATILE_SELECTORS: frozenset[str] = frozenset(
    {"commerce.price.current", "commerce.price.original", "commerce.stock.available"}
)


class UntemplatedSelectorError(Exception):
    """No deterministic template exists for this selector.

    Raised instead of fabricating phrasing for a selector the fast path does
    not yet template.
    """


@runtime_checkable
class ClusterEnvelope(Protocol):
    """Structural cluster envelope (design Decision 9).

    The concrete envelope lives in a parallel worktree; this Protocol keeps the
    fast path decoupled from it.
    """

    cluster_id: str
    intent: str
    message_count: int
    unique_viewer_count: int
    representative_questions: tuple[str, ...]
    product_candidates: tuple[tuple[str, float], ...]
    resolved_product_ids: tuple[str, ...]
    ranking_score: float
    novelty: float
    current_script_product_id: str | None
    source_platform_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class FactValue:
    """One authoritative fact value with a freshness flag.

    ``fresh`` matters for volatile facts (price/stock): stale values must not
    be spoken as current.
    """

    value: str
    fresh: bool


@dataclass(frozen=True, slots=True)
class FastPathConfig:
    """Fast-path tuning knobs."""

    min_product_confidence: float = 0.8
    fact_selectors: frozenset[str] = frozenset(
        {
            "commerce.price.current",
            "commerce.price.original",
            "commerce.stock.available",
            "commerce.warranty",
            "commerce.shipping",
        }
    )
    verbalize_where_appropriate: bool = False

    @property
    def known_selectors(self) -> frozenset[str]:
        """Selectors the fast path knows how to answer."""
        return self.fact_selectors


@dataclass(frozen=True, slots=True)
class FastPathEligibility:
    """Result of the deterministic eligibility evaluation.

    ``entity_id`` is set exactly when ``eligible`` is True: the single resolved
    entity the fast path may answer for.
    """

    eligible: bool
    entity_id: str = ""
    reason: str = ""
    selector: str = ""


@runtime_checkable
class FactProvider(Protocol):
    """Authoritative fact source (implemented by a later cluster)."""

    def get_fact(self, entity_id: str, selector: str) -> FactValue | None: ...


@runtime_checkable
class Verbalizer(Protocol):
    """One grounded verbalization generation (implemented by a later cluster).

    The live implementation adapts the LLM seam; a test fake returns a canned
    string. The fast path calls it at most once per answer.
    """

    def verbalize(self, request: VerbalizationRequest) -> str: ...


def select_fact_selector(intent: str) -> str | None:
    """Map a cluster intent to a known fact selector, or None."""
    return INTENT_SELECTOR_MAP.get(intent.strip().lower())


def _product_confidence(envelope: ClusterEnvelope, entity_id: str) -> float:
    """Max confidence among candidates matching the given resolved entity."""
    best = 0.0
    for candidate_id, confidence in envelope.product_candidates:
        if candidate_id == entity_id and confidence > best:
            best = confidence
    return best


def is_fast_path_eligible(
    envelope: ClusterEnvelope,
    config: FastPathConfig,
    selectors: frozenset[str] | None = None,
) -> FastPathEligibility:
    """Evaluate deterministic factual fast-path eligibility for a cluster.

    All conditions must hold: known non-empty intent; at least one resolved
    product id; the intent maps to exactly one known fact selector; product
    confidence at or above the threshold; a single resolved entity; no
    comparison or referential signals.

    ``selectors`` overrides the configured known-selector set (e.g. a runtime
    that only trusts a subset); defaults to ``config.known_selectors``.
    """
    known = config.known_selectors if selectors is None else selectors
    if not envelope.intent or not envelope.intent.strip():
        return FastPathEligibility(False, reason="intent_unknown")
    if not envelope.resolved_product_ids:
        return FastPathEligibility(False, reason="no_resolved_product")
    if len({*envelope.resolved_product_ids}) != 1:
        return FastPathEligibility(False, reason="multiple_entities")
    selector = select_fact_selector(envelope.intent)
    if selector is None:
        return FastPathEligibility(False, reason="selector_unknown")
    if selector not in known:
        return FastPathEligibility(False, reason="selector_unknown")
    lowered_intent = envelope.intent.lower()
    if any(marker in lowered_intent for marker in COMPARISON_MARKERS):
        return FastPathEligibility(False, reason="comparison_signal")
    if any(marker in lowered_intent for marker in REFERENTIAL_MARKERS):
        return FastPathEligibility(False, reason="referential_signal")
    questions = " ".join(envelope.representative_questions).lower()
    if any(marker in questions for marker in REFERENTIAL_MARKERS):
        return FastPathEligibility(False, reason="referential_signal")
    entity_id = envelope.resolved_product_ids[0]
    confidence = _product_confidence(envelope, entity_id)
    if confidence < config.min_product_confidence:
        return FastPathEligibility(False, reason="product_confidence_low")
    return FastPathEligibility(True, entity_id=entity_id, selector=selector)


def build_templated_answer(entity_display_name: str, fact_value: str, selector: str) -> str:
    """Build the exact deterministic Vietnamese answer for a known selector.

    Parameterized ONLY by grounded evidence — the entity display name and the
    exact fact value — never adding numbers the evidence did not provide.
    """
    if selector == "commerce.price.current":
        return f"Giá hiện tại của {entity_display_name} là {fact_value}."
    if selector == "commerce.price.original":
        return f"Giá gốc của {entity_display_name} là {fact_value}."
    if selector == "commerce.stock.available":
        return f"Hiện tại {entity_display_name} {fact_value}."
    if selector == "commerce.warranty":
        return f"Bảo hành của {entity_display_name}: {fact_value}."
    if selector == "commerce.shipping":
        return f"Giao hàng cho {entity_display_name}: {fact_value}."
    raise UntemplatedSelectorError(selector)


class FastPathExecutor:
    """Executes a ``FactualFastPlan`` against authoritative evidence.

    Zero LLM calls for exact templatable answers; at most one verbalization
    generation when configured (task 12.4), with deterministic fallback to the
    exact template if the verbalizer fails. Telemetry is reported through a
    plain callable sink with canonical metric names; the live telemetry module
    is wired up by a later cluster.
    """

    def run_plan(
        self,
        plan: FactualFastPlan,
        envelope: ClusterEnvelope,
        evidence_provider: FactProvider,
        verbalizer: Verbalizer | None,
        config: FastPathConfig,
        metric_sink: Callable[[str, int | float], None] | None = None,
    ) -> PlanResult:
        started = time.monotonic()

        def emit(name: str, value: int | float) -> None:
            if metric_sink is not None:
                metric_sink(name, value)

        entity_id = plan.target_entity_id
        fact = evidence_provider.get_fact(entity_id, plan.fact_selector)
        emit("evidence_ops", 1)
        if fact is None or not fact.fresh:
            emit("llm_calls", 0)
            emit("prompt_tokens", 0)
            emit("generated_tokens", 0)
            emit("latency_ms", int((time.monotonic() - started) * 1000))
            return PlanResult(
                kind=PlanKind.UNAVAILABLE,
                unavailable=UnavailableAnswer(reason="evidence_unavailable"),
            )

        # The envelope carries no display name, so the grounded name is the
        # resolved entity id; display-name resolution is a later cluster's job.
        display_name = entity_id
        templated = build_templated_answer(display_name, fact.value, plan.fact_selector)

        llm_calls = 0
        generated_tokens = 0
        prompt_tokens = 0
        text = templated
        if config.verbalize_where_appropriate and verbalizer is not None:
            request = VerbalizationRequest(
                grounded_fact=f"{plan.fact_selector}: {fact.value}",
                question_context=envelope.intent,
                entity_display_name=display_name,
                entity_type="product",
            )
            try:
                text = verbalizer.verbalize(request)
                llm_calls = 1
            except Exception:
                # Deterministic degradation: keep the exact grounded template.
                text = templated

        emit("llm_calls", llm_calls)
        emit("prompt_tokens", prompt_tokens)
        emit("generated_tokens", generated_tokens)
        emit("latency_ms", int((time.monotonic() - started) * 1000))
        return PlanResult(
            kind=PlanKind.ANSWER,
            answer=AnswerText(
                text=text,
                source_entity_id=entity_id,
                source_selector=plan.fact_selector,
            ),
        )
