"""Application-op contracts for entity evidence (cluster C10).

C8 (entity context) is implemented in parallel and its concrete store is not
in this branch; the planner and cache run against these protocols, and C8's
repository only has to satisfy them. Signatures are the stable interface the
OpenSpec change pins: ``search_entities`` / ``get_entities`` /
``get_documents``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Protocol, Sequence

from backend.application.evidence.models import EntityDocumentView

__all__ = [
    "EntityRepository",
    "EntitySearchResult",
    "EntitySource",
]


class EntitySource(Protocol):
    """One authoritative read of an entity at a given instant."""

    entity_id: str
    revision: Optional[str]
    selectors: Sequence[str]
    fields: dict[str, Any]
    observed_at: datetime


class EntitySearchResult(Protocol):
    """One search hit: id plus optional display name."""

    entity_id: str
    name: Optional[str]


class EntityRepository(Protocol):
    """Authoritative entity store behind the evidence planner (C8's surface).

    ``search_entities`` resolves free-text queries to ids; ``get_entities``
    returns document views for known ids. All three are async because the
    real store is remote, and ``fetch_sources`` returns typed source values
    so the cache can store ``Fact`` entries without knowing the store's
    internals.
    """

    async def search_entities(
        self, queries: Sequence[str], entity_type: Optional[str] = None
    ) -> list[list[EntitySearchResult]]: ...

    async def get_entities(
        self, ids: Sequence[str], selectors: Optional[Sequence[str]] = None
    ) -> list[EntityDocumentView]: ...

    async def fetch_sources(
        self,
        ids: Sequence[str],
        selectors: Sequence[str],
        requested_at: datetime,
    ) -> list[EntitySource]: ...
