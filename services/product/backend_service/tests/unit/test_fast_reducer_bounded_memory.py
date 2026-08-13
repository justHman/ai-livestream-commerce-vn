"""Bounded-memory + stable-ID proof for the reducer (OpenSpec 5.9).

Deterministic 6-hour synthetic livestream on the fake clock: sustained
traffic with 20s batches and the first topic recurring every 15 minutes.
After every fast-lane run the test reconciles when due and runs once per
microbatch, exactly like the real loop.

Invariant asserted throughout and at the end: the store's LIVE member
count (sum of member_ids across clusters) never exceeds a fixed bound
derived from the configuration (horizon 75s x 0.5/s = ~38 live members +
reconciliation slack), while ``embedded_total`` grows linearly with the
stream (throughput proof). Memory therefore does NOT track history.
Stable-ID and end-to-end liveness are asserted across the whole stream.
"""

from __future__ import annotations

import pytest

from backend.application.reducer import AcceptedComment, FastReducer, FastReducerConfig

from .benchmark_fixtures.reducer_stream import StreamComment, TopicEmbedder, TopicStream

SIX_HOURS_SEC = 6 * 3600.0
COMMENT_RATE = 0.5
BATCH_SEC = 20.0
TOPIC_COUNT = 6
RECURRING_TOPIC = 0
# Horizon (75s) at 0.5/s = ~38 live members; cap leaves wide slack for
# reconciliation batches and sits far below the hard configured cap
# (max_active_clusters * max_members_per_cluster = 8000).
LIVE_MEMBER_CAP = 150
HOURLY_EPSILON = 150
HOURS = 6
BATCHES_PER_HOUR = 180  # 3600s / 20s batches


class _Clock:
    """Mutable fake clock: advancing it moves the reducer's view of time."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def now(self) -> float:
        return self.value


def _comment(item: StreamComment) -> AcceptedComment:
    return AcceptedComment(
        event_id=item.event_id,
        comment_id=item.comment_id,
        text=item.text,
        ts=item.ts,
        viewer_key=item.viewer_key,
    )


def _make_reducer() -> tuple[FastReducer, _Clock, TopicEmbedder]:
    clock = _Clock()
    embedder = TopicEmbedder(TOPIC_COUNT + 1)  # +1 for the hour-6 fresh topic
    reducer = FastReducer(
        config=FastReducerConfig(rolling_horizon_sec=75.0),
        embedder=embedder,
        now_fn=clock.now,
    )
    return reducer, clock, embedder


def _make_stream() -> TopicStream:
    return TopicStream(
        duration_sec=SIX_HOURS_SEC,
        rate=COMMENT_RATE,
        per_batch=int(COMMENT_RATE * BATCH_SEC),
        topics=TOPIC_COUNT,
    )


async def _feed(
    reducer: FastReducer, clock: _Clock, stream: TopicStream
) -> tuple[list[int], set[str]]:
    """Feed the whole stream on the fake clock.

    Each batch advances the clock by BATCH_SEC, runs the fast lane, and
    reconciles when due — the exact loop the runtime runs. Returns the
    live member count after every batch plus the set of cluster ids that
    ever received a recurring-topic comment.
    """
    live_counts = []
    topic_cluster_ids: set[str] = set()
    for batch in stream.batches:
        clock.value += BATCH_SEC
        for item in batch:
            reducer.notify_new_events("s1", _comment(item))
        await reducer.run_once("s1", clock.now())
        if reducer.reconciliation_due("s1", clock.now()):
            await reducer.reconcile("s1", clock.now())
        store = reducer._get_store("s1")
        live_counts.append(store.stats()["member_ids_count"])
        for item in batch:
            if TopicEmbedder.topic_of(item.text) != RECURRING_TOPIC:
                continue
            for cluster in store._clusters.values():
                if item.comment_id in cluster.member_ids:
                    topic_cluster_ids.add(cluster.cluster_id)
    return live_counts, topic_cluster_ids


@pytest.mark.asyncio
async def test_six_hour_livestream_memory_stays_bounded_and_flat() -> None:
    stream = _make_stream()
    reducer, clock, _ = _make_reducer()

    live_counts, _ = await _feed(reducer, clock, stream)

    store = reducer._get_store("s1")
    assert max(live_counts) <= LIVE_MEMBER_CAP
    assert store.stats()["member_ids_count"] <= LIVE_MEMBER_CAP
    assert reducer.stats("s1")["embedded_total"] == stream.comment_count
    assert reducer.stats("s1")["reconciles_run"] >= 3


@pytest.mark.asyncio
async def test_hourly_live_memory_does_not_track_history() -> None:
    stream = _make_stream()
    reducer, clock, _ = _make_reducer()

    live_counts, _ = await _feed(reducer, clock, stream)

    first_hour = live_counts[BATCHES_PER_HOUR - 1]
    assert len(live_counts) == BATCHES_PER_HOUR * HOURS
    assert max(live_counts) <= LIVE_MEMBER_CAP
    assert live_counts[-1] <= first_hour + HOURLY_EPSILON
    # The stream processed 10k+ comments while live state stayed ~flat.
    assert reducer.stats("s1")["embedded_total"] > 10_000


@pytest.mark.asyncio
async def test_recurring_topic_keeps_one_stable_cluster_id_across_six_hours() -> None:
    stream = _make_stream()
    reducer, clock, _ = _make_reducer()

    _, topic_cluster_ids = await _feed(reducer, clock, stream)

    assert len(topic_cluster_ids) == 1, (
        f"recurring topic split into clusters across the stream: {topic_cluster_ids}"
    )
    assert reducer.stats("s1")["reconciles_run"] >= 3  # stability survived reconciliations


@pytest.mark.asyncio
async def test_fresh_topic_at_hour_six_creates_new_active_cluster() -> None:
    stream = _make_stream()
    reducer, clock, _ = _make_reducer()

    live_counts, _ = await _feed(reducer, clock, stream)
    assert max(live_counts) <= LIVE_MEMBER_CAP

    # A fresh comment on a brand-new topic at the end of the stream still
    # creates a new active cluster — the store keeps operating end-to-end.
    clock.value += 1.0
    text = "6|fresh-late"
    reducer.notify_new_events(
        "s1",
        AcceptedComment(
            event_id=text,
            comment_id=text,
            text=text,
            ts=clock.now(),
            viewer_key="v9",
        ),
    )
    await reducer.run_once("s1", clock.now())
    store = reducer._get_store("s1")
    new_cluster = next(
        cluster for cluster in store._clusters.values() if text in cluster.member_ids
    )
    assert new_cluster is not None
    assert new_cluster.member_ids == [text]
    assert any(c.cluster_id == new_cluster.cluster_id for c in store.active_clusters(clock.now()))
