"""Tests for the entity repository (task 8.4): revision guard + CRUD round-trip."""

from __future__ import annotations

import pytest

from backend.application.entity.models import EntityDocument
from backend.application.entity.repository import InMemoryEntityRepository, RevisionConflictError

from .fixtures import FASHION


async def test_upsert_get_round_trip_returns_same_document() -> None:
    repo = InMemoryEntityRepository()
    await repo.upsert(FASHION)

    assert await repo.get(FASHION.id) == FASHION


async def test_get_returns_none_for_unknown_id() -> None:
    repo = InMemoryEntityRepository()

    assert await repo.get("product:missing") is None


async def test_upsert_rejects_lower_revision() -> None:
    repo = InMemoryEntityRepository(initial={FASHION.id: FASHION})
    regressed = FASHION.model_copy(update={"revision": FASHION.revision - 1})

    with pytest.raises(RevisionConflictError):
        await repo.upsert(regressed)


async def test_upsert_rejects_equal_revision() -> None:
    repo = InMemoryEntityRepository(initial={FASHION.id: FASHION})

    with pytest.raises(RevisionConflictError):
        await repo.upsert(FASHION)


async def test_upsert_accepts_higher_revision() -> None:
    repo = InMemoryEntityRepository(initial={FASHION.id: FASHION})
    bumped = FASHION.model_copy(update={"revision": FASHION.revision + 1})

    await repo.upsert(bumped)

    assert (await repo.get(FASHION.id)).revision == FASHION.revision + 1


async def test_delete_returns_true_and_removes() -> None:
    repo = InMemoryEntityRepository(initial={FASHION.id: FASHION})

    assert await repo.delete(FASHION.id) is True
    assert await repo.get(FASHION.id) is None


async def test_delete_missing_returns_false() -> None:
    repo = InMemoryEntityRepository()

    assert await repo.delete("product:missing") is False


async def test_list_entities_returns_all() -> None:
    repo = InMemoryEntityRepository(
        initial={
            FASHION.id: FASHION,
            "product:other": FASHION.model_copy(update={"id": "product:other"}),
        }
    )

    assert {e.id for e in await repo.list_entities()} == {FASHION.id, "product:other"}


async def test_list_entities_filters_by_type() -> None:
    repo = InMemoryEntityRepository(initial={FASHION.id: FASHION})

    assert await repo.list_entities(entity_type="shop") == []


async def test_document_json_round_trip_through_repository() -> None:
    repo = InMemoryEntityRepository()
    restored = EntityDocument.model_validate(FASHION.model_dump(mode="json"))

    await repo.upsert(restored)

    assert await repo.get(FASHION.id) == restored
