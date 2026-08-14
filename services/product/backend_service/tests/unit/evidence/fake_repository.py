"""In-memory fake satisfying the EntityRepository protocol (cluster C10).

Cluster C8's concrete entity store is being implemented in parallel; the
evidence planner is tested against this minimal in-memory stand-in that
implements the same protocol surface.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Optional, Sequence

from backend.application.evidence.models import EntityDocumentView
from backend.application.evidence.repository import EntitySearchResult, EntitySource


class _SearchResult:
    def __init__(self, entity_id: str, name: Optional[str]) -> None:
        self.entity_id = entity_id
        self.name = name


class _Source:
    def __init__(
        self,
        entity_id: str,
        revision: Optional[str],
        selectors: Sequence[str],
        fields: dict[str, Any],
        observed_at: datetime,
    ) -> None:
        self.entity_id = entity_id
        self.revision = revision
        self.selectors = list(selectors)
        self.fields = fields
        self.observed_at = observed_at


class FakeEntityRepository:
    """Dict-backed store; ``search`` matches query substrings against names.

    ``get_entities`` returns a document view per known id, ``fetch_sources``
    returns one source per id covering the requested selectors. Delays and
    fetch counters let tests observe batching and concurrency.
    """

    def __init__(self, entities: dict[str, dict[str, Any]]) -> None:
        self._entities = dict(entities)
        self.search_calls: list[tuple[list[str], Optional[str]]] = []
        self.document_calls: list[tuple[list[str], Optional[Sequence[str]]]] = []
        self.source_calls: list[tuple[list[str], list[str]]] = []
        self._delay = 0.0
        self._active = 0
        self.max_active_fetches = 0

    def set_delay(self, seconds: float) -> None:
        self._delay = seconds

    async def search_entities(
        self, queries: Sequence[str], entity_type: Optional[str] = None
    ) -> list[list[EntitySearchResult]]:
        self.search_calls.append((list(queries), entity_type))
        if self._delay:
            await asyncio.sleep(self._delay)
        out: list[list[EntitySearchResult]] = []
        for q in queries:
            hits = [
                _SearchResult(eid, ent.get("name"))
                for eid, ent in self._entities.items()
                if q.lower() in str(ent.get("name", "")).lower()
            ]
            out.append(hits)
        return out

    async def get_entities(
        self, ids: Sequence[str], selectors: Optional[Sequence[str]] = None
    ) -> list[EntityDocumentView]:
        self.document_calls.append((list(ids), list(selectors) if selectors else None))
        if self._delay:
            await asyncio.sleep(self._delay)
        views: list[EntityDocumentView] = []
        for eid in ids:
            ent = self._entities.get(eid)
            if ent is None:
                continue
            views.append(
                EntityDocumentView(
                    entity_id=eid,
                    name=ent.get("name"),
                    revision=ent.get("revision"),
                    fields={},
                )
            )
        return views

    async def fetch_sources(
        self,
        ids: Sequence[str],
        selectors: Sequence[str],
        requested_at: datetime,
    ) -> list[EntitySource]:
        self.source_calls.append((list(ids), list(selectors)))
        self._active += 1
        self.max_active_fetches = max(self.max_active_fetches, self._active)
        if self._delay:
            await asyncio.sleep(self._delay)
        self._active -= 1
        sources: list[EntitySource] = []
        for eid in ids:
            ent = self._entities.get(eid)
            if ent is None:
                continue
            sources.append(
                _Source(
                    entity_id=eid,
                    revision=ent.get("revision"),
                    selectors=selectors,
                    fields={s: ent.get(s) for s in selectors},
                    observed_at=requested_at,
                )
            )
        return sources


def build_fake() -> FakeEntityRepository:
    """Two products: stable catalog facts + volatile commercial facts."""
    return FakeEntityRepository(
        {
            "P001": {
                "name": "Kem chống nắng SPF50",
                "revision": "rev-1",
                "price": 329000,
                "stock": 42,
                "material": "nỉ cotton",
                "origin": "Việt Nam",
            },
            "P020": {
                "name": "Áo hoodie trắng",
                "revision": "rev-1",
                "price": 350000,
                "stock": 120,
                "material": "nỉ cotton",
                "origin": "Việt Nam",
            },
        }
    )
