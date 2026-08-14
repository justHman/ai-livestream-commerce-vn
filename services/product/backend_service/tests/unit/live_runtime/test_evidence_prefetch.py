"""Tasks 14.7/14.8: stable-evidence prefetch + volatile just-in-time revalidation.

Proves against the C10 ``EvidenceCache`` (real TTL+revision semantics):

- 14.7: ONLY stable selectors are prefetched (volatile never); the prefetch
  is bounded per tick, gated by the high-confidence score, deterministic
  (highest score first), and idempotent on an already-cached stable entry.
- 14.8: volatile selectors need revalidation, stable never; a fresh cached
  volatile entry yields True with NO refetch (call-recording fake); a
  stale/missing entry refetches and then yields True; a refetch failure
  yields False; the revalidator never invents facts (a miss stays False).
"""

from __future__ import annotations

from backend.application.agentic_director.fast_path import INTENT_SELECTOR_MAP
from backend.application.evidence.cache import CacheStatus, EvidenceCache
from backend.application.evidence.models import VOLATILE_SELECTORS, EvidenceConfig, Fact
from backend.application.live_runtime.evidence_prefetch import (
    EvidencePrefetcher,
    PrefetchConfig,
    VolatileRevalidator,
    stable_selectors,
)

ALL_INTENT_SELECTORS = frozenset(INTENT_SELECTOR_MAP.values())


class _Envelope:
    def __init__(self, cluster_id: str, score: float, entity_id: str = "P001") -> None:
        self.cluster_id = cluster_id
        self.ranking_score = score
        self.resolved_product_ids = (entity_id,)


class _RecordingLookup:
    """EvidenceLookup fake recording every prefetch/get; cache-backed gets."""

    def __init__(self, cache: EvidenceCache) -> None:
        self.cache = cache
        self.prefetches: list[tuple[str, str]] = []

    def prefetch(self, entity_id: str, selector: str) -> None:
        self.prefetches.append((entity_id, selector))
        self.cache.set(entity_id, selector, Fact(key=selector, type="stable", value="ok"))

    def get(self, entity_id: str, selector: str, revision=None) -> tuple[str, object | None]:
        return self.cache.get(entity_id, selector, revision)


def _stable_fact(selector: str) -> Fact:
    return Fact(key=selector, type="stable", value="ok")


# --- 14.7: stable-only prefetch ---------------------------------------------


def test_stable_selectors_are_fastpath_minus_volatile() -> None:
    stable = stable_selectors()

    assert stable == ALL_INTENT_SELECTORS - VOLATILE_SELECTORS
    assert not stable & VOLATILE_SELECTORS


def test_prefetch_touches_only_stable_selectors() -> None:
    cache = EvidenceCache()
    lookup = _RecordingLookup(cache)
    prefetcher = EvidencePrefetcher(lookup, PrefetchConfig(min_score=0.0, max_prefetch_per_tick=10))

    prefetcher.prefetch_for([_Envelope("cl-a", 0.9)])

    prefetched = {selector for _, selector in lookup.prefetches}
    assert prefetched <= stable_selectors()
    assert not prefetched & VOLATILE_SELECTORS


def test_prefetch_bounded_per_tick() -> None:
    cache = EvidenceCache()
    lookup = _RecordingLookup(cache)
    prefetcher = EvidencePrefetcher(lookup, PrefetchConfig(min_score=0.0, max_prefetch_per_tick=2))

    count = prefetcher.prefetch_for([_Envelope("cl-a", 0.9)])

    assert count == 2
    assert len(lookup.prefetches) == 2


def test_prefetch_gated_by_high_confidence() -> None:
    cache = EvidenceCache()
    lookup = _RecordingLookup(cache)
    prefetcher = EvidencePrefetcher(lookup, PrefetchConfig(min_score=0.6, max_prefetch_per_tick=10))

    count = prefetcher.prefetch_for([_Envelope("cl-low", 0.4)])

    assert count == 0
    assert lookup.prefetches == []


