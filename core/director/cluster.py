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

from .embedder import average, cosine


@dataclass
class Comment:
    """One viewer message with its embedding and arrival time."""

    text: str
    embedding: list[float]
    t: float  # seconds (injected clock — deterministic for tests)


@dataclass
class Cluster:
    """A group of semantically similar comments."""

    centroid: list[float]
    members: list[str] = field(default_factory=list)
    embeddings: list[list[float]] = field(default_factory=list)
    newest_t: float = 0.0
    skips: int = 0                       # times this cluster was skipped (eviction)
    product_id: Optional[str] = None     # filled by retrieval
    retrieval_score: float = 0.0         # cosine to the matched product

    @property
    def size(self) -> int:
        return len(self.members)

    def add(self, c: Comment) -> None:
        self.members.append(c.text)
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
            sim = cosine(c.embedding, cl.centroid)
            if sim >= best_sim:
                best, best_sim = cl, sim
        if best is None:
            clusters.append(
                Cluster(centroid=list(c.embedding), members=[c.text],
                        embeddings=[list(c.embedding)], newest_t=c.t)
            )
        else:
            best.add(c)
    return clusters
