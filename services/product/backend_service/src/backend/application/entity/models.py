"""Typed domain models for the universal commerce entity document (tasks 8.1-8.3).

Pydantic v2 value objects (same style as ``script_authoring/models.py``):
the API layer serializes these directly, so the wire shape is the class shape.
The core envelope is intentionally small — vertical-specific attributes live in
facts/knowledge blocks, never as new fields on these models.

Every model carries a stable string id (``<type>:<uuid>``) so persisted rows,
API payloads, and downstream consumers can reference the same identity without
depending on database integer ids.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EntityDocument",
    "Fact",
    "KnowledgeBlock",
    "Relation",
    "new_id",
]

# Volatile facts stay structured; long irregular prose lives in knowledge
# blocks (Decision 10). These are the exact source keys the registry knows.
_FRESHNESS_LITERAL = Literal["stable", "volatile"]

# Fact values are kept deliberately simple: scalars only, no nested dicts.
# Composite values (e.g. variant stock) belong in a knowledge block or in a
# ``custom.*`` key with a structured ``labels`` payload until a canonical key
# exists (Decision 11: unknown attributes are stored, not rejected).
FactValue = Union[int, float, str, bool]


def new_id(prefix: str) -> str:
    """Return a stable unique id like ``<prefix>:<uuid4-hex>``."""
    return f"{prefix}:{uuid.uuid4().hex}"


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for new-document defaults."""
    return datetime.now(timezone.utc).isoformat()


class Fact(BaseModel):
    """One typed, revisioned fact value on an entity.

    ``key`` is a canonical registry key (e.g. ``commerce.price.current``) or a
    custom key (``custom.<slug>``) for facts the registry does not know.
    ``labels`` are user-facing aliases for THIS fact instance (e.g. how the
    operator named the field in the workbench), distinct from the global
    aliases the registry maps.
    """

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    type: Literal["int", "float", "str", "bool"]
    value: FactValue
    unit: Optional[str] = Field(default=None, max_length=64)
    labels: list[str] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)
    freshness: _FRESHNESS_LITERAL = "stable"
    updated_at: str = Field(default_factory=_now_iso)
    source: Optional[str] = Field(default=None, max_length=512)


class KnowledgeBlock(BaseModel):
    """Long revisioned prose (description, usage, story, campaign background).

    Kept out of facts on purpose: prose is retrieved per query (Decision 13),
    so it must not be serialized whole-document every turn.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    kind: Literal["description", "usage", "story", "campaign", "custom"] = "custom"
    title: str = Field(default="")
    content: str = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    revision: int = Field(default=1, ge=1)


class Relation(BaseModel):
    """Link to another entity, typed so consumers can filter by kind."""

    model_config = ConfigDict(extra="forbid")

    target_entity_id: str = Field(min_length=1)
    relation_type: Literal[
        "belongs_to_shop",
        "related_product",
        "campaign_targets",
        "custom",
    ] = "custom"
    metadata: dict[str, str] = Field(default_factory=dict)


class EntityDocument(BaseModel):
    """Aggregate root: the universal commerce entity envelope (Decision 10)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    entity_type: Literal["product", "shop", "campaign"]
    revision: int = Field(default=0, ge=0)
    name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    facts: list[Fact] = Field(default_factory=list)
    knowledge_blocks: list[KnowledgeBlock] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)

    def get_fact(self, key: str) -> Optional[Fact]:
        """Return the latest fact with the given canonical/custom key."""
        for fact in self.facts:
            if fact.key == key:
                return fact
        return None
