"""Decision preparation — prompt composition, model generation, prepared variants.

Owns the LLM generation for a prepared Director decision: composing the
prompt layers (via the canonical prompt bundle), generating answer variants
bounded by the cache depth, and returning the prepared script set. The
Decision FSM (decision.py) chooses WHAT to say; this module prepares HOW it
is worded before playback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["PrepareResult", "generate_variants", "prompt_layers_for"]


@dataclass
class PrepareResult:
    """Prepared LLM output for one decision."""

    script: str
    variants: tuple[str, ...] = ()


def generate_variants(
    llm: Any,
    prompt: str,
    system_prompt: Optional[str],
    *,
    variant_count: int,
    session_id: str,
    utterance_id: str,
) -> PrepareResult:
    """Generate up to ``variant_count`` answer scripts via the LLM stream.

    Runs the blocking ``stream_chunks`` inside the caller's thread (the
    coordinator calls this from ``asyncio.to_thread``). ``variant_count``
    bounds the LLM cost per decision; cache rotation happens in the FSM.
    """
    from llm.engines.base import LLMRequest

    request = LLMRequest.from_prompt(prompt, system_prompt=system_prompt or None)
    variants: list[str] = []
    for variant_index in range(max(1, variant_count)):
        text = "".join(
            chunk.text
            for chunk in llm.stream_chunks(
                request,
                session_id=session_id,
                utterance_id=f"{utterance_id}:{variant_index}",
            )
        )
        variants.append(text)
    return PrepareResult(script=variants[0], variants=tuple(variants))


def prompt_layers_for(
    session: Any,
    decision: Any,
    *,
    stage_task: str = "",
) -> dict[str, str]:
    """Compose the prompt-layer dict for a decision from the canonical bundle.

    Delegates to the session's composer seam (``session.prompt_layers`` when
    available); the coordinator fallback uses the legacy BASE_SALE_PERSONA
    for sessions without a Director runtime attached.
    """
    from backend.application.director.prompts.composer import (
        compose_decision_prompt,
        compose_fallback_prompt,
        select_flow,
    )
    from backend.application.director.prompts.loader import load_bundle

    bundle = load_bundle()
    context: dict[str, str] = {}
    if stage_task:
        context["stage_task"] = stage_task
    if session is not None and getattr(session, "shop_profile", ""):
        context["shop_profile"] = session.shop_profile
    flow = select_flow(has_required_context=bool(stage_task))
    if flow == "fallback":
        final_prompt = compose_fallback_prompt(bundle=bundle, context=context or None)
    else:
        final_prompt = compose_decision_prompt(bundle=bundle, context=context or None)
    return {
        "base_role": bundle.prompt("base_sales_vi"),
        "shop_profile": (session.shop_profile if session is not None else ""),
        "stage_task": stage_task,
        "final_prompt": final_prompt,
    }
