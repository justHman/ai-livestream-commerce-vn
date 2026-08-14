"""Contract tests for the deterministic reducer stream fixtures (OpenSpec 5.8).

Proves the fixtures are deterministic and produce the behaviors the
bounded-memory and reconciliation scenarios depend on: a fixed topic
keeps ONE stable cluster_id across fast-lane runs and reconciliations,
and interleaved arrival of a borderline topic pair creates two
microclusters that reconcile merges back deterministically.
"""

from __future__ import annotations

import math

from backend.application.reducer import ClusterStore, ClusterStoreConfig

from .benchmark_fixtures.reducer_stream import (
    StreamComment,
    TopicEmbedder,
    TopicStream,
    microcluster_scenario,
    scenario_batches,
    scenario_vectors,
    stream_batches,
    topic_vectors,
)

DIM = 24
MERGE_THRESHOLD = 0.375
HORIZON = 75.0


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _topic_store() -> ClusterStore:
    return ClusterStore(
        "s1",
        config=ClusterStoreConfig(merge_threshold=MERGE_THRESHOLD, rolling_horizon_sec=HORIZON),
    )


def _scenario_store(comments: list[StreamComment], vectors: list[list[float]]) -> ClusterStore:
    store = _topic_store()
    for item, vector in zip(comments, vectors):
        store.assign(
            comment_id=item.comment_id,
            text=item.text,
            vector=vector,
            ts=item.ts,
            viewer_key=item.viewer_key,
        )
    return store


def test_topic_vectors_are_fixed_normalized_and_separated() -> None:
    vectors = topic_vectors(4)

    assert len(vectors) == 4
    for vector in vectors:
        assert abs(sum(x * x for x in vector) - 1.0) < 1e-9
    assert vectors == topic_vectors(4)  # deterministic across calls
    # Cross-topic cosine ~0.275, well below the 0.375 merge threshold, so
    # distinct topics never collapse into one cluster at assign time.
    assert all(
        _cosine(vectors[i], vectors[j]) < MERGE_THRESHOLD
        for i in range(4)
        for j in range(4)
        if i != j
    )


def test_topic_embedder_maps_text_topic_to_fixed_vector() -> None:
    embedder = TopicEmbedder(4)

    first = embedder.encode(["0|a", "1|b", "2|c"])
    second = embedder.encode(["0|a", "1|b", "2|c"])

    assert first == second
    assert _cosine(first[0], first[0]) > 0.99
    assert _cosine(first[0], first[1]) < MERGE_THRESHOLD


def test_stream_generation_is_deterministic_and_sized() -> None:
    stream = TopicStream(duration_sec=900.0, rate=2.0, per_batch=30, topics=4)

    batches = stream_batches(stream)
    assert len(batches) == 60  # 900s / 15s batches
    # One recurring-topic burst lands in the last batch (3 extra comments).
    assert all(len(batch) in (30, 33) for batch in batches)
    assert sum(len(batch) for batch in batches) == stream.comment_count
    # Every comment has a distinct id and a monotonic timestamp.
    ids = [item.comment_id for batch in batches for item in batch]
    assert len(set(ids)) == len(ids)
    timestamps = [item.ts for batch in batches for item in batch]
    assert timestamps == sorted(timestamps)
    # Deterministic across independent generations.
    assert (
        stream_batches(TopicStream(duration_sec=900.0, rate=2.0, per_batch=30, topics=4)) == batches
    )


def test_stream_recurring_topic_keeps_one_stable_cluster_id() -> None:
    stream = TopicStream(duration_sec=600.0, rate=2.0, per_batch=30, topics=4)
    embedder = TopicEmbedder(4)
    store = _topic_store()
    seen_ids = set()
    now = 1000.0
    for batch in stream_batches(stream):
        now += 15.0
        for item in batch:
            cid = store.assign(
                comment_id=item.comment_id,
                text=item.text,
                vector=embedder.encode([item.text])[0],
                ts=item.ts,
                viewer_key=item.viewer_key,
            )
            if TopicEmbedder.topic_of(item.text) == 0:
                seen_ids.add(cid)
        if store.reconcile_due(now):
            store.reconcile(now)

    assert len(seen_ids) == 1, f"recurring topic split into clusters: {seen_ids}"
    cluster = store.get_cluster(next(iter(seen_ids)))
    assert cluster is not None
    assert cluster.message_count > 0


def test_microcluster_scenario_interleaved_vs_grouped() -> None:
    scenario = microcluster_scenario()
    interleaved, grouped = scenario_batches(scenario)
    interleaved_vectors, grouped_vectors = scenario_vectors(scenario)
    interleaved_store = _scenario_store(interleaved, interleaved_vectors)
    grouped_store = _scenario_store(grouped, grouped_vectors)

    assert len(interleaved_store._clusters) == 2
    assert len(grouped_store._clusters) == 1
    # Both stores hold the same six member identities.
    interleaved_members = sorted(
        m for c in interleaved_store._clusters.values() for m in c.member_ids
    )
    grouped_members = sorted(m for c in grouped_store._clusters.values() for m in c.member_ids)
    assert interleaved_members == ["i0", "i1", "i2", "i3", "i4", "i5"]
    assert grouped_members == ["g0", "g1", "g2", "g3", "g4", "g5"]


def test_microcluster_scenario_reconcile_merges_deterministically() -> None:
    scenario = microcluster_scenario()
    interleaved, _ = scenario_batches(scenario)
    interleaved_vectors, _ = scenario_vectors(scenario)
    store = _scenario_store(interleaved, interleaved_vectors)
    assert len(store._clusters) == 2

    result = store.reconcile(scenario["reconcile_at"])

    assert result.merged == 1
    active = store.active_clusters(scenario["reconcile_at"])
    assert len(active) == 1
    survivor = active[0]
    assert sorted(survivor.member_ids) == ["i0", "i1", "i2", "i3", "i4", "i5"]
    assert survivor.message_count == 6
    assert len(survivor.member_ids) == len(set(survivor.member_ids))  # no dupes
