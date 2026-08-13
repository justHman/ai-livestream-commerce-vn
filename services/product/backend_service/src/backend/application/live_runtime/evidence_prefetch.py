"""Stable-evidence prefetch + volatile just-in-time revalidation (tasks 14.7/14.8).

While a script sentence plays, the arbiter MAY prefetch STABLE evidence for
a high-confidence pending cluster (task 14.7) — stable selectors are
revision-scoped, so a cached value stays valid until the entity revision
changes and prefetching early is safe. VOLATILE selectors (price, stock,
promotion, availability) are NEVER prefetched: they live in the short-TTL
bucket and must be revalidated just-in-time before speech (task 14.8).

Both classes are duck-typed against the C10 ``EvidenceCache`` (sync, lock-
guarded) and the fast-path selector registry; no async needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from backend.application.agentic_director.fast_path import INTENT_SELECTOR_MAP
from backend.application.evidence.models import VOLATILE_SELECTORS

__all__ = [
    "EvidencePrefetcher",
    "PrefetchConfig",
    "VolatileRevalidator",
    "stable_selectors",
]


def stable_selectors() -> frozenset[str]:
    """Fast-path known selectors minus the volatile bucket (task 14.7).

    The fast path answers only selectors in ``INTENT_SELECTOR_MAP`` values;
    of those, the volatile ones must be revalidated near speech, so only the
    complement is safe to prefetch and hold while a sentence plays.
    """
    return frozenset(INTENT_SELECTOR_MAP.values()) - VOLATILE_SELECTORS


@dataclass(frozen=True, slots=True)
class PrefetchConfig:
    """Prefetch tuning knobs (task 14.7)."""

    min_score: float = 0.6
    max_prefetch_per_tick: int = 2


@runtime_checkable
class EvidenceLookup(Protocol):
    """Structural view of the C10 cache/prefetch surface.

    ``prefetch`` warms the stable bucket (cache ``set`` with a stable fact);
    ``get`` is the C10 ``EvidenceCache.get(entity_id, selector, revision)``.
    """

    def prefetch(self, entity_id: str, selector: str) -> None: ...
    def get(
        self, entity_id: str, selector: str, revision: Any | None = None
    ) -> tuple[str, Any | None]: ...


class EvidencePrefetcher:
    """Deterministic, bounded stable-evidence prefetch (task 14.7).

    ``prefetch_for`` scans the pending candidates in score-descending order,
    prefetches only STABLE selectors for candidates at or above
    ``min_score``, and stops after ``max_prefetch_per_tick`` prefetches.
    Already-cached stable selectors are no-ops (the cache reports HIT), so
    re-prefetching is idempotent. Volatile selectors are never touched here.
    """

    def __init__(
        self,
        lookup: EvidenceLookup,
        config: PrefetchConfig | None = None,
    ) -> None:
        self._lookup = lookup
        self._config = config or PrefetchConfig()
        self._stable = stable_selectors()

    @property
    def stable(self) -> frozenset[str]:
        return self._stable

    def prefetch_for(self, candidates: list[Any]) -> int:
        """Prefetch stable evidence for high-confidence candidates.

        Returns the number of prefetch calls made this tick (bounded by
        ``max_prefetch_per_tick``). Candidates are processed highest score
        first; below ``min_score`` nothing is prefetched.
        """
        count = 0
        ranked = sorted(
            candidates,
            key=lambda candidate: float(getattr(candidate, "ranking_score", 0.0)),
            reverse=True,
        )
        for candidate in ranked:
            if float(getattr(candidate, "ranking_score", 0.0)) < self._config.min_score:
                break
            for entity_id in getattr(candidate, "resolved_product_ids", ()):
                for selector in sorted(self._stable):
                    if count >= self._config.max_prefetch_per_tick:
                        return count
                    status, fact = self._lookup.get(entity_id, selector, revision=None)
                    if status == "hit" and fact is not None:
                        continue  # idempotent: already cached stable stays cached
                    self._lookup.prefetch(entity_id, selector)
                    count += 1
        return count


class VolatileRevalidator:
    """Just-in-time volatile evidence revalidation (task 14.8).

    ``revalidate(entity_id, selector)`` returns True only when the volatile
    fact is fresh after revalidation: a HIT within the short TTL is accepted
    without refetch; a MISS/STALE entry triggers ``prefetch`` (the
    cache/planner fetch path) and the outcome is re-checked. On failure the
    method returns False — the caller must refuse to speak the stale value
    (never invent facts). ``needs_revalidation`` gates selectors to the
    volatile bucket.
    """

    def __init__(self, lookup: EvidenceLookup) -> None:
        self._lookup = lookup

    def needs_revalidation(self, selector: str) -> bool:
        """True for volatile selectors only.

        ``VOLATILE_SELECTORS`` are the short-TTL bucket names (price, stock,
        promotion, availability); the fast-path selectors that embed one of
        these tokens (``commerce.price.current``) are STABLE in the C10
        registry (``Fact.type == stable``) and must not be revalidated here.
        """
        return selector in VOLATILE_SELECTORS

    def revalidate(self, entity_id: str, selector: str) -> bool:
        """Ensure one volatile fact is fresh; True when it is (after refetch if needed).

        The C10 cache TTL is wall-clock based (``EvidenceConfig.
        volatile_ttl_seconds`` since ``cached_at``); a MISS/STALE entry
        triggers ``prefetch`` (the cache/planner fetch path) and the outcome
        is re-checked. On failure the method returns False — the caller must
        refuse to speak the stale value (never invent facts).
        """
        status, fact = self._lookup.get(entity_id, selector, revision=None)
        if status == "hit" and fact is not None:
            return True
        if status in ("miss", "stale"):
            self._lookup.prefetch(entity_id, selector)
            status, fact = self._lookup.get(entity_id, selector, revision=None)
            return status == "hit" and fact is not None
        return False