def test_prefetch_deterministic_highest_score_first() -> None:
    cache = EvidenceCache()
    lookup = _RecordingLookup(cache)
    prefetcher = EvidencePrefetcher(lookup, PrefetchConfig(min_score=0.0, max_prefetch_per_tick=2))

    prefetcher.prefetch_for(
        [_Envelope("cl-low", 0.5, entity_id="P002"), _Envelope("cl-high", 0.9, entity_id="P001")]
    )

    assert lookup.prefetches[0][0] == "P001"  # highest score first


def test_prefetch_idempotent_on_cached_stable() -> None:
    cache = EvidenceCache()
    cache.set("P001", "commerce.warranty", _stable_fact("commerce.warranty"))
    lookup = _RecordingLookup(cache)
    prefetcher = EvidencePrefetcher(lookup, PrefetchConfig(min_score=0.0, max_prefetch_per_tick=10))

    prefetcher.prefetch_for([_Envelope("cl-a", 0.9)])
    before = list(lookup.prefetches)
    prefetcher.prefetch_for([_Envelope("cl-a", 0.9)])

    # A second prefetch of an already-cached stable selector stays a no-op
    # (the cache reports HIT); nothing new is recorded.
    assert lookup.prefetches == before


# --- 14.8: volatile just-in-time revalidation --------------------------------


def test_volatile_needs_revalidation_stable_never() -> None:
    revalidator = VolatileRevalidator(_RecordingLookup(EvidenceCache()))

    for selector in VOLATILE_SELECTORS:
        assert revalidator.needs_revalidation(selector) is True
    for selector in stable_selectors():
        assert revalidator.needs_revalidation(selector) is False


def test_fresh_volatile_entry_true_without_refetch() -> None:
    cache = EvidenceCache(config=EvidenceConfig(volatile_ttl_seconds=60))
    cache.set("P001", "price", Fact(key="price", type="volatile", value="1.2 triệu"))
    lookup = _RecordingLookup(cache)
    revalidator = VolatileRevalidator(lookup)

    assert revalidator.revalidate("P001", "price") is True
    assert lookup.prefetches == []


def test_stale_volatile_refetches_then_true() -> None:
    now = {"value": 100.0}
    cache = EvidenceCache(config=EvidenceConfig(volatile_ttl_seconds=1), now=lambda: now["value"])
    cache.set("P001", "price", Fact(key="price", type="volatile", value="cũ"))
    now["value"] = 200.0  # advance past the 1s TTL -> stale
    lookup = _RecordingLookup(cache)
    revalidator = VolatileRevalidator(lookup)

    assert revalidator.revalidate("P001", "price") is True
    assert ("P001", "price") in lookup.prefetches


def test_refetch_failure_yields_false_never_invents() -> None:
    cache = EvidenceCache()

    class NoWarmLookup(_RecordingLookup):
        def prefetch(self, entity_id: str, selector: str) -> None:
            self.prefetches.append((entity_id, selector))
            # the fetch failed: nothing is warmed

    lookup = NoWarmLookup(cache)
    revalidator = VolatileRevalidator(lookup)

    # No entry and a failed refetch -> False; no value was invented.
    assert revalidator.revalidate("P001", "price") is False
    assert ("P001", "price") in lookup.prefetches
    assert lookup.cache.get("P001", "price", None)[0] == CacheStatus.MISS


def test_refetch_failure_after_prefetch_returns_false() -> None:
    """A prefetch that cannot warm the cache must yield False (no invented facts)."""
    cache = EvidenceCache()

    class FailLookup(_RecordingLookup):
        def prefetch(self, entity_id: str, selector: str) -> None:
            self.prefetches.append((entity_id, selector))
            # deliberately do NOT warm the cache: the fetch failed

    failing = FailLookup(cache)
    revalidator = VolatileRevalidator(failing)

    assert revalidator.revalidate("P001", "price") is False
    assert ("P001", "price") in failing.prefetches
