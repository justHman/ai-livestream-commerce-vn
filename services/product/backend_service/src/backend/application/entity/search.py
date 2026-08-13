"""Entity search over a collection (task 8.5).

Pure matching helpers for the Director's entity lookup: exact id first,
then name, alias, tag — case- and diacritic-insensitive so Vietnamese
viewer terms ("tai nghe chong on") match stored names ("Tai nghe chống ồn").
``fact_selectors`` narrows results to entities carrying at least one of the
requested fact keys (canonical or ``custom.*``).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

from .models import EntityDocument
from .registry import resolve_key

__all__ = ["search_entities"]


def _normalize(text: str) -> str:
    """Fold case, whitespace, and diacritics for matching."""
    folded = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", folded).strip().lower()


def search_entities(
    entities: Iterable[EntityDocument],
    query: str,
    entity_type: Optional[str] = None,
    fact_selectors: Optional[list[str]] = None,
) -> list[EntityDocument]:
    """Return matching entities ordered by match strength (id > name > alias > tag).

    With ``fact_selectors`` (canonical keys or user labels), only entities
    having at least one of those facts match — labels resolve through the
    registry so "Giá hiện tại" and "commerce.price.current" behave the same.
    """
    needle = _normalize(query)
    if not needle:
        return []
    selectors = {resolve_key(s) for s in fact_selectors} if fact_selectors else None

    def matches(entity: EntityDocument) -> Optional[int]:
        if entity_type is not None and entity.entity_type != entity_type:
            return None
        if selectors is not None and not any(f.key in selectors for f in entity.facts):
            return None
        if _normalize(entity.id) == needle:
            return 0
        if _normalize(entity.name) == needle:
            return 1
        if any(_normalize(a) == needle for a in entity.aliases):
            return 2
        if any(_normalize(t) == needle for t in entity.tags):
            return 3
        return None

    ranked = [
        (strength, entity) for entity in entities if (strength := matches(entity)) is not None
    ]
    ranked.sort(key=lambda pair: pair[0])
    return [entity for _, entity in ranked]
