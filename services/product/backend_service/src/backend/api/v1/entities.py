"""backend.api.v1.entities — Shop/Product Data Studio REST surface (tasks 9.1-9.8).

The Entity Data Studio is a simple editor over the universal entity document:
a simple common-field form (9.1), arbitrary user-facing label/value rows
mapped through the fact registry (9.2), raw/pasted knowledge blocks (9.4),
optional AI extraction suggestions (9.5), and the exact query-relevant
context renderer as a preview (9.8). Task 9.7 (advanced normalized document
view) is satisfied by GET /entities/{id} returning the full document.

Suggestion semantics (task 9.6): a suggestion becomes authoritative ONLY when
the operator explicitly includes it — the PUT /entities/{id} save flow
re-sends accepted rows as ``fact_rows``/``common``. There is no separate
accept endpoint; the save path is the single authority and never depends on
the extraction seam.

Save semantics (task 9.2-9.4): the request is the FULL editor state. Every
save REPLACES all fact rows and knowledge blocks (entity revision = stored
revision + 1, per-fact/per-block revision resets to 1); the repository's
revision guard still rejects concurrent stale writes with 409.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal, Optional

from fastapi import Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from backend.api.dependencies import container_from_request
from backend.application.entity.extraction import (
    SuggestionResponse,
    fact_from_common,
    fact_from_row,
    suggest_facts,
)
from backend.application.entity.models import (
    EntityDocument,
    KnowledgeBlock,
    new_id,
)
from backend.application.entity.registry import (
    COMMERCE_PRICE_CURRENT,
    COMMERCE_PRICE_ORIGINAL,
    COMMERCE_PROMOTION,
    COMMERCE_SHIPPING,
    COMMERCE_STOCK_AVAILABLE,
    COMMERCE_STOCK_QUANTITY,
    COMMERCE_WARRANTY,
    IDENTITY_BRAND,
    IDENTITY_SKU,
)
from backend.application.entity.repository import RevisionConflictError
from backend.application.entity.render import render_entity_context

from .auth import viewer_auth
from .router import router as _router

logger = logging.getLogger(__name__)

__all__ = ["_router"]

EntityType = Literal["product", "shop", "campaign"]
BlockKind = Literal["description", "usage", "story", "campaign", "custom"]

_COMMON_KEYS = frozenset(
    (
        COMMERCE_PRICE_CURRENT,
        COMMERCE_PRICE_ORIGINAL,
        COMMERCE_STOCK_AVAILABLE,
        COMMERCE_STOCK_QUANTITY,
        COMMERCE_PROMOTION,
        COMMERCE_SHIPPING,
        COMMERCE_WARRANTY,
        IDENTITY_BRAND,
        IDENTITY_SKU,
    )
)

_EntityId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


def _domain_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build the stable ``{"error": {"code", "message"}}`` envelope (see scripts.py)."""
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _repo(request: Request):
    """The container-scoped entity repository (always wired, memory default)."""
    return container_from_request(request).entity_repo


# ── Request/response models (stable wire contract) ──────────────────


