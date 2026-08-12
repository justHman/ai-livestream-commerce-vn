"""Generate and Fix prompt builders (tasks 6.3, 6.4; design Decision 5).

Generate is creative: project skill + relevant generation constraints +
authoritative context + requested duration/intent + plan/segment assignment
+ compact continuity state. Fix is constrained repair: immutable source +
exact failed rules' repair instructions + only the authoritative facts
needed to prevent claim drift — and it explicitly forbids broad rewrites,
new claims, and new CTAs unless a failed rule requires them. The two
contracts never share content beyond the typed ``PromptParts`` shell.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from .context_builder import AuthoritativeContext
from .continuity import ContinuityState
from .intent import ScriptIntent, TransitionContext, transition_guidance

__all__ = [
    "PromptParts",
    "OversizedContextError",
    "PromptBuildError",
    "build_generate_prompt",
    "build_repair_prompt",
    "estimate_tokens",
    "guard_budget",
]

# Token-estimation heuristic: one token ~= 4 characters (chars/4).
_CHARS_PER_TOKEN: int = 4


def estimate_tokens(text: str) -> int:
    """Rough token estimate for prompt-budget guarding (chars/4 heuristic).

    Documented as an estimate: the model's real tokenizer may differ by a
    constant factor, which the guard's explicit budget covers. Deterministic
    and dependency-free.
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)


class OversizedContextError(ValueError):
    """Raised when assembled prompt parts exceed the configured token budget.

    Deliberately NOT a truncation: silently dropping skill guidance or
    repair instructions would let a model generate outside its contract, so
    an oversized context fails loudly and predictably (task 6.7).
    """

    def __init__(self, actual_tokens: int, max_tokens: int) -> None:
        super().__init__(
            f"prompt parts exceed token budget: {actual_tokens} > {max_tokens}"
        )
        self.actual_tokens = actual_tokens
        self.max_tokens = max_tokens


class PromptBuildError(ValueError):
    """Raised when a builder receives input it cannot render (task 6.4)."""


class PromptParts(BaseModel):
    """Structured prompt assembly: system / context / user parts.

    Model-facing request is built from these three strings; the parts carry
    no tools, no function-calling schema, and no iteration/control hooks
    (task 6.6). ``parts_keywords`` is a stable, machine-readable marker of
    the contract kind for prompt-contract tests (6.5).
    """

    system: str = ""
    context: str = ""
    user: str = ""
    parts_keywords: tuple[str, ...] = ()


def _quote_section(title: str, text: str) -> str:
    return f"## {title}\n{text.strip()}\n"


def _render_authoritative_context(ctx: AuthoritativeContext) -> str:
    """Render the minimal authoritative context as prompt text.

    ONLY the slices a generation operation may reference are rendered —
    unrelated catalog data never enters a prompt (task 6.1).
    """
    lines: list[str] = []
    if ctx.shop:
        lines.append(
            "Shop: " + "; ".join(f"{k}: {v}" for k, v in sorted(ctx.shop.items()))
        )
    if ctx.persona:
        lines.append(
            "Persona: " + "; ".join(f"{k}: {v}" for k, v in sorted(ctx.persona.items()))
        )
    if ctx.campaign:
        lines.append(
            "Campaign: "
            + "; ".join(f"{k}: {v}" for k, v in sorted(ctx.campaign.items()))
        )
    if ctx.product:
        lines.append(
            "Product: "
            + "; ".join(f"{k}: {v}" for k, v in sorted(ctx.product.items()))
        )
    if ctx.promotions:
        lines.append("Promotions:")
        for promo in ctx.promotions:
            lines.append(
                " - " + "; ".join(f"{k}: {v}" for k, v in sorted(promo.items()))
            )
    if ctx.facts:
        lines.append("Authoritative facts (may be claimed):")
        for fact in ctx.facts:
            lines.append(
                " - " + "; ".join(f"{k}: {v}" for k, v in sorted(fact.items()))
            )
    return "\n".join(lines)


def _render_continuity(state: ContinuityState) -> str:
    """Render the compact continuity state (task 8.8 bounded context)."""
    lines: list[str] = [
        "Continuity state (bounded):",
    ]
    if state.previous_segment_tail:
        lines.append("Previous-segment tail: " + state.previous_segment_tail)
    if state.covered_fact_ids:
        lines.append("Covered fact IDs: " + ", ".join(sorted(state.covered_fact_ids)))
    if state.handled_objection_ids:
        lines.append(
            "Handled objection IDs: "
            + ", ".join(sorted(state.handled_objection_ids))
        )
    lines.append(f"CTA count so far: {state.cta_count}")
    if state.opening_fingerprints:
        lines.append(
            "Used opening fingerprints: "
            + ", ".join(sorted(state.opening_fingerprints))
        )
    if state.last_topic:
        lines.append(f"Last topic: {state.last_topic}")
    if state.next_topic:
        lines.append(f"Next topic: {state.next_topic}")
    return "\n".join(lines)


def _render_transition(ctx: TransitionContext) -> str:
    """Render transition-policy guidance plus allowed summaries only."""
    lines: list[str] = [transition_guidance(ctx)]
    if ctx.previous_product_summary:
        lines.append(f"Previous product summary: {ctx.previous_product_summary}")
    if ctx.next_product_summary:
        lines.append(f"Next product summary: {ctx.next_product_summary}")
    return "\n".join(lines)


