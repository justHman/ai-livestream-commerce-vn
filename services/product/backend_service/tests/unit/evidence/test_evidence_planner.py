"""Evidence planner + cache unit tests (cluster C10, task 10.7).

Covers: cache-hit, partial-hit, stale, revision-change, volatile-refresh,
plus batch fan-in, bounded concurrency, query resolution, entity-not-found,
and content-safe diagnostics.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from backend.application.evidence import (
    CacheStatus,
    EvidenceCache,
    EvidenceConfig,
    EvidencePlanner,
    EvidenceRequest,
    FreshnessPolicy,
)

from .fake_repository import build_fake

NOW = datetime(2026, 8, 14, 10, 0, 0, tzinfo=timezone.utc)


def _clock() -> datetime:
    return NOW


def _req(**kwargs) -> EvidenceRequest:
    kwargs.setdefault("entity_id", "P001")
    kwargs.setdefault("selectors", ["price"])
    return EvidenceRequest(**kwargs)


async def test_cache_hit_serves_from_cache_without_fetching():
    """A fully cached request is served without a source fetch."""
    repo = build_fake()
    planner = EvidencePlanner(repo)
    request = _req(selectors=["material"])

    await planner.plan([request])
    second = await planner.plan([request])

    assert second.results[0].cache_status == {"material": CacheStatus.HIT}
    assert second.results[0].facts["material"].value == "nỉ cotton"
    # First plan fetched; second plan must not touch the repository.
    assert len(repo.source_calls) == 1


async def test_partial_hit_batches_only_missing():
    """Spec: P001 cached, P020 missing -> fetch only P020 (batch fan-in 1)."""
    repo = build_fake()
    planner = EvidencePlanner(repo)
    cached = _req(entity_id="P001", selectors=["material"])
    await planner.plan([cached])

    bundle = await planner.plan(
        [
            _req(entity_id="P001", selectors=["material"]),
            _req(entity_id="P020", selectors=["material"]),
        ]
    )

    p001, p020 = bundle.results
    assert p001.cache_status == {"material": CacheStatus.HIT}
    assert p020.cache_status == {"material": CacheStatus.MISS}
    assert p020.facts["material"].value == "nỉ cotton"
    # One batched fetch covering exactly the miss.
    assert len(repo.source_calls) == 2  # first plan + partial-hit plan
    assert repo.source_calls[-1][0] == ["P020"]
    assert bundle.diagnostics.batch_fan_in == 1


async def test_stale_volatile_is_refreshed_near_speech():
    """Expired volatile price is revalidated (spec 10.6: near speech)."""
    repo = build_fake()
    cache = EvidenceCache(EvidenceConfig(volatile_ttl_seconds=30), now=time.time)
    planner = EvidencePlanner(repo, cache=cache)
    request = _req(selectors=["price"])
    await planner.plan([request])

    # Fast-forward past the TTL; the stale value is still usable mid-speech
    # but revalidated at the next plan.
    cache._now = lambda: time.time() + 31
    bundle = await planner.plan([request])

    assert bundle.results[0].cache_status == {"price": CacheStatus.STALE}
    assert bundle.results[0].facts["price"].value == 329000
    assert bundle.diagnostics.stale_refreshes == 1
    # Fresh value now cached again.
    cache._now = time.time
    bundle = await planner.plan([request])
    assert bundle.results[0].cache_status == {"price": CacheStatus.HIT}


async def test_stable_stays_hit_within_revision():
    """A stable fact is a hit until the entity revision changes."""
    repo = build_fake()
    planner = EvidencePlanner(repo)
    request = _req(selectors=["origin"])
    await planner.plan([request])

    bundle = await planner.plan([request])

    assert bundle.results[0].cache_status == {"origin": CacheStatus.HIT}


async def test_stable_revision_change_is_revalidated():
    """A new entity revision invalidates stable facts (revision-scoped)."""
    repo = build_fake()
    planner = EvidencePlanner(repo)
    request = _req(selectors=["origin"])
    await planner.plan([request])

    repo._entities["P001"]["revision"] = "rev-2"
    bundle = await planner.plan([request])

    assert bundle.results[0].cache_status == {"origin": CacheStatus.STALE}
    assert bundle.results[0].revision == "rev-2"
    assert bundle.diagnostics.stale_refreshes == 1


async def test_volatile_ignores_revision_change():
    """Volatile selectors are TTL-scoped, not revision-scoped."""
    repo = build_fake()
    planner = EvidencePlanner(repo)
    request = _req(selectors=["stock"])
    await planner.plan([request])

    repo._entities["P001"]["revision"] = "rev-2"
    bundle = await planner.plan([request])

    assert bundle.results[0].cache_status == {"stock": CacheStatus.HIT}


async def test_mixed_selectors_partial_refresh():
    """Stable hit + volatile miss in one request -> fetch only the miss."""
    repo = build_fake()
    cache = EvidenceCache(EvidenceConfig(volatile_ttl_seconds=30), now=time.time)
    planner = EvidencePlanner(repo, cache=cache)
    request = _req(selectors=["origin", "stock"])
    await planner.plan([request])

    cache._now = lambda: time.time() + 31
    bundle = await planner.plan([request])

    assert bundle.results[0].cache_status == {"origin": CacheStatus.HIT, "stock": CacheStatus.STALE}
    assert repo.source_calls[-1][1] == ["stock"]


async def test_query_requests_resolve_via_search():
    """Query-only requests resolve through the repository's search."""
    repo = build_fake()
    planner = EvidencePlanner(repo)

    bundle = await planner.plan([_req(entity_id=None, query="hoodie", selectors=["price"])])

    result = bundle.results[0]
    assert result.entity_id == "P020"
    assert result.facts["price"].value == 350000
    assert repo.search_calls == [(["hoodie"], None)]


