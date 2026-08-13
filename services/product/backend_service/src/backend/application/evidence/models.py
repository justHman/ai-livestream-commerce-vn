"""Typed models for the evidence planner and cache (cluster C10).

Pure pydantic value objects: no network or storage access. The planner reads
and writes these through ``EntityRepository`` (see ``repository.py``), the
protocol that cluster C8's concrete entity store implements against this
package's public surface.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field, model_validator

__all__ = [
    "EntityDocumentView",
    "EntityRef",
    "EvidenceBundle",
    "EvidenceConfig",
    "EvidenceDiagnostics",
    "EvidenceRequest",
    "EvidenceResult",
    "Fact",
    "FreshnessPolicy",
    "VOLATILE_SELECTORS",
]


class FreshnessPolicy(StrEnum):
    """Freshness semantics for a selector's cached evidence.

    Stable facts (size, material, ...) are revision-scoped: a cached value
    stays valid until the entity revision it was read at changes. Volatile
    facts (price, stock, ...) use a short TTL so the value is revalidated on
    the plan nearest to speech (Decision 13).
    """

    STABLE = "stable"
    VOLATILE = "volatile"


# Spec 10.6: these selectors SHALL use short freshness even when a caller
# asks for stable — a "stable" price would be spoken stale mid-stream.
VOLATILE_SELECTORS: frozenset[str] = frozenset({"price", "stock", "promotion", "availability"})


class EvidenceConfig(BaseModel):
    """Tunables for cache TTL and planner concurrency."""

    volatile_ttl_seconds: int = 30
    max_concurrency: int = 4
    max_cache_entries: int = 512


class EntityRef(BaseModel):
    """One entity search hit; ``query`` echoes the query it matched so batch
    search results stay correlatable to their inputs."""

    entity_id: str
    name: Optional[str] = None
    entity_type: Optional[str] = None
    query: Optional[str] = None


class Fact(BaseModel):
    """One authoritative fact: key/type/value plus freshness provenance.

    ``revision`` is the entity revision the fact was read at (the cache's
    stable-scoped staleness signal); ``freshness`` is the fact's own
    observed-at time when the source provides one.
    """

    key: str
    type: str = "text"
    value: Any = None
    freshness: Optional[datetime] = None
    revision: Optional[str] = None
    source: str = "unknown"
    rendered_text: Optional[str] = None


class EntityDocumentView(BaseModel):
    """Normalized document view (Decision 12): common fields, revision, and
    per-selector facts.

    C8's repository returns these from ``get_documents``; the planner turns
    ``fields`` into cacheable facts and uses ``rendered_text`` verbatim when
    the store provides one.
    """

    entity_id: str
    name: Optional[str] = None
    revision: Optional[str] = None
    fields: dict[str, Fact] = Field(default_factory=dict)
    rendered_text: Optional[str] = None


class EvidenceRequest(BaseModel):
    """One evidence ask: a target (id or free-text query) plus selectors.

    ``freshness`` requests stable (revision-scoped) or volatile (short TTL)
    semantics; volatile selectors always win (see ``VOLATILE_SELECTORS``).
    When both ``entity_id`` and ``query`` are set, the id wins.
    """

    entity_id: Optional[str] = None
    query: Optional[str] = None
    selectors: list[str] = Field(min_length=1)
    freshness: FreshnessPolicy = FreshnessPolicy.STABLE
    revision: Optional[str] = None

    @model_validator(mode="after")
    def _require_target(self) -> "EvidenceRequest":
        if not self.entity_id and not self.query:
            raise ValueError("EvidenceRequest needs entity_id or query")
        return self


class EvidenceResult(BaseModel):
    """Per-request outcome in an ``EvidenceBundle``; aligns 1:1 with requests.

    ``cache_status`` maps each requested selector to hit|miss|stale. ``error``
    is a typed code (``entity_not_found``) when the request could not be
    satisfied — the agent MUST NOT invent facts then (spec: authoritative
    evidence wins over model claims).
    """

    request: EvidenceRequest
    entity_id: Optional[str] = None
    selectors: list[str]
    facts: dict[str, Fact] = Field(default_factory=dict)
    rendered_text: Optional[str] = None
    freshness: Optional[datetime] = None
    revision: Optional[str] = None
    cache_status: dict[str, str] = Field(default_factory=dict)
    error: Optional[str] = None


class EvidenceDiagnostics(BaseModel):
    """Content-safe observability for one plan (spec: Agent execution is
    observable).

    Selector names and counts only — never fact values, so traces do not leak
    private business content.
    """

    requested_selectors: list[str] = Field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    stale_refreshes: int = 0
    batch_fan_in: int = 0


class EvidenceBundle(BaseModel):
    """Batch-native answer: ``results`` mirrors ``requests`` element-wise."""

    requests: list[EvidenceRequest]
    results: list[EvidenceResult]
    diagnostics: EvidenceDiagnostics