def _segment_assignment_text(
    plan: Optional[dict], segment_index: Optional[int]
) -> str:
    """Render the plan/segment assignment block (used by Generate only)."""
    parts: list[str] = []
    if plan:
        parts.append("## Plan assignment")
        parts.append(
            "; ".join(f"{k}: {v}" for k, v in sorted(plan.items()))
        )
    if segment_index is not None:
        parts.append(
            f"## Segment assignment\n"
            f"You are writing segment {segment_index} of this product script."
        )
    return "\n".join(parts)


def build_generate_prompt(
    skill_text: str,
    generation_constraints: list[str],
    context: AuthoritativeContext,
    duration_s: int,
    intent: ScriptIntent,
    transition: TransitionContext,
    *,
    plan: Optional[dict] = None,
    segment_index: Optional[int] = None,
    continuity: Optional[ContinuityState] = None,
    repair_keywords: tuple[str, ...] = (),
) -> PromptParts:
    """Build the creative Generate prompt (tasks 6.3, 6.5).

    The model input includes the project-owned sales skill guidance, the
    relevant generation constraints, authoritative context, requested
    duration/intent, transition policy, and the plan/segment assignment plus
    compact continuity state. Repair-only instructions are never included;
    the returned parts carry only the ``GENERATE_SCRIPT_SEGMENT`` marker.
    """
    system_blocks: list[str] = [
        skill_text.strip() or "(no sales skill provided)",
        "## Generation constraints",
        *(f"- {c}" for c in generation_constraints if c),
    ]
    context_blocks: list[str] = [
        _quote_section("Authoritative context", _render_authoritative_context(context)),
        _quote_section(
            "Requested duration and intent",
            f"Requested spoken duration: {duration_s} seconds\n"
            f"Intent: {intent.intent}",
        ),
        _quote_section("Transition policy", _render_transition(transition)),
    ]
    if continuity is not None:
        context_blocks.append(
            _quote_section("Continuity state", _render_continuity(continuity))
        )

    assignment = _segment_assignment_text(plan, segment_index)
    user_parts: list[str] = [
        "GENERATE_SCRIPT_SEGMENT",
        "Write the assigned product script segment in natural, spoken, "
        "VieNeu-ready Vietnamese. Follow the skill guidance and constraints "
        "exactly; claim only facts from the authoritative context.",
    ]
    if assignment:
        user_parts.append(assignment)
    if continuity is not None:
        user_parts.append(
            "Do not recap the whole product; bridge only from the "
            "continuity state above."
        )
    return PromptParts(
        system="\n\n".join(block for block in system_blocks if block),
        context="\n\n".join(block for block in context_blocks if block),
        user="\n\n".join(user_parts),
        parts_keywords=("GENERATE_SCRIPT_SEGMENT",),
    )


def build_repair_prompt(
    source_text: str,
    failed_rule_ids: list[str],
    rule_repair_instructions: list[str],
    authoritative_facts: AuthoritativeContext,
) -> PromptParts:
    """Build the constrained Fix prompt (tasks 6.4, 6.5).

    Inputs are the immutable source text, the exact failed rule IDs, and the
    repair instructions for exactly those rules, plus ONLY the authoritative
    facts needed to prevent claim drift. The prompt explicitly forbids broad
    rewrites, new claims, and new CTAs unless a failed rule requires them.
    No sales skill and no repair-unrelated rules are ever included.
    """
    if not source_text.strip():
        raise PromptBuildError("repair source text is empty")
    if not failed_rule_ids:
        raise PromptBuildError("repair requires at least one failed rule id")
    if len(failed_rule_ids) != len(rule_repair_instructions):
        raise PromptBuildError(
            "repair instructions must match failed rule ids one-to-one"
        )

    system_blocks: list[str] = [
        "You are repairing a gate-failed script version with MINIMAL changes.",
        "## Constraints",
        "Fix ONLY the failed rules listed below. Do NOT rewrite the script, "
        "do NOT add new claims, and do NOT add new calls to action unless a "
        "listed failed rule requires it.",
        "Preserve compliant wording, meaning, structure, tone, and factual "
        "claims unless a listed failed rule requires a change.",
        "Claim only facts from the authoritative context below.",
    ]
    context_blocks: list[str] = [
        _quote_section(
            "Failed rules",
            "\n".join(
                f"- {rule_id}: {instruction}"
                for rule_id, instruction in zip(
                    failed_rule_ids, rule_repair_instructions
                )
                if instruction
            ),
        ),
        _quote_section(
            "Authoritative facts (anti-drift)",
            _render_authoritative_context(authoritative_facts),
        ),
    ]
    user_parts: list[str] = [
        "REPAIR_SCRIPT_SEGMENT",
        "Apply the minimum edits to the source text below that satisfy the "
        "failed rules.",
        f"## Immutable source text\n{source_text.strip()}",
        "Return the repaired full text.",
    ]
    return PromptParts(
        system="\n\n".join(system_blocks),
        context="\n\n".join(context_blocks),
        user="\n\n".join(user_parts),
        parts_keywords=("REPAIR_SCRIPT_SEGMENT", *failed_rule_ids),
    )


def guard_budget(parts: PromptParts, max_tokens: int) -> PromptParts:
    """Guard the assembled parts against the token budget (task 6.7).

    Raises:
        OversizedContextError: total estimated tokens exceed ``max_tokens``.
            Never truncates — critical constraints must not be silently
            dropped.
    """
    total = estimate_tokens(parts.system) + estimate_tokens(parts.context) + estimate_tokens(parts.user)
    if total > max_tokens:
        raise OversizedContextError(total, max_tokens)
    return parts
