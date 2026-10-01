"""Reducer -> Director adapter (P0-FB-014).

The bounded reducer (``FastReducer`` -> ``ClusterStore`` -> ``ClusterEnvelope``)
is the ONLY viewer-demand input to the Director for a P0 session. This module
is the seam, and it is pure: no store mutation, no I/O, no coordinator import.

    session ClusterStore -> active_clusters(now) -> score_clusters(...)
        -> build_envelope(...) -> ReducerSelection
        -> legacy Cluster/ScoredCluster shape -> Director.decide_from_reducer

Two rules the seam exists to enforce:

* **Bounded content.** ``Cluster.members`` carries ONLY the envelope's
  ``representative_questions`` (already capped at ``max_representatives`` by
  ``build_envelope``). The raw member transcript never becomes prompt context,
  so it is never reconstructed here. The true member COUNT rides on a
  ``_ReducerCluster`` so the legacy size gates still see reality.
* **Provenance beside, never inside.** Member ``event_id``s and the original
  ``occurred_at`` bounds ride on ``ReducerSelection``, beside the envelope,
  because the envelope's trust boundary excludes member and viewer ids
  (``reducer/envelope.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from ..reducer.cluster_store import ClusterStore
from ..reducer.demand import DemandConfig, score_clusters
from ..reducer.envelope import build_envelope
from .clustering import Cluster
from .decision import Decision
from .scoring import ScoredCluster, score_cluster

__all__ = [
    "ReducerSelection",
    "build_selections",
    "decide_from_reducer",
    "select_scored",
]


@dataclass(frozen=True)
class ReducerSelection:
    """One reducer cluster as Director input, with provenance beside it.

    ``member_comment_ids`` is the FULL member set (answered-state suppression,
    idempotent action); ``cluster_members`` is only the representative question
    texts, which is the only thing allowed to reach a prompt.
    """

    envelope: Any
    member_comment_ids: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    oldest_occurred_at: float
    newest_occurred_at: float
    cluster_members: tuple[str, ...]
    resolved_product_ids: tuple[str, ...]
    # Director-clock instant of the newest member: the same conversion the
    # legacy path applies at ``coordinator._tick_once``.
    cluster_newest_t: float
    intent: str = "unknown"


@dataclass
class _ReducerCluster(Cluster):
    """A legacy ``Cluster`` whose ``size`` is the true member count.

    ``members`` is capped to the representative questions (bounded prompt
    context), so ``Cluster.size`` would under-report. Overriding the property
    keeps every legacy size gate — singleton drop, ``size_norm``, the >= 2
    novelty check — reading the real cluster size.
    """

    member_count: int = 0

    @property
    def size(self) -> int:
        return self.member_count


def build_selections(
    director: Any,
    *,
    store: Optional[ClusterStore],
    reducer_now: float,
    director_now: float,
    wall_now: float,
    provenance: Optional[Callable[[str], Optional[dict]]] = None,
    demand_config: Optional[DemandConfig] = None,
) -> list[ReducerSelection]:
    """Project one session's live clusters into Director-shaped selections.

    ``store is None`` means reducer mode is NOT actually active (the 013
    truthful-outcome opt-in is off, or the reducer has no session yet). That
    REFUSES: the caller gets an empty list and decides nothing, rather than
    silently falling back to the legacy raw-comment feed.

    Two clocks, deliberately: ``reducer_now`` is the reducer's own clock
    (cluster horizon + demand recency), ``director_now`` / ``wall_now`` drive
    the legacy conversion so a selected cluster's age is the real one and is
    never reset.
    """
    if store is None:
        return []
    active = store.active_clusters(reducer_now)
    if not active:
        return []
    current = director.state.current_product()
    current_product_id = current.product_id if current is not None else None
    scores = {
        d.cluster_id: d
        for d in score_clusters(
            active, current_product_id, reducer_now, demand_config or DemandConfig()
        )
    }
    selections: list[ReducerSelection] = []
    for cluster in active:
        score = scores.get(cluster.cluster_id)
        if score is None:
            continue  # non-actionable intent: never inflates actionable demand
        envelope = build_envelope(
            cluster,
            score_breakdown=score.breakdown(),
            ranking_score=score.score,
            novelty=score.novelty_score,
            current_script_product_id=current_product_id,
            config=getattr(store, "config", None),
        )
        stamps = [
            cluster._member_ts[cid] for cid in cluster.member_ids if cid in cluster._member_ts
        ]
        event_ids: list[str] = []
        if provenance is not None:
            for cid in cluster.member_ids:
                record = provenance(cid)
                if record and record.get("event_id"):
                    event_ids.append(str(record["event_id"]))
        selections.append(
            ReducerSelection(
                envelope=envelope,
                member_comment_ids=tuple(cluster.member_ids),
                source_event_ids=tuple(dict.fromkeys(event_ids)),
                oldest_occurred_at=min(stamps) if stamps else cluster.created_at,
                newest_occurred_at=max(stamps) if stamps else cluster.updated_at,
                cluster_members=envelope.representative_questions,
                resolved_product_ids=envelope.resolved_product_ids,
                cluster_newest_t=director_now - max(0.0, wall_now - cluster.newest_t),
                intent=cluster.intent,
            )
        )
    selections.sort(key=lambda s: (-s.envelope.ranking_score, s.envelope.cluster_id))
    return selections


def _as_cluster(selection: ReducerSelection) -> _ReducerCluster:
    """Convert one selection to the legacy ``Cluster`` shape.

    ``members`` = representative questions only. ``product_id`` = the single
    resolved id, or None when the reducer preserved ambiguity.
    """
    return _ReducerCluster(
        centroid=[],
        members=list(selection.cluster_members),
        member_ids=list(selection.member_comment_ids),
        newest_t=selection.cluster_newest_t,
        product_id=(
            selection.resolved_product_ids[0] if len(selection.resolved_product_ids) == 1 else None
        ),
        intent=selection.intent,
        actionable=True,
        member_count=len(selection.member_comment_ids),
    )


def select_scored(
    director: Any,
    selections: Sequence[ReducerSelection],
    *,
    now: float,
) -> list[ScoredCluster]:
    """Rank selections with the legacy scorer so ``_decide`` keeps its logic.

    No ``cluster_comments`` / ``rank_clusters`` over raw comments here — the
    clusters already exist, so only the bounded ranking is reused. Clusters
    whose every member is already answered are dropped, matching the legacy
    filter, and the same age/skip eviction bounds apply.
    """
    cfg = director.cfg
    state = director.state
    clusters = [_as_cluster(s) for s in selections]
    max_size = max((c.size for c in clusters), default=1)
    scored: list[ScoredCluster] = []
    for cluster in clusters:
        if cluster.member_ids and all(
            member_id in state.answered_comments for member_id in cluster.member_ids
        ):
            continue
        if (
            now - cluster.newest_t > cfg.cluster_max_age_sec
            or cluster.skips > cfg.cluster_max_skips
        ):
            continue
        scored.append(score_cluster(cluster, state, cfg, now, max_size))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored


def decide_from_reducer(
    director: Any,
    selections: Sequence[ReducerSelection],
    *,
    now: float,
    high_value_ids: Optional[Callable[[str], bool]] = None,
) -> Decision:
    """Produce the next Decision from reducer output alone.

    Delegates to the SAME ``_decide`` the legacy feed uses, with the ranked
    clusters injected so ``cluster_comments`` / ``rank_clusters`` never run.
    The four stage-only exclusions are relaxed ONLY for clusters the
    configured high-value predicate matches; everything else the legacy path
    enforces — pivot checkpoint, no nested pivot, cooldown + signature
    suppression, ``mark_answered``, the protected opening — is reused as is.
    """
    by_cluster: dict[str, ReducerSelection] = {s.envelope.cluster_id: s for s in selections}
    ranked = select_scored(director, selections, now=now)
    high = high_value_ids or (lambda _cid: False)
    return director._decide(
        [],
        now,
        ranked=ranked,
        by_cluster=by_cluster,
        is_high_value=high,
    )


def attach_provenance(decision: Decision, by_cluster: dict[str, ReducerSelection]) -> None:
    """Stamp the selected cluster's provenance onto a reducer-driven decision."""
    if decision.source_cluster_id is None:
        return
    selection = by_cluster.get(decision.source_cluster_id)
    if selection is None:
        return
    decision.source_event_ids = selection.source_event_ids
    decision.occurred_at_bounds = (
        selection.oldest_occurred_at,
        selection.newest_occurred_at,
    )
