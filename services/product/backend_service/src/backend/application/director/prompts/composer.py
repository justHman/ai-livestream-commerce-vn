"""Decision and fallback prompt composition (OpenSpec 1.13).

Static sections come only from the validated cached bundle (loader). Runtime
shop/product/comment/session values are serialized as untrusted data inside
explicit begin/end delimiters and cannot select, reorder, or replace static
files. Guardrails are immutable: they always appear verbatim from the cache and
can never be overridden by runtime context.

Exact order:
  Decision: base sales -> response guardrails -> director decision
            -> delimited untrusted runtime context
  Fallback: base sales -> response guardrails -> fallback response
            -> delimited untrusted runtime context (only available pieces)

Diagnostics expose only bundle identity/hash and token counts — never rendered
prompt text or customer values.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .loader import PromptBundle, load_bundle

BOUNDARY_BEGIN = "<<<UNTRUSTED_CONTEXT_BEGIN>>>"
BOUNDARY_END = "<<<UNTRUSTED_CONTEXT_END>>>"


@dataclass(frozen=True, slots=True)
class ContextBundle:
    """Untrusted runtime context serialized into delimited blocks."""

    values: Mapping[str, str] = field(default_factory=dict)

    def to_blocks(self) -> str:
        """Serialize a mapping of named untrusted values.

        Values are escaped so that a runtime string cannot terminate the block
        or inject a fake static system section: boundary markers inside a value
        are replaced with visibly escaped placeholders.
        """
        if not self.values:
            return ""
        lines = [BOUNDARY_BEGIN]
        for key in sorted(self.values):
            raw = self.values[key]
            escaped = (
                raw.replace(BOUNDARY_BEGIN, "<escaped:untrusted_begin>")
                .replace(BOUNDARY_END, "<escaped:untrusted_end>")
            )
            lines.append(f"[{key}]\n{escaped}")
        lines.append(BOUNDARY_END)
        return "\n".join(lines)


def _serialize_context(values: Mapping[str, str] | None) -> str:
    if not values:
        return ""
    return ContextBundle(values=values).to_blocks()


def compose_decision_prompt(
    *,
    bundle: PromptBundle | None = None,
    context: Mapping[str, str] | None = None,
) -> str:
    """Compose the decision flow: base -> guardrails -> decision -> context.

    Guardrails are appended before any runtime context, so they are immutable
    with respect to untrusted data.
    """
    b = bundle or load_bundle()
    base = b.prompt("base_sales_vi")
    guardrails = b.prompt("response_guardrails_vi")
    decision = b.prompt("director_decision_vi")
    context_block = _serialize_context(context)
    return "\n\n".join(
        part for part in (base, guardrails, decision, context_block) if part
    )


def compose_fallback_prompt(
    *,
    bundle: PromptBundle | None = None,
    context: Mapping[str, str] | None = None,
) -> str:
    """Compose the fallback flow: base -> guardrails -> fallback -> context.

    Fallback selects when required context is absent or the model is
    unavailable/invalid. Only available context pieces are included.
    """
    b = bundle or load_bundle()
    base = b.prompt("base_sales_vi")
    guardrails = b.prompt("response_guardrails_vi")
    fallback = b.prompt("fallback_response_vi")
    context_block = _serialize_context(context)
    return "\n\n".join(
        [part for part in (base, guardrails, fallback, context_block) if part]
    )


def select_flow(
    *,
    has_required_context: bool,
    model_available: bool = True,
    model_output_valid: bool = True,
) -> str:
    """Choose the flow name: 'decision' or 'fallback'.

    Falls back unless every required condition holds. This explicit gate keeps
    flow selection deterministic and separable from prompt text.
    """
    if not has_required_context or not model_available or not model_output_valid:
        return "fallback"
    return "decision"


__all__ = [
    "BOUNDARY_BEGIN",
    "BOUNDARY_END",
    "ContextBundle",
    "compose_decision_prompt",
    "compose_fallback_prompt",
    "select_flow",
]
