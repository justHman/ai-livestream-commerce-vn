"""EvidencePlanner — cache-first, batch-native evidence planning (cluster C10).

Spec: batch only missing authoritative evidence; volatile evidence is
revalidated near speech. Flow per plan:

1. resolve free-text queries -> entity ids (batched, concurrent)
2. read entity revisions (batched, concurrent)
3. per request, split selectors into cache hit / miss / stale
4. fetch ONLY misses and stale entries, concurrently (bounded by
   ``EvidenceConfig.max_concurrency`` via ``asyncio.to_thread``)
5. build the bundle: per-request facts, rendered text, cache status,
   content-safe diagnostics (selector names and counts, never values)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Optional

from backend.application.evidence.cache import CacheStatus, EvidenceCache
from backend.application.evidence.models import (
    EntityDocumentView,
    EntityRef,
    EvidenceBundle,
    EvidenceConfig,
    EvidenceDiagnostics,
    EvidenceRequest,
    EvidenceResult,
    Fact,
    FreshnessPolicy,
    VOLATILE_SELECTORS,
)
from backend.application.evidence.repository import EntityRepository

__all__ = ["EvidencePlanner"]


class EvidencePlanner:
    """Application-owned planner; the model never invokes arbitrary functions
    (Decision 13) — it only requests evidence through typed requests."""

    def __init__(
        self,
        repository: EntityRepository,
        cache: Optional[EvidenceCache] = None,
        config: Optional[EvidenceConfig] = None,
        clock=None,
    ) -> None:
        self._repo = repository
        self._config = config or EvidenceConfig()
        self._cache = cache or EvidenceCache(self._config)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    @property
    def cache(self) -> EvidenceCache:
        return self._cache

    async def search_entities(
        self, queries: list[str], entity_type: Optional[str] = None
    ) -> list[list[EntityRef]]:
        """Resolve each query to entity references (one list per query).

        ``EntityRef.query`` echoes the query so batch callers can correlate.
        """
        results = await self._repo.search_entities(queries, entity_type)
        return [
            [
                EntityRef(entity_id=r.entity_id, name=r.name, entity_type=entity_type, query=q)
                for r in rs
            ]
            for q, rs in zip(queries, results)
        ]

    async def get_entities(
        self, ids: list[str], selectors: Optional[list[str]] = None
    ) -> list[EntityDocumentView]:
        """Return document views for known ids (normalized, Decision 12)."""
        return await self._repo.get_entities(ids, selectors)

    async def plan(self, requests: list[EvidenceRequest]) -> EvidenceBundle:
        """Plan evidence for requests: cache-first, batch only misses."""
        # Diagnostics are per-plan; snapshot the cache counters up front.
        baseline = (self._cache.stats.hits, self._cache.stats.misses, self._cache.stats.stale)

        # 1. Resolve query-only requests to entity ids (batched, concurrent).
        query_map = await self._resolve_queries(requests)

        # 2. Current entity revisions drive stable-scope staleness. Entities
        #    the revision read does not know are treated as not found.
        revisions = await self._fetch_revisions(requests, query_map)
        missing_entities = {eid for eid in query_map.values() if eid not in revisions}

        # 3. Split each request into hits / to-fetch (miss + stale), recording
        #    the selector's status so stale-refreshed entries are labeled.
        hits: dict[tuple[int, str], Fact] = {}
        to_fetch: list[tuple[int, str]] = []
        statuses: dict[tuple[int, str], str] = {}
        for idx, req in enumerate(requests):
            entity_id = query_map.get(idx)
            if entity_id is None or entity_id in missing_entities:
                continue
            revision = revisions.get(entity_id)
            for selector in req.selectors:
                status, fact = self._cache.get(entity_id, selector, revision)
                statuses[(idx, selector)] = status
                if fact is not None and status == CacheStatus.HIT:
                    hits[(idx, selector)] = fact
                else:
                    to_fetch.append((idx, selector))

        # 4. Batch-fetch only misses/stale, concurrently (bounded).
        fetched = await self._fetch_misses(requests, query_map, misses=to_fetch)

        # 5. Assemble the bundle.
        bundle = self._build_bundle(requests, query_map, hits, fetched, statuses, missing_entities)
        self._collect_diagnostics(bundle, baseline)
        return bundle

    async def _resolve_queries(self, requests: list[EvidenceRequest]) -> dict[int, str]:
        """Map request index -> entity id; unresolved queries stay absent."""
        query_map: dict[int, str] = {}
        by_query: dict[str, list[int]] = {}
        for idx, req in enumerate(requests):
            if req.entity_id:
                query_map[idx] = req.entity_id
            elif req.query:
                by_query.setdefault(req.query, []).append(idx)
        if not by_query:
            return query_map

        def _run() -> dict[str, str]:
            queries = list(by_query)
            results = asyncio.run(self._repo.search_entities(queries))
            return {
                q: (results[i][0].entity_id if results[i] else "") for i, q in enumerate(queries)
            }

        resolved = await asyncio.to_thread(_run)
        for query, idxs in by_query.items():
            if resolved.get(query):
                for idx in idxs:
                    query_map[idx] = resolved[query]
        return query_map

    async def _fetch_revisions(
        self, requests: list[EvidenceRequest], query_map: dict[int, str]
    ) -> dict[str, Optional[str]]:
        ids = sorted({eid for eid in query_map.values()})
        if not ids:
            return {}

        def _run() -> list[EntityDocumentView]:
            return asyncio.run(self._repo.get_entities(ids, None))

        docs = await asyncio.to_thread(_run)
        return {d.entity_id: d.revision for d in docs}

    async def _fetch_misses(
        self,
        requests: list[EvidenceRequest],
        query_map: dict[int, str],
        misses: list[tuple[int, str]],
    ) -> dict[tuple[int, str], Fact]:
        """Fetch the missing selectors of each entity as one concurrent task
        per entity, bounded by ``max_concurrency`` (spec 10.5)."""
        if not misses:
            return {}
        by_entity: dict[str, list[tuple[int, str]]] = {}
        for i, selector in misses:
            by_entity.setdefault(query_map[i], []).append((i, selector))
        semaphore = asyncio.Semaphore(self._config.max_concurrency)

        async def fetch_entity(
            eid: str, keys: list[tuple[int, str]]
        ) -> dict[tuple[int, str], Fact]:
            async with semaphore:
                return await asyncio.to_thread(
                    self._fetch_entity_sources, eid, [k for _, k in keys], keys
                )

        tasks = [fetch_entity(eid, keys) for eid, keys in sorted(by_entity.items())]
        results = await asyncio.gather(*tasks)
        return {key: fact for result in results for key, fact in result.items()}

    def _fetch_entity_sources(
        self, eid: str, selectors: list[str], keys: list[tuple[int, str]]
    ) -> dict[tuple[int, str], Fact]:
        """Synchronous store read: one batched source fetch per entity."""
        sources = asyncio.run(self._repo.fetch_sources([eid], selectors, self._clock()))
        by_eid: dict[str, list] = {}
        for src in sources:
            by_eid.setdefault(src.entity_id, []).append(src)
        found: dict[tuple[int, str], Fact] = {}
        for key, selector in zip(keys, selectors):
            src = next(
                (s for s in by_eid.get(eid, []) if selector in s.selectors),
                None,
            )
            if src is None:
                continue
            found[key] = self._to_fact(selector, src)
        return found

    @staticmethod
    def _to_fact(selector: str, src) -> Fact:
        """Build a cacheable Fact from one source value."""
        value = src.fields.get(selector)
        rendered = None
        if value is not None:
            raw = value.rendered_text if hasattr(value, "rendered_text") else None
            rendered = raw if raw is not None else str(value)
        return Fact(
            key=selector,
            value=value,
            freshness=src.observed_at,
            revision=src.revision,
            source="repository",
            rendered_text=rendered,
        )

    def _build_bundle(
        self,
        requests: list[EvidenceRequest],
        query_map: dict[int, str],
        hits: dict[tuple[int, str], Fact],
        fetched: dict[tuple[int, str], Fact],
        statuses: dict[tuple[int, str], str],
        missing_entities: set[str],
    ) -> EvidenceBundle:
        results: list[EvidenceResult] = []
        for idx, req in enumerate(requests):
            eid = query_map.get(idx)
            if eid is None or eid in missing_entities:
                results.append(
                    EvidenceResult(
                        request=req,
                        selectors=req.selectors,
                        cache_status={s: CacheStatus.MISS for s in req.selectors},
                        error="entity_not_found",
                    )
                )
                continue
            facts: dict[str, Fact] = {}
            cache_status: dict[str, str] = {}
            revision: Optional[str] = None
            for selector in req.selectors:
                fact = hits.get((idx, selector)) or fetched.get((idx, selector))
                if fact is None:
                    cache_status[selector] = CacheStatus.MISS
                    continue
                facts[selector] = fact
                cache_status[selector] = statuses.get((idx, selector), CacheStatus.MISS)
                revision = fact.revision or revision
                if (idx, selector) not in hits:
                    # Refreshed facts go back into the cache for the next plan;
                    # volatile selectors keep TTL semantics on the write path.
                    self._cache.set(
                        eid,
                        selector,
                        fact.model_copy(update={"type": self._selector_type(req, selector)}),
                    )
            results.append(
                EvidenceResult(
                    request=req,
                    entity_id=eid,
                    selectors=req.selectors,
                    facts=facts,
                    rendered_text=self._render(facts, req.selectors),
                    freshness=(
                        max(
                            (f.freshness for f in facts.values() if f.freshness),
                            default=None,
                        )
                    ),
                    revision=revision,
                    cache_status=cache_status,
                )
            )
        return EvidenceBundle(
            requests=requests,
            results=results,
            diagnostics=EvidenceDiagnostics(),
        )

    @staticmethod
    def _selector_type(req: EvidenceRequest, selector: str) -> str:
        """Effective freshness bucket for one selector (volatile wins, spec 10.6)."""
        if selector in VOLATILE_SELECTORS:
            return FreshnessPolicy.VOLATILE.value
        return req.freshness.value

    @staticmethod
    def _render(facts: dict[str, Fact], selectors: list[str]) -> Optional[str]:
        """Concatenate per-selector rendered text; None when nothing rendered."""
        parts = [facts[s].rendered_text for s in selectors if s in facts and facts[s].rendered_text]
        return " ".join(parts) if parts else None

    def _collect_diagnostics(self, bundle: EvidenceBundle, baseline: tuple[int, int, int]) -> None:
        """Content-safe per-plan counters: selector names and counts only,
        never fact values (spec: Agent execution is observable)."""
        d = bundle.diagnostics
        d.requested_selectors = sorted({s for r in bundle.requests for s in r.selectors})
        d.cache_hits = self._cache.stats.hits - baseline[0]
        d.cache_misses = self._cache.stats.misses - baseline[1]
        d.stale_refreshes = self._cache.stats.stale - baseline[2]
        d.batch_fan_in = d.cache_misses + d.stale_refreshes
