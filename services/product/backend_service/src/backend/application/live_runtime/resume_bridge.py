"""Deterministic script-resume bridge after Q&A (task 14.9, design Decision 19).

After a Q&A interleave, the runtime may speak a concise natural bridge back
to the current script product. The bridge is a pure deterministic template
parameterized ONLY by the script product id/name and optional prior-sentence
metadata — never an LLM call (a separate bridge-only LLM call is NOT part of
the normal path). ``build_resume_bridge`` returns "" when the bridge is
disabled; ``should_speak_bridge`` is the pure predicate deciding whether a
bridge improves continuity (enabled, script not finished, and a product
switch happened or there was no previous product).
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_RESUME_BRIDGE_TEMPLATE",
    "build_resume_bridge",
    "should_speak_bridge",
]

DEFAULT_RESUME_BRIDGE_TEMPLATE: str = "Rồi, em tiếp tục với {product} nhé."


def build_resume_bridge(
    product_id: str,
    template: str | None = DEFAULT_RESUME_BRIDGE_TEMPLATE,
    *,
    previous_product: str | None = None,
    next_sentence_excerpt: str = "",
) -> str:
    """Deterministic Vietnamese resume bridge, or "" when disabled.

    ``template`` defaults to ``DEFAULT_RESUME_BRIDGE_TEMPLATE``; ``None``
    disables the bridge entirely (returns ""). ``previous_product`` and
    ``next_sentence_excerpt`` are accepted for metadata continuity but the
    default template is parameterized by product only.
    """
    if template is None:
        return ""
    if template == "":
        return ""
    product = product_id or "sản phẩm"
    return template.format(product=product)


def should_speak_bridge(
    *,
    config_enabled: bool,
    script_finished: bool,
    previous_product: str | None,
    current_product: str,
) -> bool:
    """Pure predicate: speak a bridge only when it improves continuity.

    False when disabled, when the script is finished, or when Q&A returned
    to the SAME product (a bridge then is usually useless noise). A switch
    (or no previous product) is what the bridge smooths.
    """
    if not config_enabled or script_finished:
        return False
    if not current_product:
        return False
    return previous_product != current_product
