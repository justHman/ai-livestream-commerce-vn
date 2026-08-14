"""Canonical cluster envelope for the agentic director (tasks 7.1, 7.3).

``ClusterEnvelope`` is the single untrusted, content-safe projection of one
``LiveCluster`` handed to the agentic director. It satisfies the duck-typed
``fast_path.ClusterEnvelope`` Protocol structurally (field presence, not
inheritance) and adds the ``score_breakdown`` component the Protocol omits
(design Decision 9).

Trust boundary: the envelope carries EVIDENCE ONLY — representative question
texts (capped), product candidate scores, resolved product ids, platform
counts. It never embeds model instructions, tool schemas, or mutable runtime
authority, and never leaks the raw member corpus (member ids, viewer ids or
the private ``_member_texts`` dict). ``build_envelope`` is a pure function:
no store mutation, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional

from .cluster_store import ClusterStoreConfig, LiveCluster

__all__ = ["ClusterEnvelope", "build_envelope"]


@dataclass(frozen=True, slots=True)
class ClusterEnvelope:
    """Immutable evidence-only projection of one cluster.

    Why is the envelope untrusted evidence? It is the ONLY cluster-derived
    input the agentic director reasons over, so anything beyond evidence —
    instructions, tool schemas, mutable runtime authority — would let cluster
    content steer model behavior. Every field here is derived data the
    director may cite, never a directive it must obey (design Decision 9).
    """

    cluster_id: str
    intent: str
    message_count: int
    unique_viewer_count: int
    representative_questions: tuple[str, ...]
    product_candidates: tuple[tuple[str, float], ...]
    resolved_product_ids: tuple[str, ...]
    ranking_score: float
    score_breakdown: tuple[tuple[str, float], ...]
    novelty: float
    current_script_product_id: str | None
    source_platform_counts: tuple[tuple[str, int], ...]


def _representative_questions(cluster: LiveCluster, max_representatives: int) -> tuple[str, ...]:
    """Capped question texts from the representative members only.

    ``_member_texts`` is the seam: the texts live in the private dict (keyed by
    comment_id, needed for recompute) and the public ``member_ids`` / the
    representative picks are what select from it. Reading the private dict
    here is deliberate — it keeps the representative boundary in one place
    instead of duplicating member text storage on the envelope.
    """
    picked = cluster.representative_comment_ids or (
        [cluster.medoid_comment_id] if cluster.medoid_comment_id else []
    )
    questions = [cluster._member_texts[cid] for cid in picked if cid in cluster._member_texts]
    return tuple(questions[:max_representatives])


def _source_platform_counts(cluster: LiveCluster) -> tuple[tuple[str, int], ...]:
    """Per-platform viewer counts from the ``{platform}:...`` viewer_key prefix."""
    counts: dict[str, int] = {}
    for viewer_key in cluster.viewer_ids:
        platform = viewer_key.partition(":")[0] if viewer_key else "unknown"
        counts[platform] = counts.get(platform, 0) + 1
    return tuple(sorted(counts.items()))


def build_envelope(
    cluster: LiveCluster,
    score_breakdown: tuple[tuple[str, float], ...] | Mapping[str, float],
    ranking_score: float,
    novelty: float,
    current_script_product_id: str | None = None,
    config: Optional[ClusterStoreConfig] = None,
) -> ClusterEnvelope:
    """Build the immutable evidence-only envelope from one cluster.

    Pure function: reads the cluster, never mutates it. ``score_breakdown`` is
    normalized to a tuple preserving the caller's order (``rank_clusters``
    display order is meaningful: product_relevance, intent_actionability,
    size, recency, phase, new_demand, total).
    """
    max_representatives = config.max_representatives if config is not None else 5
    breakdown = (
        tuple(score_breakdown.items())
        if isinstance(score_breakdown, Mapping)
        else tuple(score_breakdown)
    )
    return ClusterEnvelope(
        cluster_id=cluster.cluster_id,
        intent=cluster.intent,
        message_count=cluster.message_count,
        unique_viewer_count=cluster.unique_viewer_count,
        representative_questions=_representative_questions(cluster, max_representatives),
        product_candidates=tuple((c.product_id, c.score) for c in cluster.product_candidates),
        resolved_product_ids=tuple(cluster.resolved_product_ids),
        ranking_score=ranking_score,
        score_breakdown=breakdown,
        novelty=novelty,
        current_script_product_id=current_script_product_id,
        source_platform_counts=_source_platform_counts(cluster),
    )
