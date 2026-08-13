"""FastReducer — event-driven fast lane for the live-demand pipeline (OpenSpec 4.1-4.6).

The ingestion pipeline signals this reducer the only way it learns of work:
``notify_new_events`` appends an accepted comment to the session's pending
batch, anchors the coalescing deadline on the FIRST pending item, and sets a
per-session ``asyncio.Event``. There is no timer and no polling — an idle
session has no deadline and never wakes on its own (4.1). A future loop calls
``wait_for_work`` and then ``run_once``.

``run_once`` drains the pending batch, embeds ONLY items whose comment id is
not cached (or whose text differs — a revision re-embeds, 4.3), appends the
embedded items to an in-memory per-session demand store, and prunes demand
older than the rolling horizon. The horizon (default 75s) is independent of
microbatch timing (4.4). Clustering/ranking are later clusters; this class
only produces the snapshot those clusters consume.

Embedding runs via ``asyncio.to_thread`` so a sync embedder (hash fallback or
sentence-transformers) never blocks the event loop. The clock is injectable
(``now_fn``) so all timing logic is deterministic in tests.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .cluster_store import ClusterStore, ClusterStoreConfig, ReconciliationResult

__all__ = ["AcceptedComment", "FastReducer", "FastReducerConfig"]


@dataclass(frozen=True)
class AcceptedComment:
    """One semantic item accepted by the ingestion pipeline.

    ``event_id`` is the idempotency key of the source platform event;
    ``comment_id`` is the identity/revision key for the embedding cache — a
    revised text under the same id re-embeds.
    """

    event_id: str
    comment_id: str
    text: str
    ts: float
    viewer_key: Optional[str] = None


@dataclass
class FastReducerConfig:
    """Typed knobs for the fast lane; dashboard-tunable wiring comes later.

    ``microbatch_max_wait_ms`` is a MAXIMUM coalescing delay, not a periodic
    tick: the reducer sleeps from the first pending item until this much time
    has passed, then processes once.
    """

    microbatch_max_wait_ms: int = 300
    rolling_horizon_sec: float = 75.0
    max_pending: int = 500
    cluster_merge_threshold: float = 0.375
    max_active_clusters: int = 40
    max_members_per_cluster: int = 200
    max_representatives: int = 5
    # Reconciliation knobs (OpenSpec 5.5) — mirror the ClusterStoreConfig knobs.
    reconcile_unreconciled_threshold: int = 100
    reconcile_age_sec: float = 60.0
    cohesion_split_threshold: float = 0.15

    def validate_runtime(self) -> None:
        """Fail-fast on non-positive knobs (called by the reducer at init)."""
        if self.microbatch_max_wait_ms <= 0:
            raise ValueError("microbatch_max_wait_ms must be > 0")
        if self.rolling_horizon_sec <= 0:
            raise ValueError("rolling_horizon_sec must be > 0")
        if self.max_pending <= 0:
            raise ValueError("max_pending must be > 0")
        if self.cluster_merge_threshold <= 0:
            raise ValueError("cluster_merge_threshold must be > 0")
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


@dataclass
class _SessionState:
    """Per-session reducer state (pending batch + demand + counters)."""

    pending: list[AcceptedComment] = field(default_factory=list)
    first_pending_ts: Optional[float] = None
    wake_event: asyncio.Event = field(default_factory=asyncio.Event)
    cache: dict[str, tuple[str, list[float]]] = field(default_factory=dict)
    demand: list[dict] = field(default_factory=list)
    embedded_total: int = 0
    embed_calls: int = 0
    cache_hits: int = 0
    wake_notifications: int = 0


class FastReducer:
    """Event-driven fast lane; one instance serves all sessions.

    ``embedder`` must expose ``encode(texts: list[str]) -> list[list[float]]``
    (HashingEmbedder / BiEncoderEmbedder from director.embeddings). ``now_fn``
    is the injectable monotonic-ish clock (defaults to ``time.time``).
    """

    def __init__(
        self,
        config: Optional[FastReducerConfig] = None,
        embedder: Optional[Any] = None,
        now_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        self._config = config or FastReducerConfig()
        self._config.validate_runtime()
        if embedder is None:
            raise ValueError("embedder is required (encode(texts) -> list[list[float]])")
        self._embedder = embedder
        self._now = now_fn or time.time
        self._sessions: dict[str, _SessionState] = {}
        self._stores: dict[str, ClusterStore] = {}
        # Reconciliation observability (OpenSpec 5.6): cumulative counters plus
        # the last run's per-pass counters.
        self._reconciles_run: dict[str, int] = {}
        self._reconcile_merged_total: dict[str, int] = {}
        self._reconcile_split_total: dict[str, int] = {}
        self._last_reconcile: dict[str, ReconciliationResult] = {}

    # ------------------------------------------------------------------
    # Per-session cluster store (5.3)
    # ------------------------------------------------------------------

    def _get_store(self, session_id: str) -> ClusterStore:
        """Lazily create the session's cluster store on first use."""
        store = self._stores.get(session_id)
        if store is None:
            store = ClusterStore(
                session_id,
                config=ClusterStoreConfig(
                    merge_threshold=self._config.cluster_merge_threshold,
                    rolling_horizon_sec=self._config.rolling_horizon_sec,
                    max_active_clusters=self._config.max_active_clusters,
                    max_members_per_cluster=self._config.max_members_per_cluster,
                    max_representatives=self._config.max_representatives,
                    reconcile_unreconciled_threshold=self._config.reconcile_unreconciled_threshold,
                    reconcile_age_sec=self._config.reconcile_age_sec,
                    cohesion_split_threshold=self._config.cohesion_split_threshold,
                ),
                now_fn=self._now,
            )
            self._stores[session_id] = store
        return store

    # ------------------------------------------------------------------
    # Wakeup seam (the ONLY way the reducer learns of work)
    # ------------------------------------------------------------------

    def notify_new_events(self, session_id: str, comment: Optional[AcceptedComment] = None) -> None:
        """Signal that accepted semantic work exists for a session.

        When ``comment`` is given, it is appended to the session's pending
        batch; the coalescing deadline anchors on the FIRST pending item and
        is not reset by later adds. Sets the per-session ``asyncio.Event`` so
        a waiting loop wakes (4.1). Calling without a comment only wakes the
        waiter (work already queued elsewhere) and is a no-op otherwise.
        """
        state = self._sessions.setdefault(session_id, _SessionState())
        if comment is not None:
            state.pending.append(comment)
            if state.first_pending_ts is None:
                state.first_pending_ts = comment.ts
        state.wake_notifications += 1
        state.wake_event.set()

    # ------------------------------------------------------------------
    # Coalescing deadline (pure function of the injected clock)
    # ------------------------------------------------------------------

    def pending_deadline(self, session_id: str, now: float) -> Optional[float]:
        """Deadline for the current pending batch, None when idle.

        None when there is no pending work: an idle session has no deadline
        and must not wake (4.1). Otherwise ``first_pending_ts +
        microbatch_max_wait_ms/1000`` — a maximum coalescing delay measured
        from the first pending item, never a periodic tick (4.2).
        """
        state = self._sessions.get(session_id)
        if state is None or state.first_pending_ts is None:
            return None
        return state.first_pending_ts + self._config.microbatch_max_wait_ms / 1000.0

    def drain_batch(self, session_id: str, now: float) -> list[AcceptedComment]:
        """Return and clear the pending batch (bounded, oldest dropped).

        Called when the deadline has passed or work is being processed.
        Resets ``first_pending_ts``; also resets the wake event so a stale
        wake does not re-trigger an immediate spin in ``wait_for_work``.
        """
        state = self._sessions.get(session_id)
        if state is None:
            return []
        batch = state.pending
        if len(batch) > self._config.max_pending:
            batch = batch[-self._config.max_pending :]
        state.pending = []
        state.first_pending_ts = None
        state.wake_event.clear()
        return batch

    # ------------------------------------------------------------------
    # Fast-lane step
    # ------------------------------------------------------------------

    async def run_once(self, session_id: str, now: float) -> list[AcceptedComment]:
        """Process one pending batch through the fast lane (4.3/4.4).

        Drains the batch, embeds ONLY items whose comment_id is not cached or
        whose text differs (revision re-embeds), stores ``comment_id ->
        (text, vector)``, appends embedded items to the session's demand
        store, then prunes demand older than ``now - rolling_horizon_sec``.
        Returns the batch that was processed so later clusters can consume
        it; clustering/ranking are NOT implemented here (C5/C6 scope).
        """
        state = self._sessions.setdefault(session_id, _SessionState())
        batch = self.drain_batch(session_id, now)
        if not batch:
            return []
        new_only = [
            c
            for c in batch
            if c.comment_id not in state.cache or state.cache[c.comment_id][0] != c.text
        ]
        cache_hits = len(batch) - len(new_only)
        if cache_hits:
            state.cache_hits += cache_hits
        if new_only:
            vectors = await asyncio.to_thread(self._embedder.encode, [c.text for c in new_only])
            state.embed_calls += 1
            for comment, vector in zip(new_only, vectors):
                state.cache[comment.comment_id] = (comment.text, list(vector))
        embedded = []
        for comment in batch:
            entry = state.cache.get(comment.comment_id)
            if entry is not None and entry[0] == comment.text:
                embedded.append(
                    {
                        "comment_id": comment.comment_id,
                        "text": comment.text,
                        "ts": comment.ts,
                        "viewer_key": comment.viewer_key,
                        "embedded_at": now,
                    }
                )
        state.demand.extend(embedded)
        state.embedded_total += len(embedded)
        cutoff = now - self._config.rolling_horizon_sec
        state.demand = [d for d in state.demand if d["ts"] >= cutoff]
        store = self._get_store(session_id)
        for comment in batch:
            entry = state.cache.get(comment.comment_id)
            if entry is not None and entry[0] == comment.text:
                store.assign(
                    comment_id=comment.comment_id,
                    text=comment.text,
                    vector=entry[1],
                    ts=comment.ts,
                    viewer_key=comment.viewer_key,
                    intent="unknown",
                    product_candidates=[],
                )
        # Write-path expiry keeps store memory bounded as the horizon advances (5.4).
        store.expire(now)
        return batch

    # ------------------------------------------------------------------
    # Reconciliation (5.5-5.6) — invoked by the caller when due; never on the
    # fast-lane hot path (run_once never calls it). Pure CPU on small bounded
    # data, so it is called directly, not via asyncio.to_thread.
    # ------------------------------------------------------------------

    def reconciliation_due(self, session_id: str, now: float) -> bool:
        """Delegate the count/age trigger check to the session's store."""
        return self._get_store(session_id).reconcile_due(now)

    async def reconcile(self, session_id: str, now: float) -> ReconciliationResult:
        """Run the store's bounded reconciliation when due (no-op otherwise).

        After a successful pass the trigger state resets (store-side) and the
        per-session cumulative counters accumulate.
        """
        store = self._get_store(session_id)
        if not store.reconcile_due(now):
            return ReconciliationResult(
                clusters_before=len(store._clusters), clusters_after=len(store._clusters)
            )
        result = store.reconcile(now)
        self._reconciles_run[session_id] = self._reconciles_run.get(session_id, 0) + 1
        self._reconcile_merged_total[session_id] = (
            self._reconcile_merged_total.get(session_id, 0) + result.merged
        )
        self._reconcile_split_total[session_id] = (
            self._reconcile_split_total.get(session_id, 0) + result.split
        )
        self._last_reconcile[session_id] = result
        return result

    # ------------------------------------------------------------------
    # Async waiter (used by the future fast-lane loop; 4.6 proof seam)
    # ------------------------------------------------------------------

    async def wait_for_work(self, session_id: str) -> None:
        """Wait until pending work is due, without ever polling.

        Loop: no pending deadline -> wait on the event indefinitely (an idle
        session never wakes on its own); deadline passed -> return; deadline
        in the future -> wait on the event with that remaining timeout, then
        return. The event is consumed by ``drain_batch``, so a stale wake
        without pending work just loops back to the indefinite wait.
        """
        while True:
            now = self._now()
            deadline = self.pending_deadline(session_id, now)
            if deadline is None:
                await self._sessions.setdefault(session_id, _SessionState()).wake_event.wait()
                continue
            remaining = deadline - now
            if remaining <= 0:
                return
            state = self._sessions[session_id]
            try:
                await asyncio.wait_for(state.wake_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
            return

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def demand_snapshot(self, session_id: str, now: float) -> list[dict]:
        """Active demand within the rolling horizon (ts >= now - horizon).

        Each record: ``comment_id, text, ts, viewer_key`` — the "updated
        demand" consumed by the 4.5 benchmark (ranking itself is C6 scope).
        """
        state = self._sessions.get(session_id)
        if state is None:
            return []
        cutoff = now - self._config.rolling_horizon_sec
        return [
            {
                "comment_id": d["comment_id"],
                "text": d["text"],
                "ts": d["ts"],
                "viewer_key": d["viewer_key"],
            }
            for d in state.demand
            if d["ts"] >= cutoff
        ]

    def stats(self, session_id: str) -> dict:
        """Content-safe per-session counters (no raw viewer text)."""
        state = self._sessions.get(session_id)
        if state is None:
            return {
                "pending": 0,
                "embedded_total": 0,
                "embed_calls": 0,
                "cache_hits": 0,
                "demand_size": 0,
                "wake_notifications": 0,
                "reconciles_run": self._reconciles_run.get(session_id, 0),
                "reconcile_merged_total": self._reconcile_merged_total.get(session_id, 0),
                "reconcile_split_total": self._reconcile_split_total.get(session_id, 0),
                "last_reconcile": None,
            }
        last = self._last_reconcile.get(session_id)
        return {
            "pending": len(state.pending),
            "embedded_total": state.embedded_total,
            "embed_calls": state.embed_calls,
            "cache_hits": state.cache_hits,
            "demand_size": len(state.demand),
            "wake_notifications": state.wake_notifications,
            "reconciles_run": self._reconciles_run.get(session_id, 0),
            "reconcile_merged_total": self._reconcile_merged_total.get(session_id, 0),
            "reconcile_split_total": self._reconcile_split_total.get(session_id, 0),
            "last_reconcile": None
            if last is None
            else {
                "clusters_before": last.clusters_before,
                "clusters_after": last.clusters_after,
                "merged": last.merged,
                "split": last.split,
                "members_removed": last.members_removed,
            },
        }
