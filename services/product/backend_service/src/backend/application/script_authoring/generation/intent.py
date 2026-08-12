"""Script intent and transition-policy context (tasks 6.2, Decision 15).

``ScriptIntent`` is the requested authoring intent (e.g. ``OPENING``,
``CLOSING``, ``FEATURE_BENEFIT``, ``OBJECTION_HANDLING``, ``CTA``,
``TRANSITION``, ``CORE_CONTENT``). ``TransitionContext`` carries the
transition policy plus deterministic previous/next product summaries — but
only when the policy allows them: ``ORDER_AWARE`` may bake explicit
transitions, ``ORDER_AGNOSTIC`` strips adjacent-product dependencies and
injects generic entry/exit guidance so the Director can reorder products at
runtime (design Decision 15).
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator

from ..gate.context import TransitionPolicy

__all__ = [
    "ScriptIntent",
    "TransitionContext",
    "build_transition_context",
]


class ScriptIntent(BaseModel):
    """Requested intent for a generation operation.

    ``intent`` names the segment's authoring intent; ``target_duration_s``
    is the requested spoken duration for the segment or full script.
    """

    intent: str = Field(min_length=1)
    target_duration_s: int = Field(ge=1)


class TransitionContext(BaseModel):
    """Transition-policy context for a generation operation (Decision 15).

    ``ORDER_AGNOSTIC`` carries no adjacent-product summaries — only generic
    entry/exit guidance — so the generated core stays usable independently
    of any baked transition. ``ORDER_AWARE`` may carry deterministic
    previous/next product summaries, which the prompt builder renders only
    for that policy.
    """

    policy: TransitionPolicy = "ORDER_AGNOSTIC"
    previous_product_summary: Optional[str] = None
    next_product_summary: Optional[str] = None

    @field_validator("previous_product_summary", "next_product_summary")
    @classmethod
    def _no_blank_summaries(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not v.strip():
            return None
        return v


_ORDER_AGNOSTIC_GUIDANCE: tuple[str, ...] = (
    "Product order is NOT locked: the livestream host may reorder products "
    "at runtime.",
    "Do NOT reference any previous or next product, by name or by summary.",
    "Open with a generic entry line that works as a standalone product "
    "segment.",
    "Close with a generic exit line that works regardless of what product "
    "(if any) follows.",
    "Core sales content must stay usable independently of any baked "
    "transition.",
)


def _entry_exit_guidance(policy: TransitionPolicy) -> str:
    if policy == "ORDER_AGNOSTIC":
        return "\n".join(_ORDER_AGNOSTIC_GUIDANCE)
    return (
        "Product order IS locked for this live: explicit transitions to the "
        "previous/next product are allowed when their summaries are "
        "provided below."
    )


def build_transition_context(
    policy: TransitionPolicy,
    *,
    previous_product_summary: Optional[str] = None,
    next_product_summary: Optional[str] = None,
) -> TransitionContext:
    """Build the transition context, stripping summaries for ORDER_AGNOSTIC.

    Deterministic: an ``ORDER_AGNOSTIC`` policy always drops adjacent-product
    summaries and keeps only generic entry/exit guidance, so no prompt can
    accidentally bake a product-order dependency.
    """
    if policy == "ORDER_AGNOSTIC":
        previous_product_summary = None
        next_product_summary = None
    return TransitionContext(
        policy=policy,
        previous_product_summary=previous_product_summary,
        next_product_summary=next_product_summary,
    )


def transition_guidance(ctx: TransitionContext) -> str:
    """Render the transition-policy guidance block for a prompt."""
    return _entry_exit_guidance(ctx.policy)
