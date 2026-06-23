"""Cluster scoring + product retrieval (challenge 5, step 5-6).

ONE scoring function, THREE uses (the design insight):
  1. pick the cluster to answer this cycle (highest score)
  2. interrupt-gate: only clusters above interrupt_score_threshold may barge-in
  3. queue-evict: clusters too old / skipped too often are dropped

score = w_phase * phase_relevance      (cosine cluster->current phase target)
      + w_intent * intent_weight       (price/stock/buy boosted; chitchat low)
      + w_size  * norm(cluster_size)    (trending = many people asking)
      + w_recency * recency_decay       (newer = higher, half-life decay)

Retrieval: cluster centroid vs the pre-embedded product catalog (cosine). This
is what resolves "áo đen A vs B" — semantic + the CURRENT product as phase
target, not keyword matching. No BM25.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .cluster import Cluster
from .config import StreamConfig
from .embedder import cosine
from .state import Phase, ProductState, StreamState

# Vietnamese intent keywords -> weight. (Lightweight, not the semantic layer;
# semantic product match is done by embedding retrieval. These only bias which
# *intent* is urgent — price/stock/buy beat greetings.)
INTENT_WEIGHTS = {
    "high": (["giá", "bao nhiêu", "tiền", "ship", "giao", "mua", "đặt", "cọc",
              "còn hàng", "size", "màu", "khuyến mãi", "sale", "giảm", "freeship"], 1.0),
    "mid": (["chất lượng", "review", "thật không", "bền", "dùng", "công dụng"], 0.5),
    "low": (["chào", "hello", "hi", "đẹp", "like", "follow", "tym", "tim"], 0.1),
}


def intent_weight(text: str) -> float:
    t = text.lower()
    for _, (kws, w) in INTENT_WEIGHTS.items():
        if any(k in t for k in kws):
            return w
    return 0.3  # neutral default


def cluster_intent(cluster: Cluster) -> float:
    """Max intent weight across the cluster's members."""
    return max((intent_weight(m) for m in cluster.members), default=0.3)


def retrieve_product(
    cluster: Cluster,
    products: list[ProductState],
) -> tuple[Optional[str], float]:
    """Match a cluster to a product by centroid cosine. Returns (product_id, score)."""
    best_id, best = None, 0.0
    for p in products:
        if not p.embedding:
            continue
        s = cosine(cluster.centroid, p.embedding)
        if s > best:
            best_id, best = p.product_id, s
    return best_id, best


def phase_relevance(
    cluster: Cluster,
    state: StreamState,
) -> float:
    """How relevant is this cluster to the CURRENT phase?

    OPENING: greeting/engagement intent scores high (no product yet).
    SELLING: clusters retrieving the CURRENT product score high; other
             products mid (allow going back); off-topic low.
    CLOSING: thanks/follow intent high.
    """
    if state.phase == Phase.OPENING:
        # reward low-intent social/greeting clusters during opening
        return 1.0 - cluster_intent(cluster) * 0.3 + 0.3
    if state.phase == Phase.CLOSING:
        return 0.6
    # SELLING: use retrieval vs current product
    cur = state.current_product()
    if cur is None:
        return 0.3
    if cluster.product_id == cur.product_id:
        return 1.0
    # cluster about a different (already-known) product -> allow revisit, mid score
    if cluster.product_id is not None:
        return 0.5
    return 0.3


@dataclass
class ScoredCluster:
    cluster: Cluster
    score: float
    phase_rel: float
    intent: float


def score_cluster(
    cluster: Cluster,
    state: StreamState,
    cfg: StreamConfig,
    now: float,
    max_size: int,
) -> ScoredCluster:
    pr = phase_relevance(cluster, state)
    intent = cluster_intent(cluster)
    size_norm = (cluster.size / max_size) if max_size > 0 else 0.0
    age = max(0.0, now - cluster.newest_t)
    recency = math.pow(0.5, age / max(cfg.recency_half_life_sec, 1e-6))

    score = (
        cfg.w_phase_relevance * pr
        + cfg.w_intent * intent
        + cfg.w_cluster_size * size_norm
        + cfg.w_recency * recency
    )
    return ScoredCluster(cluster=cluster, score=score, phase_rel=pr, intent=intent)


def rank_clusters(
    clusters: list[Cluster],
    state: StreamState,
    cfg: StreamConfig,
    now: float,
) -> list[ScoredCluster]:
    """Retrieve products, score, drop stale/over-skipped, return high->low."""
    # retrieval first (fills product_id for phase_relevance)
    for cl in clusters:
        pid, rscore = retrieve_product(cl, state.products)
        cl.product_id, cl.retrieval_score = pid, rscore

    max_size = max((c.size for c in clusters), default=1)
    scored = []
    for cl in clusters:
        age = now - cl.newest_t
        if age > cfg.cluster_max_age_sec or cl.skips > cfg.cluster_max_skips:
            continue  # evict
        scored.append(score_cluster(cl, state, cfg, now, max_size))
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored
