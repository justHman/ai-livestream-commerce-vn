"""Stable bounded per-session cluster store for the live-demand pipeline (OpenSpec 5.1-5.7).

Each session owns one ``ClusterStore``: incremental semantic assignment keeps a
stable ``LiveCluster`` per topic (stable ids -> lifecycle state survives fast-lane
updates), and every store is hard-bounded (max clusters / members per cluster) so
reducer memory cannot grow linearly with total comments. Per-member timestamps
make expiry a plain filter (5.4). Reconciliation (5.5-5.7) is a count/age-triggered
bounded repair of the active horizon — merge compatible clusters, split
low-cohesion ones, recompute centroids — and NEVER runs on the fast-lane hot path.
A failed pass (5.7) is transactional: the pre-pass state is restored verbatim and
a typed content-safe ``ReconciliationFailure`` is recorded, so the fast lane
keeps operating on the last valid cluster state.
"""

from __future__ import annotations

import copy
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional
from uuid import uuid4

from ..director.embeddings import average, cosine

__all__ = [
    "LiveCluster",
    "ClusterStore",
    "ClusterStoreConfig",
    "ReconciliationError",
    "ReconciliationFailure",
    "ReconciliationResult",
]


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
        product_resolution_threshold: float = 1.5,
        product_resolution_margin: float = 1.0,
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
        self.resolve_products(product_resolution_threshold, product_resolution_margin)
        self.unique_viewer_count = len(self.viewer_ids)
        self.newest_t = max(self.newest_t, ts)
        self.updated_at = ts
        self.recompute()

    def remove_member(self, comment_id: str) -> None:
        """Remove one member (eviction/expiry) and refresh derived state."""
        self._drop_member_with_ts(comment_id)
        self.newest_t = max((self._member_ts[cid] for cid in self.member_ids), default=0.0)
        self.recompute()

    def resolve_products(
        self,
        product_resolution_threshold: float = 1.5,
        product_resolution_margin: float = 1.0,
    ) -> list[str]:
        """Derive resolved_product_ids from merged candidates (OpenSpec 6.3).

        A candidate resolves iff ``score >= max(top - margin, threshold)``
        where ``top`` is the highest merged score; otherwise ambiguity is
        preserved (resolved=[] and confidence=0.0, never a silent top-1).
        Confidence is monotonic in the top score: ``top / (top + 1)`` — 0.5 at
        the default threshold 1.5, asymptoting to 1.0. Deterministic: ids
        sorted lexicographically.
        """
        if not self.product_candidates:
            self.resolved_product_ids = []
            self.product_resolution_confidence = 0.0
            return []
        top = max(c.score for c in self.product_candidates)
        gate = max(top - product_resolution_margin, product_resolution_threshold)
        resolved = sorted(
            c.product_id for c in self.product_candidates if c.score > 0 and c.score >= gate
        )
        self.resolved_product_ids = resolved
        # Confidence is the normalized top score ONLY when the gates cleared;
        # a below-gate top still reports 0.0 (ambiguity preserved).
        self.product_resolution_confidence = top / (top + 1.0) if resolved else 0.0
        return resolved

    def recompute(
        self,
        max_representatives: int = 5,
        product_resolution_threshold: float = 1.5,
        product_resolution_margin: float = 1.0,
    ) -> None:
        """Recenter centroid, cohesion, medoid, and representative picks.

        Representatives = medoid + greedy diversity picks (lowest max-similarity
        to already-picked members), bounded by ``max_representatives``.
        """
        if not self.member_ids:
            self.centroid = []
            self.cohesion = 0.0
            self.medoid_comment_id = None
            self.representative_comment_ids = []
            self.resolve_products(product_resolution_threshold, product_resolution_margin)
            return
        self.resolve_products(product_resolution_threshold, product_resolution_margin)
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
class ReconciliationResult:
    """Typed counters of one reconciliation pass (surfaced via reducer stats)."""

    clusters_before: int = 0
    clusters_after: int = 0
    merged: int = 0
    split: int = 0
    members_removed: int = 0


@dataclass(frozen=True)
class ReconciliationFailure:
    """Typed content-safe diagnostic of one FAILED reconciliation pass (5.7).

    Deliberately free of raw viewer text: only ids and counts, so it is safe
    to surface in stats/diagnostics. ``at`` uses the store's injected clock.
    """

    session_id: str
    failure_code: str
    error_message: str
    at: float
    clusters_before: int
    members_before: int
    restored: bool

    def to_dict(self) -> dict:
        return asdict(self)


