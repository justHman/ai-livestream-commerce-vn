"""Deterministic live-comment stream fixtures for the reducer (OpenSpec 5.8).

Two fixture families for proving the stable-cluster-identity and
bounded-memory contracts, both pure stdlib (no backend imports, no
network, no wall clock):

- ``TopicStream``: a synthetic AcceptedComment-like stream over a
  configurable duration and message rate. Topic vectors are fixed per
  topic (the fake embedder maps topic -> fixed vector), so comments on
  the same topic cluster together and the same topic recurring across a
  long stream must keep ONE stable cluster_id.
- ``microcluster_scenario``: a borderline-topic pair (A at 0 deg, B
  spread 60-75 deg) whose vectors are close enough that greedy
  incremental assignment splits them into two microclusters when
  interleaved, yet a reconciliation merges them back deterministically
  without duplicating member identities. The same members grouped by
  topic collapse into one cluster from the start.

Public surface: ``StreamComment``, ``TopicEmbedder``, ``TopicStream``,
``stream_batches``, ``topic_vectors``, ``microcluster_scenario``,
``scenario_batches``, ``scenario_vectors``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "StreamComment",
    "TopicEmbedder",
    "TopicStream",
    "stream_batches",
    "topic_vectors",
    "microcluster_scenario",
    "scenario_batches",
    "scenario_vectors",
]

DIM = 24
# 0.3 side-load keeps cross-topic cosine ~0.275, well below the default
# 0.375 merge threshold: distinct topics never collapse at assign time.
_OFF_DIAGONAL = 0.3


def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def topic_vectors(count: int, dim: int = DIM) -> list[list[float]]:
    """Fixed L2-normalized vectors for ``count`` distinct semantic topics."""
    return [
        _l2(
            [
                _OFF_DIAGONAL if i == (topic + 1) % dim else 0.0 if i != topic else 1.0
                for i in range(dim)
            ]
        )
        for topic in range(count)
    ]


@dataclass(frozen=True)
class StreamComment:
    """One AcceptedComment-like item produced by a stream fixture."""

    event_id: str
    comment_id: str
    text: str
    ts: float
    viewer_key: str


class TopicEmbedder:
    """Deterministic embedder: topic -> fixed vector.

    Text must encode ``"<topic>|..."`` (the ``TopicStream`` generator
    form); ``topic_of`` decodes it. Every returned vector is
    L2-normalized.
    """

    def __init__(self, count: int, dim: int = DIM) -> None:
        self._topics = topic_vectors(count, dim)

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._topics[self.topic_of(t)] for t in texts]

    @staticmethod
    def topic_of(text: str) -> int:
        return int(text.split("|", 1)[0])


class TopicStream:
    """Deterministic interleaved-topic stream generator (no wall clock).

    ``topics`` base comments arrive at ``rate`` comments/second in batches
    of ``per_batch``; the first topic recurs with a burst of
    ``recurring_burst`` comments every ``recurring_every_sec`` so a
    stable-ID proof can observe the SAME topic keep ONE cluster_id across
    the whole stream. All batches are materialized eagerly, so a caller
    can re-run the same batch list against fresh reducer state.
    """

    def __init__(
        self,
        duration_sec: float,
        rate: float,
        per_batch: int = 10,
        topics: int = 4,
        recurring_burst: int = 3,
        recurring_every_sec: float = 900.0,
        start_ts: float = 1000.0,
        seed_offset: int = 0,
    ) -> None:
        if topics < 2:
            raise ValueError("topics must be >= 2")
        if rate <= 0.0 or per_batch <= 0:
            raise ValueError("rate and per_batch must be > 0")
        self.topics = topics
        self.recurring_topic = 0
        self.recurring_burst = recurring_burst
        self.recurring_every_sec = recurring_every_sec
        step = per_batch / rate
        batch_count = int(duration_sec // step)
        self.batches: list[list[StreamComment]] = []
        next_recurring = start_ts + recurring_every_sec
        index = 0
        for batch_index in range(batch_count):
            ts = start_ts + (batch_index + 1) * step
            batch: list[StreamComment] = []
            for i in range(per_batch):
                topic = (index + i) % topics
                text = f"{topic}|{seed_offset + index + i}"
                batch.append(
                    StreamComment(
                        event_id=text,
                        comment_id=text,
                        text=text,
                        ts=ts,
                        viewer_key=f"v{(index + i) % 15}",
                    )
                )
            index += per_batch
            if ts >= next_recurring:
                for i in range(recurring_burst):
                    text = f"{self.recurring_topic}|r-{ts}-{i}"
                    batch.append(
                        StreamComment(
                            event_id=text,
                            comment_id=text,
                            text=text,
                            ts=ts,
                            viewer_key=f"v{(index + i) % 15}",
                        )
                    )
                next_recurring += recurring_every_sec
            self.batches.append(batch)

    @property
    def comment_count(self) -> int:
        return sum(len(batch) for batch in self.batches)


def stream_batches(stream: TopicStream) -> list[list[StreamComment]]:
    """Return the fixture's batches (already materialized)."""
    return stream.batches


def microcluster_scenario() -> dict:
    """Deterministic arrival-order scenario: 2 microclusters -> 1 merge.

    Vectors: topic A at 0 deg, topic B spread across 60/70/75 deg on the
    dims-0-1 plane. A comment 68+ deg from a pure-A centroid is below the
    0.375 merge threshold, so interleaving A and B members alternates two
    microclusters, while the same members grouped by topic collapse into
    one cluster from the first assignment (B members join A). Reconcile
    then merges the two interleaved microclusters deterministically:
    B's centroid (spread 60-75 deg) is within merge distance of A's
    centroid, the survivor is the higher-message-count cluster, and member
    identities are never duplicated.
    """
    return {
        "interleaved_degrees": [0, 70, 60, 75, 0, 60],
        "grouped_degrees": [0, 0, 60, 60, 70, 75],
        "reconcile_at": 1006.0,
    }


def _angle(deg: float, dim: int = DIM) -> list[float]:
    """Unit vector at ``deg`` on the dims-0-1 plane."""
    r = math.radians(deg)
    vec = [0.0] * dim
    vec[0] = math.cos(r)
    vec[1] = math.sin(r)
    return vec


def scenario_vectors(scenario: dict, dim: int = DIM) -> tuple[list[list[float]], list[list[float]]]:
    """The interleaved and grouped vector sequences of the scenario."""
    return (
        [_angle(deg, dim) for deg in scenario["interleaved_degrees"]],
        [_angle(deg, dim) for deg in scenario["grouped_degrees"]],
    )


def scenario_batches(scenario: dict) -> tuple[list[StreamComment], list[StreamComment]]:
    """Materialize the microcluster scenario as two comment sequences.

    Returns ``(interleaved, grouped)``, each a list of six comments with
    distinct ids and monotonic timestamps.
    """
    interleaved = [
        StreamComment(
            event_id=f"i{i}",
            comment_id=f"i{i}",
            text=f"i{i}",
            ts=1000.0 + i,
            viewer_key="v1",
        )
        for i in range(len(scenario["interleaved_degrees"]))
    ]
    grouped = [
        StreamComment(
            event_id=f"g{i}",
            comment_id=f"g{i}",
            text=f"g{i}",
            ts=1000.0 + i,
            viewer_key="v1",
        )
        for i in range(len(scenario["grouped_degrees"]))
    ]
    return interleaved, grouped
