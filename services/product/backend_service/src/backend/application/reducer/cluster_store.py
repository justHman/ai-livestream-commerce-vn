"""Stable bounded per-session cluster store for the live-demand pipeline (OpenSpec 5.1-5.2).

Each session owns one ``ClusterStore``: incremental semantic assignment keeps a
stable ``LiveCluster`` per topic (stable ids -> lifecycle state survives fast-lane
updates), and every store is hard-bounded (max clusters / members per cluster) so
reducer memory cannot grow linearly with total comments. Expiry/reconciliation
are later tasks; per-member timestamps are kept so expiry is a plain filter.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import uuid4

from ..director.embeddings import average, cosine

__all__ = ["LiveCluster", "ClusterStore", "ClusterStoreConfig"]


@dataclass(frozen=True)
class ProductCandidate:
    """(product_id, score, evidence) tuple — evidence is opaque caller data."""

    product_id: str
    score: float
    evidence: str = ""


@dataclass
class LiveCluster:
    """One stable semantic topic cluster.

    ``cluster_id`` is created once and never regenerated; lifecycle state
    (last_selected_at / last_answered_at / skip_count) stays bound to it.
    Member embeddings live here (keyed by comment_id) so ``recompute`` can
    derive centroid/medoid/representatives on every add.
    """

    cluster_id: str
    created_at: float
    updated_at: float
    intent: str = "unknown"
    centroid: list[float] = field(default_factory=list)
    medoid_comment_id: Optional[str] = None
    representative_comment_ids: list[str] = field(default_factory=list)
    member_ids: list[str] = field(default_factory=list)
    viewer_ids: list[str] = field(default_factory=list)
    message_count: int = 0
    unique_viewer_count: int = 0
    intent_distribution: dict[str, int] = field(default_factory=dict)
    product_candidates: list[ProductCandidate] = field(default_factory=list)
    resolved_product_ids: list[str] = field(default_factory=list)
    product_resolution_confidence: float = 0.0
    cohesion: float = 0.0
    newest_t: float = 0.0
    last_selected_at: Optional[float] = None
    last_answered_at: Optional[float] = None
    novelty_fingerprint: str = ""
    skip_count: int = 0

    # internal: member embeddings + texts keyed by comment_id (needed to recompute)
    _member_embeddings: dict[str, list[float]] = field(default_factory=dict, repr=False)
    _member_texts: dict[str, str] = field(default_factory=dict, repr=False)
    _member_ts: dict[str, float] = field(default_factory=dict, repr=False)
    _member_viewers: dict[str, Optional[str]] = field(default_factory=dict, repr=False)

    def add_member(
        self,
        comment_id: str,
        text: str,
        vector: list[float],
        ts: float,
        viewer_key: Optional[str],
        intent: str,
        product_candidates: list[ProductCandidate],
    ) -> None:
        """Add one member and refresh derived state."""
        self.intent = intent
        self._member_embeddings[comment_id] = list(vector)
        self._member_texts[comment_id] = text
        self._member_ts[comment_id] = ts
        self._member_viewers[comment_id] = viewer_key
        if comment_id not in self.member_ids:
            self.member_ids.append(comment_id)
            self.message_count += 1
        if viewer_key is not None and viewer_key not in self.viewer_ids:
            self.viewer_ids.append(viewer_key)
        self.intent_distribution[intent] = self.intent_distribution.get(intent, 0) + 1
        for cand in product_candidates:
            self._merge_candidate(cand)
        self.unique_viewer_count = len(self.viewer_ids)
        self.newest_t = max(self.newest_t, ts)
        self.updated_at = ts
        self.recompute()

    def remove_member(self, comment_id: str) -> None:
        """Remove one member (eviction/expiry) and refresh derived state."""
        self._drop_member_with_ts(comment_id)
        self.newest_t = max((self._member_ts[cid] for cid in self.member_ids), default=0.0)
        self.recompute()

    def recompute(self, max_representatives: int = 5) -> None:
        """Recenter centroid, cohesion, medoid, and representative picks.

        Representatives = medoid + greedy diversity picks (lowest max-similarity
        to already-picked members), bounded by ``max_representatives``.
        """
        if not self.member_ids:
            self.centroid = []
            self.cohesion = 0.0
            self.medoid_comment_id = None
            self.representative_comment_ids = []
            return
        vectors = [self._member_embeddings[cid] for cid in self.member_ids]
        self.centroid = average(vectors)
        sims = [(cosine(v, self.centroid), cid) for cid, v in zip(self.member_ids, vectors)]
        medoid_sim, medoid_id = max(sims, key=lambda p: (p[0], p[1]))
        self.medoid_comment_id = medoid_id
        self.cohesion = medoid_sim
        self.representative_comment_ids = self._pick_representatives(max_representatives)

    def _pick_representatives(self, max_representatives: int) -> list[str]:
        """Medoid first, then members least similar to already-picked ones."""
        picked = [self.medoid_comment_id] if self.medoid_comment_id else []
        remaining = [cid for cid in self.member_ids if cid not in picked]
        while picked and len(picked) < max_representatives and remaining:
            picked_vecs = [self._member_embeddings[cid] for cid in picked]
            best = min(
                remaining,
                key=lambda cid: max(cosine(self._member_embeddings[cid], pv) for pv in picked_vecs),
            )
            picked.append(best)
            remaining.remove(best)
        return [cid for cid in picked if cid is not None]

    def _merge_candidate(self, cand: ProductCandidate) -> None:
        for i, existing in enumerate(self.product_candidates):
            if existing.product_id == cand.product_id:
                self.product_candidates[i] = ProductCandidate(
                    cand.product_id, max(existing.score, cand.score), existing.evidence
                )
                return
        self.product_candidates.append(cand)

    def _drop_member_with_ts(self, comment_id: str) -> None:
        self._member_embeddings.pop(comment_id, None)
        self._member_texts.pop(comment_id, None)
        self._member_ts.pop(comment_id, None)
        self._member_viewers.pop(comment_id, None)
        if comment_id in self.member_ids:
            self.member_ids.remove(comment_id)
            self.message_count = max(0, self.message_count - 1)
        # Viewer count is derived from remaining members so eviction bounds it too.
        self.viewer_ids = [
            v for v in {vk for vk in self._member_viewers.values() if vk is not None}
        ]
        self.unique_viewer_count = len(self.viewer_ids)
        if not self.member_ids:
            self.centroid = []
            self.cohesion = 0.0
            self.medoid_comment_id = None
            self.representative_comment_ids = []


@dataclass
class ClusterStoreConfig:
    """Typed knobs for one per-session ClusterStore.

    ``merge_threshold`` mirrors ``StreamConfig.cluster_merge_threshold`` and
    ``rolling_horizon_sec`` the FastReducer horizon; the store keeps its own
    copies so it is independently testable. The remaining knobs exist ONLY to
    bound memory (OpenSpec "reducer memory remains bounded").
    """

    merge_threshold: float = 0.375
    rolling_horizon_sec: float = 75.0
    max_active_clusters: int = 40
    max_members_per_cluster: int = 200
    max_representatives: int = 5

    def validate_runtime(self) -> None:
        """Fail-fast on non-positive knobs (called by the store at init)."""
        if self.merge_threshold <= 0:
            raise ValueError("merge_threshold must be > 0")
        if self.rolling_horizon_sec <= 0:
            raise ValueError("rolling_horizon_sec must be > 0")
        if self.max_active_clusters <= 0:
            raise ValueError("max_active_clusters must be > 0")
        if self.max_members_per_cluster <= 0:
            raise ValueError("max_members_per_cluster must be > 0")
        if self.max_representatives <= 0:
            raise ValueError("max_representatives must be > 0")


class ClusterStore:
    """Bounded per-session store of stable ``LiveCluster`` objects.

    ``assign`` merges a new comment into the best-matching compatible cluster
    (cosine to centroid >= threshold, intent-compatible) or creates a new
    cluster with a stable id. Ids never change across updates. Bounds drop the
    deterministic loser: smallest active cluster when at capacity, oldest
    member when a cluster is over its member cap.
    """

    def __init__(
        self,
        session_id: str,
        config: Optional[ClusterStoreConfig] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self.session_id = session_id
        self._config = config or ClusterStoreConfig()
        self._config.validate_runtime()
        self._now = now_fn or time.time
        self._clusters: dict[str, LiveCluster] = {}
        self._total_clusters_created = 0
        self._total_members_assigned = 0
        self._evicted_count = 0

    # ------------------------------------------------------------------
    # Assignment
    # ------------------------------------------------------------------

    def assign(
        self,
        comment_id: str,
        text: str,
        vector: list[float],
        ts: float,
        viewer_key: Optional[str] = None,
        intent: str = "unknown",
        product_candidates: Optional[list[ProductCandidate]] = None,
    ) -> str:
        """Assign one comment to a cluster; returns the stable cluster_id."""
        candidates = list(product_candidates or [])
        best, best_sim = None, self._config.merge_threshold
        for cluster in self._clusters.values():
            if cluster.intent != intent:
                continue
            sim = cosine(vector, cluster.centroid)
            if sim >= best_sim:
                best, best_sim = cluster, sim
        if best is None:
            best = self._create_cluster(ts)
        best.add_member(comment_id, text, vector, ts, viewer_key, intent, candidates)
        self._total_members_assigned += 1
        # Bounds run after the add so a fresh 0-member cluster never evicts itself.
        self._enforce_cluster_bound()
        self._enforce_member_bound(best)
        return best.cluster_id

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def active_clusters(self, now: float) -> list[LiveCluster]:
        """Clusters with any member inside the rolling horizon, demand-sorted.

        Deterministic order: message_count desc, then cluster_id asc.
        """
        cutoff = now - self._config.rolling_horizon_sec
        active = [c for c in self._clusters.values() if c.newest_t >= cutoff and c.member_ids]
        active.sort(key=lambda c: (-c.message_count, c.cluster_id))
        return active

    def get_cluster(self, cluster_id: str) -> Optional[LiveCluster]:
        return self._clusters.get(cluster_id)

    def stats(self) -> dict:
        """Content-safe counters (no raw viewer text)."""
        return {
            "session_id": self.session_id,
            "active_cluster_count": len(self._clusters),
            "total_clusters_created": self._total_clusters_created,
            "total_members_assigned": self._total_members_assigned,
            "member_ids_count": sum(len(c.member_ids) for c in self._clusters.values()),
            "evicted_count": self._evicted_count,
        }

    # ------------------------------------------------------------------
    # Bounds
    # ------------------------------------------------------------------

    def _create_cluster(self, ts: float) -> LiveCluster:
        cluster = LiveCluster(
            cluster_id=uuid4().hex,
            created_at=ts,
            updated_at=ts,
            intent="unknown",  # set by the first add_member
        )
        self._clusters[cluster.cluster_id] = cluster
        self._total_clusters_created += 1
        return cluster

    def _enforce_cluster_bound(self) -> None:
        if len(self._clusters) <= self._config.max_active_clusters:
            return
        victim = min(
            self._clusters.values(),
            key=lambda c: (c.message_count, c.created_at, c.cluster_id),
        )
        self._clusters.pop(victim.cluster_id)
        self._evicted_count += 1

    def _enforce_member_bound(self, cluster: LiveCluster) -> None:
        overflow = len(cluster.member_ids) - self._config.max_members_per_cluster
        for _ in range(max(0, overflow)):
            oldest = min(cluster.member_ids, key=lambda cid: cluster._member_ts[cid])
            cluster._drop_member_with_ts(oldest)
            self._evicted_count += 1