class ReconciliationError(Exception):
    """Raised by ``ClusterStore.reconcile`` when a pass fails mid-way.

    The store has already restored the pre-pass state before this is raised;
    the fast lane catches it to record diagnostics and keep operating.
    """

    def __init__(self, failure: ReconciliationFailure) -> None:
        super().__init__(failure.error_message)
        self.failure = failure


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
    # Reconciliation knobs (OpenSpec 5.5): mirror the FastReducerConfig knobs.
    reconcile_unreconciled_threshold: int = 100
    reconcile_age_sec: float = 60.0
    # A cluster is only split when it is far less cohesive than the merge
    # threshold: 0.375 merges similar comments, 0.15 splits members whose
    # average similarity to the centroid is well below that bar — i.e. the
    # cluster never hovers in the zone where comments would have been merged
    # into it in the first place. Low values prevent split/merge oscillation.
    cohesion_split_threshold: float = 0.15
    # Product resolution gates (OpenSpec 6.3): a candidate resolves only when
    # its merged score is >= product_resolution_threshold AND within
    # product_resolution_margin of the top score. No candidate cleared both ->
    # resolved=[] and confidence=0.0 (ambiguity preserved, never silent top-1).
    product_resolution_threshold: float = 1.5
    product_resolution_margin: float = 1.0

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
        if self.reconcile_unreconciled_threshold <= 0:
            raise ValueError("reconcile_unreconciled_threshold must be > 0")
        if self.reconcile_age_sec <= 0:
            raise ValueError("reconcile_age_sec must be > 0")
        if self.cohesion_split_threshold <= 0:
            raise ValueError("cohesion_split_threshold must be > 0")
        if self.product_resolution_threshold <= 0:
            raise ValueError("product_resolution_threshold must be > 0")
        if self.product_resolution_margin < 0:
            raise ValueError("product_resolution_margin must be >= 0")


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
        self._evicted_members = 0
        self._evicted_clusters = 0
        # Reconciliation trigger state (OpenSpec 5.5): count/age of unreconciled
        # comments since the last reconciliation. PURE trigger state — assign()
        # never delays fast-lane assignment because of it.
        self._unreconciled_count = 0
        self._first_unreconciled_at: Optional[float] = None
        # Last failed reconciliation (5.7): None until the first failure, then
        # replaced by every subsequent failure.
        self.last_reconciliation_failure: Optional[ReconciliationFailure] = None
        # Test-only failure injection (5.7): when set, the next reconcile pass
        # fails at the named phase after the snapshot, then the seam clears.
        self._fail_next_reconcile_at: Optional[str] = None

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
        best.add_member(
            comment_id,
            text,
            vector,
            ts,
            viewer_key,
            intent,
            candidates,
            product_resolution_threshold=self._config.product_resolution_threshold,
            product_resolution_margin=self._config.product_resolution_margin,
        )
        self._total_members_assigned += 1
        # Every fast-lane assignment is an unreconciled comment; the first one
        # after a reconciliation anchors the age timer.
        self._unreconciled_count += 1
        if self._first_unreconciled_at is None:
            self._first_unreconciled_at = ts
        # Bounds run after the add so a fresh 0-member cluster never evicts itself.
        self._enforce_cluster_bound()
        self._enforce_member_bound(best)
        return best.cluster_id

    # ------------------------------------------------------------------
    # Expiry (5.4)
    # ------------------------------------------------------------------

    def expire(self, now: float) -> int:
        """Evict members older than the rolling horizon; returns evicted count.

        Per-member timestamps make this a plain filter: members whose
        ``ts < now - rolling_horizon_sec`` are dropped from ALL clusters,
        affected clusters are recomputed, and clusters left empty are
        removed. Expiry is the write-path memory bound (5.9) and only
        touches state outside the horizon — nothing extra is retained.
        """
        cutoff = now - self._config.rolling_horizon_sec
        evicted = 0
        for cluster in list(self._clusters.values()):
            for cid in [c for c in cluster.member_ids if cluster._member_ts[c] < cutoff]:
                cluster._drop_member_with_ts(cid)
                evicted += 1
            if not cluster.member_ids:
                self._clusters.pop(cluster.cluster_id)
                self._evicted_clusters += 1
            else:
                cluster.newest_t = max(
                    (cluster._member_ts[cid] for cid in cluster.member_ids), default=0.0
                )
                cluster.recompute(
                    product_resolution_threshold=self._config.product_resolution_threshold,
                    product_resolution_margin=self._config.product_resolution_margin,
                )
        self._evicted_members += evicted
        return evicted

    # ------------------------------------------------------------------
    # Reconciliation (5.5-5.6) — NEVER called from the fast-lane hot path
    # ------------------------------------------------------------------

    def reconcile_due(self, now: float) -> bool:
        """True when count OR age threshold is met (whichever comes first).

        Count: ``unreconciled_count >= reconcile_unreconciled_threshold``.
        Age: ``now - first_unreconciled_at >= reconcile_age_sec`` once the
        first unreconciled comment arrived. Pure trigger state — it never
        gates fast-lane assignment.
        """
        if self._unreconciled_count >= self._config.reconcile_unreconciled_threshold:
            return True
        if self._first_unreconciled_at is not None:
            return now - self._first_unreconciled_at >= self._config.reconcile_age_sec
        return False

    def _reset_reconciliation_trigger(self) -> None:
        """Clear the trigger state after a successful reconciliation."""
        self._unreconciled_count = 0
        self._first_unreconciled_at = None

    def reconcile(self, now: float) -> ReconciliationResult:
        """Deterministic bounded repair of the active horizon (OpenSpec 5.6).

        Steps: expire members outside the horizon, merge compatible active
        clusters (greedy first-match in demand order, surviving stable id =
        higher message_count, tie-break lower cluster_id), split active
        clusters with cohesion below the split threshold in one greedy pass,
        recompute every touched cluster, and enforce the member bound. Cold
        clusters outside the horizon are never touched. Members are moved via
        add_member/remove_member (idempotent) so identities/demand are never
        duplicated or lost.

        Fail-safe (5.7): the pass is transactional — the affected clusters and
        the trigger state are snapshotted first, and any mid-pass exception
        restores the snapshot verbatim, records a typed
        ``ReconciliationFailure``, and re-raises ``ReconciliationError`` so the
        fast lane stays on the last valid cluster state.
        """
        snapshot = {cid: copy.deepcopy(cluster) for cid, cluster in self._clusters.items()}
        # ponytail: deep copy is bounded by max_active_clusters * max_members —
        # swap for structural sharing if a reconciliation ever gets heavy.
        trigger = (self._unreconciled_count, self._first_unreconciled_at)
        before = len(self._clusters)
        members_before = sum(len(c.member_ids) for c in self._clusters.values())
        result = ReconciliationResult(clusters_before=before)
        phase = "merge_state_corruption"
        try:
            # Test-only failure seam (5.7): one-shot, consumed before mutation.
            if self._fail_next_reconcile_at is not None:
                phase = self._fail_next_reconcile_at
                self._fail_next_reconcile_at = None
                raise ValueError(f"injected reconciliation failure at {phase}")
            result.members_removed = self.expire(now)
            phase = "expiry_error"
            self._reset_reconciliation_trigger()
            active = self.active_clusters(now)
            merged_away: set[str] = set()
            for survivor in active:
                if survivor.cluster_id in merged_away:
                    continue
                candidates = [
                    c
                    for c in active
                    if c.cluster_id not in merged_away
                    and c.cluster_id != survivor.cluster_id
                    and c.intent == survivor.intent
                    and self._mergeable(survivor, c)
                ]
                if not candidates:
                    continue
                candidates.sort(key=lambda c: (-c.message_count, c.cluster_id))
                other = candidates[0]
                if self._should_survive(other, survivor):
                    survivor, other = other, survivor
                for member_id in list(other.member_ids):
                    survivor.add_member(
                        comment_id=member_id,
                        text=other._member_texts[member_id],
                        vector=other._member_embeddings[member_id],
                        ts=other._member_ts[member_id],
                        viewer_key=other._member_viewers[member_id],
                        intent=survivor.intent,
                        product_candidates=other.product_candidates,
                        product_resolution_threshold=self._config.product_resolution_threshold,
                        product_resolution_margin=self._config.product_resolution_margin,
                    )
                    other.remove_member(member_id)
                self._clusters.pop(other.cluster_id, None)
                merged_away.add(other.cluster_id)
                result.merged += 1
                survivor.recompute(
                    product_resolution_threshold=self._config.product_resolution_threshold,
                    product_resolution_margin=self._config.product_resolution_margin,
                )
                # Boundedness: merged member counts may exceed the cap — evict
                # the oldest deterministically via the existing member-bound path.
                self._enforce_member_bound(survivor)

            for cluster in active:
                if cluster.cluster_id in merged_away or not cluster.member_ids:
                    continue
                if (
                    cluster.cohesion < self._config.cohesion_split_threshold
                    and len(cluster.member_ids) >= 2
                ):
                    self._split_low_cohesion(cluster, merged_away)
                    result.split += 1
                    # Merging into others may overflow their cap — evict oldest.
                    for other in self._clusters.values():
                        self._enforce_member_bound(other)

            self._enforce_cluster_bound()
            result.clusters_after = len(self._clusters)
        except Exception as exc:  # noqa: BLE001 - any mid-pass failure restores state
            failure = self._record_failure(exc, phase, before, members_before, trigger)
            self._restore(snapshot, trigger)
            raise ReconciliationError(failure) from exc
        return result

    def _record_failure(
        self,
        exc: Exception,
        phase: str,
        clusters_before: int,
        members_before: int,
        trigger: tuple[int, Optional[float]],
    ) -> ReconciliationFailure:
        """Record a typed content-safe diagnostic (5.7) for the failed pass.

        The message is sanitized by ``str(exc)`` — the error text may be
        derived from malformed cluster data, so diagnostics must never embed
        raw viewer text.
        """
        failure = ReconciliationFailure(
            session_id=self.session_id,
            failure_code=phase,
            error_message=str(exc),
            at=self._now(),
            clusters_before=clusters_before,
            members_before=members_before,
            restored=True,
        )
        self.last_reconciliation_failure = failure
        return failure

    def _restore(
        self,
        snapshot: dict[str, LiveCluster],
        trigger: tuple[int, Optional[float]],
    ) -> None:
        """Restore the pre-pass state verbatim so the fast lane keeps operating."""
        self._clusters = {cid: copy.deepcopy(c) for cid, c in snapshot.items()}
        self._unreconciled_count, self._first_unreconciled_at = trigger

    def _mergeable(self, a: LiveCluster, b: LiveCluster) -> bool:
        """Centroid-compatible AND intent-compatible AND products non-conflicting."""
        if a.intent != b.intent:
            return False
        if cosine(a.centroid, b.centroid) < self._config.merge_threshold:
            return False
        # Never merge clusters with clearly conflicting resolved products:
        # both resolved non-empty and disjoint (or identically empty with
        # conflicting candidates) is a conflict; one side empty is fine.
        if a.resolved_product_ids and b.resolved_product_ids:
            return bool(set(a.resolved_product_ids) & set(b.resolved_product_ids))
        if a.resolved_product_ids or b.resolved_product_ids:
            return True
        a_ids = {c.product_id for c in a.product_candidates}
        b_ids = {c.product_id for c in b.product_candidates}
        if a_ids and b_ids and not (a_ids & b_ids):
            return False
        return True

    @staticmethod
    def _should_survive(a: LiveCluster, b: LiveCluster) -> bool:
        """True when ``a`` is the deterministic survivor: higher message_count,
        tie-break lower cluster_id (lexicographic)."""
        if a.message_count != b.message_count:
            return a.message_count > b.message_count
        return a.cluster_id < b.cluster_id

    def _split_low_cohesion(self, cluster: LiveCluster, removed: set[str]) -> None:
        """One deterministic greedy pass: members by (ts asc) move to the best
        other active cluster when cosine >= merge_threshold, else stay."""
        others = [
            c
            for c in self._clusters.values()
            if c.cluster_id != cluster.cluster_id and c.cluster_id not in removed and c.member_ids
        ]
        others.sort(key=lambda c: (-c.message_count, c.cluster_id))
        for member_id in sorted(cluster.member_ids, key=lambda cid: cluster._member_ts[cid]):
            # Read member data BEFORE removal — remove_member drops it.
            text = cluster._member_texts[member_id]
            vector = cluster._member_embeddings[member_id]
            ts = cluster._member_ts[member_id]
            viewer_key = cluster._member_viewers[member_id]
            best, best_sim = None, self._config.merge_threshold
            for other in others:
                sim = cosine(vector, other.centroid)
                if sim >= best_sim:
                    best, best_sim = other, sim
            if best is None:
                continue
            cluster.remove_member(member_id)
            best.add_member(
                comment_id=member_id,
                text=text,
                vector=vector,
                ts=ts,
                viewer_key=viewer_key,
                intent=cluster.intent,
                product_candidates=cluster.product_candidates,
                product_resolution_threshold=self._config.product_resolution_threshold,
                product_resolution_margin=self._config.product_resolution_margin,
            )
        if not cluster.member_ids:
            self._clusters.pop(cluster.cluster_id, None)

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

    def cluster_count(self) -> int:
        """Total clusters currently held (public read for the reducer seam)."""
        return len(self._clusters)

    def stats(self) -> dict:
        """Content-safe counters (no raw viewer text)."""
        failure = self.last_reconciliation_failure
        return {
            "session_id": self.session_id,
            "active_cluster_count": len(self._clusters),
            "total_clusters_created": self._total_clusters_created,
            "total_members_assigned": self._total_members_assigned,
            "member_ids_count": sum(len(c.member_ids) for c in self._clusters.values()),
            "evicted_count": self._evicted_count,
            "evicted_members": self._evicted_members,
            "evicted_clusters": self._evicted_clusters,
            "unreconciled_count": self._unreconciled_count,
            "reconciliation_failures": 1 if failure is not None else 0,
            "last_reconciliation_failure": None if failure is None else failure.to_dict(),
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
