"""Query-relevant entity context rendering (task 8.6).

Turns an entity document into a compact text bundle for the Agent/LLM —
the query-shaped subset, never full-document serialization (Decision 13).
With ``selectors`` only the requested facts render and only knowledge blocks
whose kind/title/tags relate to the selector topic; a "Price question"
selects ``commerce.price.current`` and pulls no warranty/usage prose.
Without selectors every fact renders and each block is truncated.
Volatile facts carry their ``updated_at`` so freshness is visible to the
reader; task 8.9 later wires this to approval freshness.
"""

from __future__ import annotations

from typing import Optional

from .models import EntityDocument, Fact, KnowledgeBlock
from .registry import is_volatile, resolve_key

__all__ = ["render_entity_context"]


def _selectors_to_keys(selectors: Optional[list[str]]) -> Optional[set[str]]:
    """Normalize selectors to canonical keys (labels resolve via the registry)."""
    if selectors is None:
        return None
    return {resolve_key(s) for s in selectors}


def _fact_line(fact: Fact) -> str:
    """One rendered fact line; volatile facts show their updated_at."""
    value = f"{fact.value} {fact.unit}".rstrip() if fact.unit else str(fact.value)
    freshness = f" (updated {fact.updated_at})" if is_volatile(fact.key) else ""
    return f"  {fact.key}: {value}{freshness}"


def _block_topics(block: KnowledgeBlock) -> list[str]:
    """Tokens used to judge whether a block belongs to a selector's topic."""
    return [block.kind, block.title, *block.tags]


def _render_block(block: KnowledgeBlock, max_chars: int) -> str:
    """Render one block, truncated to ``max_chars`` chars — prose is retrieved
    per query, so blocks never serialize whole-document."""
    content = block.content if len(block.content) <= max_chars else f"{block.content[:max_chars]}…"
    return f"  [{block.id}] {block.title or block.kind}: {content}"


def render_entity_context(
    entity: EntityDocument,
    selectors: Optional[list[str]] = None,
    max_block_chars: int = 400,
) -> str:
    """Render the query-relevant subset of an entity as compact text.

    With ``selectors``: only the selected facts render, and only knowledge
    blocks whose kind/title/tags overlap the selectors' topics. Without:
    all facts plus each block truncated to ``max_block_chars``.
    """
    lines = [f"#{entity.id} | {entity.name} | rev {entity.revision}"]

    selector_keys = _selectors_to_keys(selectors)
    if selector_keys is not None:
        wanted = sorted(selector_keys)
        facts = [f for f in entity.facts if f.key in wanted]
        blocks = [
            b
            for b in entity.knowledge_blocks
            if set(_block_topics(b)) & _topic_words(wanted, facts)
        ]
    else:
        facts = entity.facts
        blocks = entity.knowledge_blocks

    for fact in sorted(facts, key=lambda f: f.key):
        lines.append(_fact_line(fact))
    for block in blocks:
        lines.append(_render_block(block, max_block_chars))
    return "\n".join(lines)


def _topic_words(selector_keys: set[str], facts: list[Fact]) -> set[str]:
    """Topic tokens for block relevance: selector key tokens + fact labels.

    Selector ``commerce.price.current`` (labels "Giá hiện tại", "Giá bán",
    "Giá", "Price") therefore matches blocks tagged "giá" but not warranty or
    usage prose — the spec's "Price question" scenario.
    """
    tokens: set[str] = set()
    for key in selector_keys:
        tokens.update(key.split("."))
    for fact in facts:
        tokens.update(fact.labels)
    return tokens