class FactRowIn(BaseModel):
    """One user-facing label/value row (task 9.2).

    The label resolves through the fact registry; unknown labels become
    ``custom.<slug>`` facts instead of being rejected (task 9.3).
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)
    unit: Optional[str] = Field(default=None, max_length=64)


class KnowledgeBlockIn(BaseModel):
    """One raw/pasted knowledge block (task 9.4)."""

    model_config = ConfigDict(extra="forbid")

    kind: BlockKind = "custom"
    title: str = Field(default="", max_length=256)
    content: str = Field(min_length=1, max_length=20_000)
    tags: list[str] = Field(default_factory=list, max_length=32)


class SimpleEntityUpsertReq(BaseModel):
    """The Data Studio form: simple common fields + arbitrary rows + prose.

    ``common`` accepts ONLY the canonical keys below (unknown keys are a 400);
    each entry is a string the UI typed, coerced to the registry type.
    """

    model_config = ConfigDict(extra="forbid")

    id: _EntityId
    entity_type: EntityType = "product"
    name: str = Field(min_length=1, max_length=256)
    aliases: list[str] = Field(default_factory=list, max_length=32)
    tags: list[str] = Field(default_factory=list, max_length=32)
    common: dict[str, str] = Field(default_factory=dict)
    fact_rows: list[FactRowIn] = Field(default_factory=list, max_length=200)
    knowledge_blocks: list[KnowledgeBlockIn] = Field(default_factory=list, max_length=50)

    def unknown_common_keys(self) -> list[str]:
        """Canonical keys in ``common`` the registry does not know.

        Checked by the endpoint so the error surfaces as HTTP 400 with a
        stable domain code (a Pydantic validator would render 422).
        """
        return sorted(set(self.common) - _COMMON_KEYS)

    def to_entity(self, stored: Optional[EntityDocument]) -> EntityDocument:
        """Convert the form to an entity document (full-replace semantics).

        Created entities get revision 1; updates get ``stored.revision + 1``.
        All facts and knowledge blocks are replaced wholesale and reset to
        revision 1 — the request IS the full editor state. The repository's
        revision guard still protects against concurrent stale writes.
        """
        revision = 1 if stored is None else stored.revision + 1
        facts = [fact_from_common(key, value, revision=1) for key, value in self.common.items()]
        facts.extend(
            fact_from_row(row.label, row.value, row.unit, revision=1) for row in self.fact_rows
        )
        blocks = [
            KnowledgeBlock(
                id=new_id("block"),
                kind=block.kind,
                title=block.title,
                content=block.content,
                tags=block.tags,
                revision=1,
            )
            for block in self.knowledge_blocks
        ]
        return EntityDocument(
            id=self.id,
            entity_type=self.entity_type,
            revision=revision,
            name=self.name,
            aliases=list(self.aliases),
            tags=list(self.tags),
            facts=facts,
            knowledge_blocks=blocks,
            relations=list(stored.relations) if stored is not None else [],
        )


class SuggestionReq(BaseModel):
    """Extraction input; advisory only, the save path never calls this."""

    model_config = ConfigDict(extra="forbid")

    entity_type: EntityType = "product"
    text: str = Field(min_length=1, max_length=20_000)
    block_kind: BlockKind = "custom"
    block_title: str = Field(default="", max_length=256)


class RenderPreviewReq(BaseModel):
    """Exact query-relevant context renderer input (task 9.8)."""

    model_config = ConfigDict(extra="forbid")

    selectors: list[str] = Field(default_factory=list, max_length=20)
    max_block_chars: int = Field(default=400, ge=1, le=2_000)


class EntityListOut(BaseModel):
    entities: list[EntityDocument]


# ── Endpoints ───────────────────────────────────────────────────────


@_router.get("/entities")
async def entities_list(
    request: Request,
    entity_type: Optional[EntityType] = None,
    _: None = Depends(viewer_auth),
) -> EntityListOut:
    """List stored entities, optionally filtered by type (sorted by id)."""
    repo = _repo(request)
    entities = await repo.list_entities(entity_type=entity_type)
    return EntityListOut(entities=sorted(entities, key=lambda e: e.id))


@_router.get("/entities/{entity_id}")
async def entities_get(
    entity_id: str,
    request: Request,
    _: None = Depends(viewer_auth),
) -> EntityDocument:
    """The full normalized entity document (task 9.7 advanced view)."""
    entity = await _repo(request).get(entity_id)
    if entity is None:
        raise _domain_error(404, "entity_not_found", f"entity {entity_id} not found")
    return entity


@_router.put("/entities/{entity_id}")
async def entities_put(
    entity_id: str,
    req: SimpleEntityUpsertReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> EntityDocument:
    """Save the Data Studio form (the single authority for facts/blocks).

    The body id must match the path id; the stored document is returned at
    200. Concurrent stale writes (revision regression) surface as 409.
    """
    if entity_id != req.id:
        raise _domain_error(400, "entity_id_mismatch", "path id must match body id")
    unknown = req.unknown_common_keys()
    if unknown:
        raise _domain_error(
            400,
            "unknown_common_key",
            f"unknown common field key(s): {', '.join(unknown)}",
        )
    repo = _repo(request)
    stored = await repo.get(entity_id)
    entity = req.to_entity(stored)
    try:
        await repo.upsert(entity)
    except RevisionConflictError:
        raise _domain_error(
            409,
            "revision_conflict",
            f"entity {entity_id} was modified concurrently; reload and retry",
        ) from None
    return entity


@_router.delete("/entities/{entity_id}", status_code=status.HTTP_204_NO_CONTENT)
async def entities_delete(
    entity_id: str,
    request: Request,
    _: None = Depends(viewer_auth),
) -> None:
    """Delete an entity; 204 on success, 404 when missing."""
    deleted = await _repo(request).delete(entity_id)
    if not deleted:
        raise _domain_error(404, "entity_not_found", f"entity {entity_id} not found")


@_router.post("/entities/suggestions")
async def entities_suggestions(
    req: SuggestionReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> SuggestionResponse:
    """Optional AI fact extraction (task 9.5); NEVER required before saving.

    Advisory only: the operator moves accepted suggestions into the form and
    the save path (PUT) is the single authority. Always 200: no engine, a
    stub engine, malformed output, or an engine failure all return an empty
    suggestion list (with a ``note`` when the output did not parse).
    """
    container = container_from_request(request)
    llm = None
    if container.engine_manager is not None:
        llm = container.engine_manager.llm
    return await suggest_facts(llm, req.text, req.entity_type, req.block_kind, req.block_title)


@_router.post("/entities/{entity_id}/render-preview")
async def entities_render_preview(
    entity_id: str,
    req: RenderPreviewReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict:
    """Exact query-relevant evidence/context rendering preview (task 9.8)."""
    entity = await _repo(request).get(entity_id)
    if entity is None:
        raise _domain_error(404, "entity_not_found", f"entity {entity_id} not found")
    rendered = render_entity_context(
        entity,
        selectors=req.selectors or None,
        max_block_chars=req.max_block_chars,
    )
    return {
        "entity_id": entity_id,
        "selectors": list(req.selectors),
        "rendered": rendered,
    }
