"""Compact continuity state for sequential segment generation (task 8.1).

The state is the only cross-segment context: a bounded previous-segment
tail plus covered IDs/fingerprints. It is validated before use and never
expands to the full prior script.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_serializer, field_validator

# Task 8.8: the prompt-visible tail is bounded by this constant so a long
# script never grows the per-segment prompt with full prior prose.
TAIL_LIMIT_CHARS: int = 300


class ContinuityState(BaseModel):
    """Typed continuity state for segment generation (task 8.1).

    Attributes:
        previous_segment_tail: Bounded previous-segment tail (<= TAIL_LIMIT_CHARS).
        covered_fact_ids: Fact IDs already used, in deterministic order.
        handled_objection_ids: Objection IDs already handled.
        cta_count: CTA count so far.
        opening_fingerprints: Opening fingerprints used so far.
        last_topic: Topic of the previous segment.
        next_topic: Topic the next segment must cover.
    """

    previous_segment_tail: str = Field(
        default="",
        description=f"Bounded previous-segment tail (<= {TAIL_LIMIT_CHARS} chars)",
    )
    covered_fact_ids: frozenset[str] = Field(default_factory=frozenset)
    handled_objection_ids: frozenset[str] = Field(default_factory=frozenset)
    cta_count: int = Field(default=0, ge=0)
    opening_fingerprints: frozenset[str] = Field(default_factory=frozenset)
    last_topic: Optional[str] = None
    next_topic: Optional[str] = None

    @field_validator("previous_segment_tail")
    @classmethod
    def _clip_tail(cls, v: str) -> str:
        """Bound the tail and strip whitespace so the prompt stays compact.

        NOTE: this runs on pydantic's validation path (``model_validate``),
        not on the frozen-model ``__init__`` — callers constructing
        ``ContinuityState`` directly must pre-clip (see ``build_tail``).
        """
        stripped = v.strip()
        if len(stripped) <= TAIL_LIMIT_CHARS:
            return stripped
        return stripped[-TAIL_LIMIT_CHARS:]

    @field_serializer("covered_fact_ids", "handled_objection_ids", "opening_fingerprints")
    def _serialize_sets(self, v: frozenset[str]) -> list[str]:
        return sorted(v)


def build_tail(segment_text: str) -> str:
    """Return a bounded tail of ``segment_text`` for the next prompt (task 8.8).

    The full prior segment never enters the next prompt; only the last
    ``TAIL_LIMIT_CHARS`` characters (the end of speech is what continuity
    must bridge). Cheap and deterministic — no summary call involved.
    """
    return segment_text.strip()[-TAIL_LIMIT_CHARS:]
