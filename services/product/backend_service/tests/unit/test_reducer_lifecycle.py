"""Representative selection and lifecycle persistence on stable clusters (6.9, 6.11).

The 6.9 representative algorithm (medoid + greedy max-min-similarity diversity)
already landed with C5; these tests lock its spec behavior: medoid inclusion,
bounding, subset of members, and recompute after eviction of the medoid. The
6.11 tests prove skip/selection/answer state survives fast-lane updates — a
new comment on the same topic keeps the same cluster_id AND its lifecycle.
"""

from __future__ import annotations

import math

from backend.application.reducer import ClusterStore, ClusterStoreConfig


def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _vec(dim: int, basis: int) -> list[float]:
    """Basis vector ``basis`` of ``dim`` plus tiny deterministic noise."""
    return _l2([1.0 if i == basis else 0.001 * i for i in range(dim)])


def _make_store(max_reps: int = 5, horizon: float = 75.0) -> tuple[ClusterStore, list[float]]:
    now = [1000.0]
    store = ClusterStore(
        "s1",
        config=ClusterStoreConfig(
            merge_threshold=0.5,
            max_representatives=max_reps,
            rolling_horizon_sec=horizon,
        ),
        now_fn=lambda: now[0],
    )
    return store, now


def _assign(
    store: ClusterStore,
    comment_id: str,
    basis: int,
    ts: float,
    viewer_key: str = "v1",
    intent: str = "price",
) -> str:
    return store.assign(
        comment_id=comment_id,
        text=f"text {comment_id}",
        vector=_vec(8, basis),
        ts=ts,
        viewer_key=viewer_key,
        intent=intent,
    )


# ---------------------------------------------------------------------------
# 6.9 — semantic representatives (medoid + bounded diversity)
# ---------------------------------------------------------------------------


def test_representatives_include_medoid_and_are_bounded_subset() -> None:
    store, now = _make_store(max_reps=5)
    ids = [_assign(store, f"c{i}", 0, now[0] + float(i)) for i in range(12)]
    cluster = store.get_cluster(ids[0])
    assert cluster is not None

    assert cluster.medoid_comment_id in cluster.member_ids
    assert cluster.representative_comment_ids[0] == cluster.medoid_comment_id
    assert set(cluster.representative_comment_ids) <= set(cluster.member_ids)
    assert len(cluster.representative_comment_ids) <= 5


def test_representatives_bounded_by_max_representatives() -> None:
    store, now = _make_store(max_reps=3)
    ids = [_assign(store, f"c{i}", i % 3, now[0] + float(i)) for i in range(20)]
    cluster = store.get_cluster(ids[0])
    assert cluster is not None

    assert len(cluster.representative_comment_ids) == 3


def test_near_duplicate_cluster_selects_medoid_plus_diversity() -> None:
    store, now = _make_store(max_reps=5)
    # Many near-duplicate paraphrases: same topic basis with growing jitter,
    # so the medoid is the member closest to the centroid and the diversity
    # picks are the members farthest from it.
    ids = [_assign(store, f"dup{i}", 0, now[0] + float(i)) for i in range(10)]
    cluster = store.get_cluster(ids[0])
    assert cluster is not None
    medoid = cluster.medoid_comment_id
    assert medoid is not None
    medoid_vec = cluster._member_embeddings[medoid]
    farthest = max(
        (cid for cid in cluster.member_ids if cid != medoid),
        key=lambda cid: sum(a * b for a, b in zip(cluster._member_embeddings[cid], medoid_vec)),
    )

    # Agent context never needs the whole member list.
    assert len(cluster.representative_comment_ids) < len(cluster.member_ids)
    assert cluster.representative_comment_ids[0] == medoid
    # Diversity is real: the member least similar to the medoid is picked.
    assert farthest in cluster.representative_comment_ids


def test_representatives_refresh_after_medoid_eviction() -> None:
    store, now = _make_store(max_reps=5, horizon=10.0)
    for i in range(5):
        _assign(store, f"c{i}", 0, now[0] + float(i) * 0.1)
    cluster = next(iter(store._clusters.values()))
    medoid = cluster.medoid_comment_id
    assert medoid is not None
    assert medoid.startswith("c")  # centroid tilts to the 5-member topic side

    # Age the old topic members out of the horizon; the medoid is among them.
    now[0] += 11.0
    _assign(store, "fresh", 1, now[0])
    store.expire(now[0])

    cluster = next(iter(store._clusters.values()))
    assert medoid not in cluster.member_ids
    assert cluster.medoid_comment_id == "fresh"  # recomputed after eviction
    assert cluster.representative_comment_ids[0] == "fresh"
    assert set(cluster.representative_comment_ids) <= set(cluster.member_ids)


# ---------------------------------------------------------------------------
# 6.11 — skip/selection lifecycle persists on stable cluster identity
# ---------------------------------------------------------------------------


def test_lifecycle_state_survives_fast_lane_updates() -> None:
    store, now = _make_store()
    cid = _assign(store, "c1", 0, now[0], viewer_key="v1")
    now[0] += 1.0
    _assign(store, "c2", 0, now[0], viewer_key="v2")

    store.mark_selected(cid, now[0])
    store.increment_skip(cid)
    store.increment_skip(cid)

    now[0] += 1.0
    _assign(store, "c3", 0, now[0], viewer_key="v3")  # same topic -> same cluster

    cluster = store.get_cluster(cid)
    assert cluster is not None
    assert cluster.cluster_id == cid
    assert cluster.last_selected_at == now[0] - 1.0
    assert cluster.skip_count == 2
    assert cluster.message_count == 3
    assert cluster.unique_viewer_count == 3


def test_mark_answered_records_timestamp() -> None:
    store, now = _make_store()
    cid = _assign(store, "c1", 0, now[0])

    store.mark_answered(cid, now[0] + 2.0)

    cluster = store.get_cluster(cid)
    assert cluster is not None
    assert cluster.last_answered_at == now[0] + 2.0


def test_lifecycle_mutations_are_noop_for_unknown_cluster() -> None:
    store, now = _make_store()
    _assign(store, "c1", 0, now[0])

    store.mark_selected("nope", now[0])
    store.mark_answered("nope", now[0])
    store.increment_skip("nope")

    assert store.cluster_count() == 1  # nothing created, nothing crashed
