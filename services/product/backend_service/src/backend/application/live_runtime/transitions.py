"""Canonical deterministic Vietnamese natural transitions (tasks 15.1-15.5, Decision 19).

Lead-in and resume bridges are deterministic Vietnamese templates
parameterized ONLY by topic, product display name/code, current script
product, and optional prior-sentence metadata — never raw viewer text and
never an LLM call. ``build_qa_lead_in`` is the single lead-in surface;
the resume bridge (``build_resume_bridge``/``should_speak_bridge``) is
re-exported from ``resume_bridge.py`` so this module is the one
natural-transitions surface.
"""

from __future__ import annotations

from .resume_bridge import (
    DEFAULT_RESUME_BRIDGE_TEMPLATE,
    build_resume_bridge,
    should_speak_bridge,
)

__all__ = [
    "DEFAULT_QA_LEAD_IN_TEMPLATE",
    "DEFAULT_RESUME_BRIDGE_TEMPLATE",
    "INTENT_TOPIC_PHRASES",
    "build_qa_lead_in",
    "build_resume_bridge",
    "should_speak_bridge",
]

# Trailing ". " so direct concatenation with the grounded answer reads
# naturally as one turn: "Em thấy nhiều anh chị đang hỏi P020 có hỗ trợ sạc
# nhanh không. P020 có sạc nhanh 65W nha."
DEFAULT_QA_LEAD_IN_TEMPLATE: str = "Em thấy nhiều anh chị đang hỏi {product} {topic_phrase}. "

FALLBACK_QA_LEAD_IN_TEMPLATE: str = "Em thấy nhiều anh chị đang hỏi về {product}. "

# Small deterministic cluster-intent -> natural topic-phrase mapping. The
# fixture intents ("sạc nhanh", "giá") are the spec examples; unknown intents
# fall back to the "về {product}" form — no speculative table.
INTENT_TOPIC_PHRASES: dict[str, str] = {
    "sạc nhanh": "có hỗ trợ sạc nhanh không",
    "giá": "giá bao nhiêu",
}


def build_qa_lead_in(product: str, topic_phrase: str | None = None) -> str:
    """Deterministic natural Q&A lead-in (Decision 19), never raw viewer text.

    The viewer question is always paraphrased through the template — the raw
    ``representative_questions`` text must never be spoken as a standalone.
    ``product`` falls back to "sản phẩm" when empty; ``topic_phrase`` falls
    back to the "về {product}" form when None/empty.
    """
    product = product or "sản phẩm"
    if topic_phrase:
        return DEFAULT_QA_LEAD_IN_TEMPLATE.format(
            product=product, topic_phrase=topic_phrase
        )
    return FALLBACK_QA_LEAD_IN_TEMPLATE.format(product=product)
