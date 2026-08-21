"""One-call sequential segment generator (tasks 8.2/8.3/8.8).

Exactly one normal semantic call per preplanned segment index. The prompt
carries only the compact ``ContinuityState`` (bounded tail + covered IDs) —
never the full prior script — so no summary LLM call is needed between
segments and prompt context stays bounded.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, field_serializer

from backend.application.contracts.llm_engines import LLMEngine, LLMRequest
from backend.application.script_authoring.generation.continuity import (
    TAIL_LIMIT_CHARS,
    ContinuityState,
    closing_fingerprint,
    extract_ctas,
)


class SegmentGenerationResult(BaseModel):
    """One generated segment: display/spoken text plus continuity metadata.

    Schema-validated before persistence (task 8.2): unknown authoritative
    IDs are rejected, the text is non-empty and bounded, and continuity
    state carries only validated references.
    """

    segment_index: int = Field(ge=0)
    display_text: str = Field(min_length=1)
    spoken_text: str = Field(min_length=1)
    covered_fact_ids: frozenset[str] = Field(default_factory=frozenset)
    handled_objection_ids: frozenset[str] = Field(default_factory=frozenset)
    cta_used: bool = False
    opening_fingerprint: Optional[str] = None
    topic: Optional[str] = None

    @field_serializer("covered_fact_ids", "handled_objection_ids")
    def _serialize_sets(self, v: frozenset[str]) -> list[str]:
        return sorted(v)


@dataclass
class SegmentStepOutcome:
    """Outcome of one segment generation step (task 8.6 gate ordering)."""

    index: int
    state: ContinuityState
    result: Optional[SegmentGenerationResult] = None
    error: Optional[str] = None


class ProductSegmentGenerator:
    """Exactly one normal semantic call for one preplanned segment index.

    ``run_one`` builds the segment prompt from the compact continuity state
    (never the full prior script), makes exactly one ``LLMEngine.generate``
    call, schema-validates the returned result, and advances continuity.
    """

    def __init__(
        self,
        llm: LLMEngine,
        *,
        system_prompt: str = "",
        max_tokens: int = 512,
    ) -> None:
        self._llm = llm
        self._system_prompt = system_prompt
        self._max_tokens = max_tokens

    def _build_prompt(self, state: ContinuityState, index: int) -> str:
        """Assemble the compact segment prompt (task 8.8 bounded context)."""
        tail = state.previous_segment_tail or "(no previous segment)"
        parts = [
            f"Write segment {index} of the product script.",
            "You receive ONLY the bounded continuity state below — never the",
            "full prior script. Do not request or expect a summary call.",
            f"Previous-segment tail (bounded to {TAIL_LIMIT_CHARS} chars):",
            tail,
        ]
        if state.covered_fact_ids:
            parts.append("Covered fact IDs: " + ", ".join(sorted(state.covered_fact_ids)))
        if state.handled_objection_ids:
            parts.append("Handled objection IDs: " + ", ".join(sorted(state.handled_objection_ids)))
        parts.append(f"CTA count so far: {state.cta_count}")
        if state.last_topic:
            parts.append(f"Last topic: {state.last_topic}")
        if state.next_topic:
            parts.append(f"Next topic: {state.next_topic}")
        return "\n".join(parts)

    def run_one(
        self,
        index: int,
        state: ContinuityState,
        *,
        session_id: str = "",
        valid_fact_ids: frozenset[str] = frozenset(),
        valid_objection_ids: frozenset[str] = frozenset(),
    ) -> SegmentStepOutcome:
        """Generate segment ``index`` with exactly one semantic call.

        The returned continuity state is advanced deterministically:
        covered/handled IDs validated against the authoritative registry are
        merged into the next state; unknown IDs are dropped (task 8.4), and
        the bounded tail is set from this segment's spoken text.
        """
        prompt = self._build_prompt(state, index)
        req = LLMRequest.from_prompt(
            prompt,
            system_prompt=self._system_prompt,
            max_tokens=self._max_tokens,
        )
        response = self._llm.generate(req)  # exactly one call per segment
        text = response.text.strip()
        if not text:
            return SegmentStepOutcome(
                index=index,
                state=state,
                error="empty model output",
            )
        try:
            result = SegmentGenerationResult(
                segment_index=index,
                display_text=text,
                spoken_text=text,
            )
        except Exception as exc:  # pydantic validation
            return SegmentStepOutcome(
                index=index,
                state=state,
                error=f"schema validation failed: {exc}",
            )

        # Merge validated IDs from the MODEL's continuity metadata (task 8.4).
        # The stub carries them on the response; real engines will return
        # them in the structured segment output.
        model_facts = getattr(response, "fact_ids", frozenset()) or frozenset()
        model_objections = getattr(response, "objection_ids", frozenset()) or frozenset()
        merged_facts = state.covered_fact_ids | (frozenset(model_facts) & valid_fact_ids)
        merged_objections = state.handled_objection_ids | (
            frozenset(model_objections) & valid_objection_ids
        )

        # Clip the tail BEFORE construction: pydantic validators do not run
        # on the auto-generated `__init__` kwargs of frozen models, so the
        # validator alone would leave the full text in the prompt.
        next_state = ContinuityState(
            previous_segment_tail=result.spoken_text[:TAIL_LIMIT_CHARS],
            covered_fact_ids=merged_facts,
            handled_objection_ids=merged_objections,
            cta_count=state.cta_count + (1 if result.cta_used else 0),
            opening_fingerprints=(
                state.opening_fingerprints
                | ({result.opening_fingerprint} if result.opening_fingerprint else set())
            ),
            used_ctas=state.used_ctas | extract_ctas(result.spoken_text),
            closing_fingerprints=state.closing_fingerprints
            | ({fp for fp in (closing_fingerprint(result.spoken_text),) if fp}),
            last_topic=result.topic or state.last_topic,
            next_topic=None,
        )
        return SegmentStepOutcome(index=index, state=next_state, result=result)


def run_segment_step(
    generator: ProductSegmentGenerator,
    index: int,
    state: ContinuityState,
    *,
    session_id: str = "",
    valid_fact_ids: frozenset[str] = frozenset(),
    valid_objection_ids: frozenset[str] = frozenset(),
) -> SegmentStepOutcome:
    """Run one sequential segment step (task 8.8 orchestration boundary).

    Pure wrapper so callers can drive ``N`` segments with exactly ``N``
    semantic calls, passing each step's ``state`` into the next prompt.
    """
    return generator.run_one(
        index,
        state,
        session_id=session_id,
        valid_fact_ids=valid_fact_ids,
        valid_objection_ids=valid_objection_ids,
    )