async def test_entity_not_found_has_typed_error():
    """Missing entity -> typed error, never invented facts."""
    repo = build_fake()
    planner = EvidencePlanner(repo)

    bundle = await planner.plan([_req(entity_id="P999", selectors=["price"])])

    result = bundle.results[0]
    assert result.error == "entity_not_found"
    assert result.facts == {}
    assert result.rendered_text is None


async def test_misses_are_batched_in_one_fetch():
    """Two missing entities in one plan -> one fetch round, both batched."""
    repo = build_fake()
    planner = EvidencePlanner(repo)

    await planner.plan(
        [
            _req(entity_id="P001", selectors=["material"]),
            _req(entity_id="P020", selectors=["material"]),
        ]
    )

    assert sorted(call[0] for call in repo.source_calls) == [["P001"], ["P020"]]


async def test_independent_misses_execute_concurrently():
    """Slow repository reads of independent entities overlap (fan-in 2)."""
    repo = build_fake()
    repo.set_delay(0.1)
    planner = EvidencePlanner(repo)

    await planner.plan(
        [
            _req(entity_id="P001", selectors=["material"]),
            _req(entity_id="P020", selectors=["material"]),
        ]
    )

    assert repo.max_active_fetches >= 2  # two entities fetched in parallel


async def test_facts_and_rendered_text_populated():
    """Planned evidence carries typed facts and rendered text."""
    repo = build_fake()
    planner = EvidencePlanner(repo)

    bundle = await planner.plan([_req(selectors=["price"])])

    fact = bundle.results[0].facts["price"]
    assert fact.key == "price"
    assert fact.value == 329000
    assert fact.revision == "rev-1"
    assert fact.freshness is not None
    assert "329000" in (bundle.results[0].rendered_text or "")


async def test_diagnostics_are_content_safe():
    """Diagnostics expose selector names and counts, never fact values."""
    repo = build_fake()
    planner = EvidencePlanner(repo)

    bundle = await planner.plan([_req(selectors=["price", "origin"])])

    d = bundle.diagnostics
    assert d.requested_selectors == ["origin", "price"]
    assert d.cache_hits == 0
    assert d.cache_misses == 2
    assert d.batch_fan_in == 2
    assert "329000" not in d.model_dump_json()
    assert "nỉ cotton" not in d.model_dump_json()


async def test_invalidate_entity_drops_its_entries():
    """Explicit invalidation forces the next plan to refetch."""
    repo = build_fake()
    planner = EvidencePlanner(repo)
    request = _req(selectors=["material"])
    await planner.plan([request])

    planner.cache.invalidate_entity("P001")
    bundle = await planner.plan([request])

    assert bundle.results[0].cache_status == {"material": CacheStatus.MISS}


async def test_volatile_request_policy_is_honored():
    """A volatile request for a stable selector is still TTL-scoped."""
    repo = build_fake()
    cache = EvidenceCache(EvidenceConfig(volatile_ttl_seconds=30), now=time.time)
    planner = EvidencePlanner(repo, cache=cache)
    request = _req(selectors=["origin"], freshness=FreshnessPolicy.VOLATILE)
    await planner.plan([request])

    cache._now = lambda: time.time() + 31
    bundle = await planner.plan([request])

    assert bundle.results[0].cache_status == {"origin": CacheStatus.STALE}
