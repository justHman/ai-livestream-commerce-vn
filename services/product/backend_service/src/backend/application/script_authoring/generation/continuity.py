"""Compact continuity state for sequential segment generation (task 8.1).

The state is the only cross-segment context: a bounded previous-segment
tail plus covered IDs/fingerprints. It is validated before use and never
expands to the full prior script.
"""

from __future__ import annotations

import re
from typing import Optional

from pydantic import BaseModel, Field, field_serializer, field_validator

# Task 8.8: the prompt-visible tail is bounded by this constant so a long
# script never grows the per-segment prompt with full prior prose.
# Raised 300 -> 1500 (15.4 real-LLM E2E): the Full Script Gate flags ANY
# 4-gram repeated across segments (REPETITION_CROSS), so the next segment's
# prompt must see enough of the previous segment to avoid reusing its
# phrases. A 300-char tail hid the previous opening hook, so a real LLM
# unknowingly repeated it. 1500 chars covers a full ~1000-char segment and
# still bounds the prompt for long scripts.
TAIL_LIMIT_CHARS: int = 1500

# CTA/closing phrases the gate's REPETITION_CROSS treats as reusable 4-gram
# templates (same vocabulary as gate/rules/repetition.py + thanks closings).
# These are the phrases a real LLM repeats across NON-adjacent segments (e.g.
# segments 1,3,5 of a K=5 script) because the prompt only shows the previous
# segment's tail — tracking them all lets the prompt say "already used: ..."
# (15.4 real-LLM E2E finding).
_CTA_RE = re.compile(
    r"(?i)(mua ngay|đặt ngay|đặt hàng|vào giỏ hàng|chốt đơn|"
    r"nhấn link|bấm link|đừng bỏ lỡ|số lượng có hạn|order ngay|"
    r"cảm ơn|cám ơn|theo dõi|đón xem|chia sẻ)"
)


def extract_ctas(text: str) -> frozenset[str]:
    """Return the CTA/closing phrases in ``text`` (compact set, 15.4)."""
    return frozenset(m.group(0).lower() for m in _CTA_RE.finditer(text))


def closing_fingerprint(segment_text: str) -> Optional[str]:
    """Return the last 4 words of a segment (its closing template), if any."""
    words = re.findall(r"[\w]+", segment_text.lower())
    if len(words) < 4:
        return None
    return " ".join(words[-4:])


class ContinuityState(BaseModel):
    """Typed continuity state for segment generation (task 8.1).

    Attributes:
        previous_segment_tail: Bounded previous-segment tail (<= TAIL_LIMIT_CHARS).
        covered_fact_ids: Fact IDs already used, in deterministic order.
        handled_objection_ids: Objection IDs already handled.
        cta_count: CTA count so far.
        opening_fingerprints: Opening fingerprints used so far.
        used_ctas: CTA/closing phrases used in prior segments (15.4).
        closing_fingerprints: Last-4-word closings used in prior segments (15.4).
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
    used_ctas: frozenset[str] = Field(default_factory=frozenset)
    closing_fingerprints: frozenset[str] = Field(default_factory=frozenset)
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

    @field_serializer(
        "covered_fact_ids",
        "handled_objection_ids",
        "opening_fingerprints",
        "used_ctas",
        "closing_fingerprints",
    )
    def _serialize_sets(self, v: frozenset[str]) -> list[str]:
        return sorted(v)


def build_tail(segment_text: str) -> str:
    """Return a bounded tail of ``segment_text`` for the next prompt (task 8.8).

    The full prior segment never enters the next prompt; only the last
    ``TAIL_LIMIT_CHARS`` characters (the end of speech is what continuity
    must bridge). Cheap and deterministic — no summary call involved.
    """
    return segment_text.strip()[-TAIL_LIMIT_CHARS:]
