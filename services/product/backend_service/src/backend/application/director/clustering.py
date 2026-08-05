"""Online clustering of viewer comments (challenge 5, step 2-3).

Greedy threshold clustering (no fixed K, no K-means): each new comment joins
the nearest existing cluster if cosine(centroid) >= merge_threshold, else
starts a new cluster. Cheap (vector ops on small dim) and incremental, which
fits a live stream where comments arrive continuously.

Each Cluster keeps a centroid (L2-normalized mean), member texts, size, the
time of its newest message (for recency), and bookkeeping for scoring/eviction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional
from uuid import uuid4

from .embeddings import average, cosine


@dataclass
class Comment:
    """One viewer message with stable identity and routing metadata."""

    text: str
    embedding: list[float]
    t: float  # seconds (injected clock — deterministic for tests)
    id: str = field(default_factory=lambda: uuid4().hex)
    category: str = "commerce"
    intent: str = "unknown"
    product_id: Optional[str] = None
    actionable: bool = True


@dataclass
class Cluster:
    """A group of semantically similar comments."""

    centroid: list[float]
    members: list[str] = field(default_factory=list)
    member_ids: list[str] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    newest_t: float = 0.0
    skips: int = 0  # times this cluster was skipped (eviction)
    product_id: Optional[str] = None  # routed or filled by retrieval
    category: str = "commerce"
    intent: str = "unknown"
    actionable: bool = True
    retrieval_score: float = 0.0  # cosine to the matched product

    @property
    def size(self) -> int:
        return len(self.members)

    def add(self, c: Comment) -> None:
        self.members.append(c.text)
        self.member_ids.append(c.id)
        self.embeddings.append(c.embedding)
        self.centroid = average(self.embeddings)
        self.newest_t = max(self.newest_t, c.t)


def cluster_comments(
    comments: list[Comment],
    merge_threshold: float = 0.55,
) -> list[Cluster]:
    """Greedy online clustering. Returns clusters newest-message-aware."""
    clusters: list[Cluster] = []
    for c in comments:
        best: Optional[Cluster] = None
        best_sim = merge_threshold
        for cl in clusters:
            if (
                cl.category,
                cl.product_id,
                cl.intent,
                cl.actionable,
            ) != (
                c.category,
                c.product_id,
                c.intent,
                c.actionable,
            ):
                continue
            sim = cosine(c.embedding, cl.centroid)
            if sim >= best_sim:
                best, best_sim = cl, sim
        if best is None:
            clusters.append(
                Cluster(
                    centroid=list(c.embedding),
                    members=[c.text],
                    member_ids=[c.id],
                    embeddings=[list(c.embedding)],
                    newest_t=c.t,
                    product_id=c.product_id,
                    category=c.category,
                    intent=c.intent,
                    actionable=c.actionable,
                )
            )
        else:
            best.add(c)
    return clusters
