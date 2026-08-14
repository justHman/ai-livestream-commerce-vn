"""Entity repository port + in-memory adapter (task 8.4).

The persistence contract for ``EntityDocument`` documents, mirroring the
session-store port: documents are stored/loaded as JSON (pydantic
``model_dump(mode="json")`` / ``model_validate``), so every adapter
(memory today, Postgres/Redis later) shares the same document semantics.

``upsert`` rejects revision regressions: an entity is immutable at or below
the stored revision, so concurrent/late writes can never silently roll back
facts (revision semantics stay honest).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .models import EntityDocument

__all__ = ["EntityRepository", "InMemoryEntityRepository"]


class EntityRepository(ABC):
    """Async persistence port for entity documents."""

    @abstractmethod
    async def upsert(self, entity: EntityDocument) -> None: ...

    @abstractmethod
    async def get(self, entity_id: str) -> Optional[EntityDocument]: ...

    @abstractmethod
    async def delete(self, entity_id: str) -> bool: ...

    @abstractmethod
    async def list_entities(self, entity_type: Optional[str] = None) -> list[EntityDocument]: ...


class InMemoryEntityRepository(EntityRepository):
    """Dict-backed store for single-process runs (dev/Colab/tests).

    Keyed by entity id; JSON document semantics match the session-store
    adapters, so swapping in a Postgres/Redis adapter later changes nothing
    at call sites.
    """

    def __init__(self, initial: Optional[dict[str, EntityDocument]] = None) -> None:
        self._store: dict[str, EntityDocument] = dict(initial or {})

    async def upsert(self, entity: EntityDocument) -> None:
        stored = self._store.get(entity.id)
        if stored is not None and entity.revision <= stored.revision:
            raise RevisionConflictError(
                f"entity {entity.id}: revision {entity.revision} <= stored {stored.revision}"
            )
        self._store[entity.id] = entity

    async def get(self, entity_id: str) -> Optional[EntityDocument]:
        return self._store.get(entity_id)

    async def delete(self, entity_id: str) -> bool:
        existed = entity_id in self._store
        self._store.pop(entity_id, None)
        return existed

    async def list_entities(self, entity_type: Optional[str] = None) -> list[EntityDocument]:
        entities = self._store.values()
        if entity_type is not None:
            entities = (e for e in entities if e.entity_type == entity_type)
        return list(entities)


class RevisionConflictError(ValueError):
    """Raised when upserting an entity at or below the stored revision."""
