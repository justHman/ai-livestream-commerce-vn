"""EvidenceCache — keyed by (entity, selector, revision, freshness bucket).

Spec: cache-aware, batch-native retrieval. Stable selectors are scoped to the
entity revision they were read at (a new revision invalidates them); volatile
selectors live in a short-TTL bucket so price/stock/promotion/availability
are revalidated near speech (spec 10.6).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Optional

from backend.application.evidence.models import (
    VOLATILE_SELECTORS,
    EvidenceConfig,
    Fact,
)

__all__ = ["CacheStatus", "EvidenceCache", "Stats"]


class CacheStatus:
    """Per-selector plan outcomes (spec: partial cache hit scenario)."""

    HIT = "hit"
    MISS = "miss"
    STALE = "stale"


@dataclass
class Stats:
    hits: int = 0
    misses: int = 0
    stale: int = 0


@dataclass
class _Entry:
    value: Fact
    revision: Optional[str]
    expires_at: float  # epoch seconds; 0 = no TTL (revision-scoped only)
    cached_at: float


class EvidenceCache:
    """In-memory TTL+revision cache. ``now`` is injectable for tests.

    Thread-safety: a single lock guards reads and writes. The planner runs
    on the event loop (async) and the store is fetched via ``to_thread``, so
    an entry can race between ``get`` and ``set``; that is benign (worst
    case one duplicate fetch) but the lock keeps the dict consistent.
    """

    def __init__(self, config: Optional[EvidenceConfig] = None, now=None) -> None:
        self._config = config or EvidenceConfig()
        self._now = now or time.time
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._lock = threading.Lock()
        self.stats = Stats()

    def get(
        self, entity_id: str, selector: str, revision: Optional[str]
    ) -> tuple[str, Optional[Fact]]:
        """Return (status, fact) for one entity/selector.

        status is hit when a non-expired entry exists and its revision is
        compatible, stale when the entry is usable but expired or revision
        changed, miss when absent.
        """
        volatile = selector in VOLATILE_SELECTORS
        key = (entity_id, selector)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.stats.misses += 1
                return CacheStatus.MISS, None
            expired = bool(entry.expires_at) and self._now() >= entry.expires_at
            revision_changed = bool(revision) and entry.revision != revision
            # Volatile entries are not revision-scoped: revalidation is TTL's
            # job, and the latest revision read is still authoritative.
            if expired or (not volatile and revision_changed):
                self.stats.stale += 1
                return CacheStatus.STALE, entry.value
            self.stats.hits += 1
            return CacheStatus.HIT, entry.value

    def set(self, entity_id: str, selector: str, fact: Fact) -> None:
        """Store one fact; TTL bucket depends on the fact's freshness type
        (the planner stamps volatile/stable from the request)."""
        volatile = fact.type == "volatile"
        ttl = self._config.volatile_ttl_seconds if volatile else 0
        with self._lock:
            if len(self._entries) >= self._config.max_cache_entries:
                # ponytail: evict the first entry, add per-key LRU if a real
                # store makes eviction frequency matter.
                self._entries.pop(next(iter(self._entries)))
            self._entries[(entity_id, selector)] = _Entry(
                value=fact,
                revision=fact.revision,
                expires_at=(self._now() + ttl) if ttl else 0.0,
                cached_at=self._now(),
            )

    def invalidate_entity(self, entity_id: str) -> None:
        """Drop every selector of one entity (explicit invalidation, spec 10.6)."""
        with self._lock:
            for key in [k for k in self._entries if k[0] == entity_id]:
                del self._entries[key]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self.stats = Stats()

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._entries)
