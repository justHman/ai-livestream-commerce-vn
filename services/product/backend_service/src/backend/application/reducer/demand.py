"""Unique-viewer-aware demand ranking and pivot demand (OpenSpec 6.6-6.8, 6.10).

Pure functions over ``LiveCluster`` state from ``ClusterStore``: no asyncio,
no store mutation, no director imports — this module is the C7 seam that
produces ranked active demand with score breakdowns for the Director.

Demand score (Decision 7) mixes unique-viewer demand (primary), message count
(secondary), intent actionability, recency, current-script product relevance,
product-resolution confidence, and novelty. Saturation functions are
sublinear: repeated messages from one viewer (20 x 1) never linearly emulate
independent demand (6 x 6) — with the default weights below a 6-viewer cluster
outranks a 20-message/1-viewer cluster, and the 6.7 test asserts exactly that.

Pivot share (6.8) also runs on UNIQUE-VIEWER shares of resolved products, not
raw message counts, so one viewer flooding product B comments never enters a
pivot by itself. Cooldown identity (6.10) is a stable semantic cluster
fingerprint over member ids + resolved products + intent — not ``product:intent``.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Optional

from .cluster_store import LiveCluster

__all__ = [
    "DemandConfig",
    "DemandScore",
    "DemandWeights",
    "cluster_fingerprint",
    "product_demand",
    "score_clusters",
    "should_pivot",
]

# Intent -> actionability weight (6.6). Spam/off_topic/social are deliberately
# excluded: they never reach the store via routing, and if they ever did, they
# are NOT eligible here — they must never inflate actionable demand.
_INTENT_ACTIONABILITY: dict[str, float] = {
    "price": 1.0,
    "stock": 1.0,
    "buy_intent": 1.0,
    "comparison": 0.9,
    "complaint": 0.8,
    "usage": 0.7,
    "unknown": 0.3,
}
# Completely non-actionable intents are excluded from ranking outright.
_NON_ACTIONABLE_INTENTS = frozenset({"spam", "off_topic", "social"})


@dataclass(frozen=True)
class DemandWeights:
    """Linear weights of the Decision 7 demand formula.

    ``w_unique_viewers`` dominates: independent viewers are the primary
    popularity signal. Message demand is a secondary nudge, recency keeps
    fresh topics ahead of stale ones, and the smaller terms tie-break.
    Sum = 1.0; tuned so 6 independent viewers outrank 20 repeats from one
    viewer (test_anti_inflation asserts the exact numbers).
    """

    w_unique_viewers: float = 0.35
    w_message_demand: float = 0.15
    w_intent: float = 0.15
    w_recency: float = 0.15
    w_script_relevance: float = 0.10
    w_product_confidence: float = 0.05
    w_novelty: float = 0.05


@dataclass(frozen=True)
class DemandConfig:
    """Typed knobs of the demand scorer.

    Saturation denominators sit above the typical demand scale so scores stay
    meaningful: 6 viewers reach 0.375 of the unique-viewer term while 1 viewer
    reaches 0.143 — a 1-viewer flood cannot reach the same unique-viewer term
    no matter how many messages it repeats (sublinear by design).
    """

    weights: DemandWeights = field(default_factory=DemandWeights)
    recency_half_life_sec: float = 30.0
    unique_viewer_saturation: float = 10.0
    message_saturation: float = 25.0
    intent_weights: dict[str, float] = field(default_factory=lambda: dict(_INTENT_ACTIONABILITY))

    def validate_runtime(self) -> None:
        """Fail-fast on non-positive knobs (mirrors the store's config style)."""
        if self.recency_half_life_sec <= 0:
            raise ValueError("recency_half_life_sec must be > 0")
        if self.unique_viewer_saturation <= 0:
            raise ValueError("unique_viewer_saturation must be > 0")
        if self.message_saturation <= 0:
            raise ValueError("message_saturation must be > 0")
        if any(w < 0 for w in asdict(self.weights).values()):
            raise ValueError("demand weights must be >= 0")


@dataclass(frozen=True)
class DemandScore:
    """One cluster's ranked demand with a typed score breakdown."""

    cluster_id: str
    score: float
    unique_viewer_score: float
    message_score: float
    intent_score: float
    recency_score: float
    script_relevance_score: float
    product_confidence_score: float
    novelty_score: float

    def breakdown(self) -> dict[str, float]:
        """The 6.9/D-9 envelope score breakdown (computed once, readable)."""
        return {
            "score": self.score,
            "unique_viewer_score": self.unique_viewer_score,
            "message_score": self.message_score,
            "intent_score": self.intent_score,
            "recency_score": self.recency_score,
            "script_relevance_score": self.script_relevance_score,
            "product_confidence_score": self.product_confidence_score,
            "novelty_score": self.novelty_score,
        }


def cluster_fingerprint(cluster: LiveCluster, *, member_texts_limit: int = 8) -> str:
    """Stable semantic identity: SHA256 over member ids + products + intent.

    Comment ids are stable for the lifetime of a cluster, so the fingerprint
    survives fast-lane updates (same topic keeps the same fingerprint), while
    a semantically distinct question on the same product+intent has different
    members and thus a different fingerprint. When representatives are empty
    (fresh cluster), up to ``member_texts_limit`` member texts stand in so the
    identity never depends on arrival-order ids alone.
    """
    ids = sorted(cluster.representative_comment_ids) or cluster.member_ids[:member_texts_limit]
    parts = [*sorted(cluster.resolved_product_ids), cluster.intent]
    parts.extend(sorted(ids))
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def _saturate(value: float, saturation: float) -> float:
    """Sublinear saturation ``v / (v + saturation)`` — bounded [0, 1)."""
    if value <= 0:
        return 0.0
    return value / (value + saturation)


def score_clusters(
    clusters: list[LiveCluster],
    current_product_id: Optional[str],
    now: float,
    cfg: DemandConfig,
) -> list[DemandScore]:
    """Rank active clusters by demand (Decision 7), deterministic sort.

    Non-actionable intents are excluded outright (spec: spam/off-topic SHALL
    not inflate actionable demand). Sort: score desc, then cluster_id asc.
    """
    cfg.validate_runtime()
    weights = cfg.weights
    ranked: list[DemandScore] = []
    for cluster in clusters:
        if cluster.intent in _NON_ACTIONABLE_INTENTS:
            continue
        unique = _saturate(float(cluster.unique_viewer_count), cfg.unique_viewer_saturation)
        messages = _saturate(float(cluster.message_count), cfg.message_saturation)
        intent = cfg.intent_weights.get(cluster.intent, 0.3)
        recency = pow(0.5, max(0.0, now - cluster.newest_t) / cfg.recency_half_life_sec)
        script_relevant = 1.0 if current_product_id in cluster.resolved_product_ids else 0.0
        novelty = 0.5 if cluster.novelty_fingerprint else 0.0
        score = (
            weights.w_unique_viewers * unique
            + weights.w_message_demand * messages
            + weights.w_intent * intent
            + weights.w_recency * recency
            + weights.w_script_relevance * script_relevant
            + weights.w_product_confidence * cluster.product_resolution_confidence
            + weights.w_novelty * novelty
        )
        ranked.append(
            DemandScore(
                cluster_id=cluster.cluster_id,
                score=score,
                unique_viewer_score=unique,
                message_score=messages,
                intent_score=intent,
                recency_score=recency,
                script_relevance_score=script_relevant,
                product_confidence_score=cluster.product_resolution_confidence,
                novelty_score=novelty,
            )
        )
    ranked.sort(key=lambda d: (-d.score, d.cluster_id))
    return ranked


def product_demand(clusters: list[LiveCluster], now: float) -> dict[str, dict]:
    """Unique-viewer-aware demand per resolved product (6.8).

    Only clusters with a member inside the horizon contribute — the caller
    passes ``active_clusters(now)``, so member counts are already
    horizon-bound. Returns ``{product_id: {"unique_viewers": int, "messages": int}}``
    keyed by resolved product ids.
    """
    demand: dict[str, dict] = {}
    for cluster in clusters:
        if cluster.intent in _NON_ACTIONABLE_INTENTS:
            continue
        for product_id in cluster.resolved_product_ids:
            entry = demand.setdefault(product_id, {"unique_viewers": 0, "messages": 0})
            entry["unique_viewers"] += cluster.unique_viewer_count
            entry["messages"] += cluster.message_count
    return demand


def should_pivot(
    products: dict[str, dict],
    target: str,
    current: str,
    *,
    min_unique_viewers: int = 3,
    enter_unique_share: float = 0.6,
    exit_unique_share: float = 0.45,
) -> Optional[str]:
    """Pivot hysteresis on UNIQUE-VIEWER shares (6.8, spec "Pivot hysteresis").

    Enter: target has >= ``min_unique_viewers`` AND >= ``enter_unique_share``
    of all resolved-product unique viewers. Exit: ``current`` drops below
    ``exit_unique_share``. Returns "enter" | "exit" | None.
    """
    total = sum(e["unique_viewers"] for e in products.values())
    if total <= 0:
        return None
    target_share = products.get(target, {}).get("unique_viewers", 0) / total
    if (
        target_share >= enter_unique_share
        and products.get(target, {}).get("unique_viewers", 0) >= min_unique_viewers
    ):
        return "enter"
    current_share = products.get(current, {}).get("unique_viewers", 0) / total
    if current_share < exit_unique_share:
        return "exit"
    return None
