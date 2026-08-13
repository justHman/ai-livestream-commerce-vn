"""Unit tests for LiveCluster / ClusterStore (OpenSpec 5.1-5.2).

Deterministic by construction: an injected fake clock (a mutable ``now``
captured by closure) drives all timestamps, and vectors are built directly
(basis vectors plus a small in-cluster jitter) — no embedder needed.
"""

from __future__ import annotations

import math

import pytest

from backend.application.reducer import (
    ClusterStore,
    ClusterStoreConfig,
    ProductCandidate,
)


def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _vec(dim: int, basis: int) -> list[float]:
    """Basis vector ``basis`` of ``dim`` plus tiny deterministic noise."""
    return _l2([1.0 if i == basis else 0.001 * i for i in range(dim)])


def _make_store(
    merge_threshold: float = 0.5,
    horizon: float = 75.0,
    max_clusters: int = 10,
    max_members: int = 200,
    max_reps: int = 5,
) -> tuple[ClusterStore, list[float]]:
    now = [1000.0]
    store = ClusterStore(
        "s1",
        config=ClusterStoreConfig(
            merge_threshold=merge_threshold,
            rolling_horizon_sec=horizon,
            max_active_clusters=max_clusters,
            max_members_per_cluster=max_members,
            max_representatives=max_reps,
        ),
        now_fn=lambda: now[0],
    )
    return store, now


def _assign(
    store: ClusterStore,
    comment_id: str,
    vec: list[float],
    ts: float,
    viewer_key: str = "v1",
    intent: str = "price",
    products: list[ProductCandidate] | None = None,
) -> str:
    return store.assign(
        comment_id=comment_id,
        text=f"text {comment_id}",
        vector=vec,
        ts=ts,
        viewer_key=viewer_key,
        intent=intent,
        product_candidates=products,
    )


def test_config_validation_rejects_non_positive_knobs() -> None:
    for kwargs in (
        {"merge_threshold": 0.0},
        {"rolling_horizon_sec": 0.0},
        {"max_active_clusters": 0},
        {"max_members_per_cluster": 0},
        {"max_representatives": 0},
    ):
        with pytest.raises(ValueError):
            ClusterStoreConfig(**kwargs).validate_runtime()


def test_cluster_id_is_stable_across_assignments_on_same_topic() -> None:
    store, now = _make_store()
    first = _assign(store, "c1", _vec(8, 0), now[0])
    now[0] += 1.0
    second = _assign(store, "c2", _vec(8, 0), now[0])

    assert first == second
    cluster = store.get_cluster(first)
    assert cluster is not None
    assert cluster.member_ids == ["c1", "c2"]
    assert cluster.message_count == 2
    assert cluster.newest_t == now[0]


def test_multi_product_cluster_allowed_no_hard_partition() -> None:
    store, now = _make_store()
    _assign(store, "c1", _vec(8, 0), now[0], products=[ProductCandidate("P001", 0.8, "explicit")])
    now[0] += 1.0
    cid2 = _assign(
        store, "c2", _vec(8, 0), now[0], products=[ProductCandidate("P020", 0.7, "mention")]
    )

    cluster = store.get_cluster(cid2)
    assert cluster is not None
    assert [c.product_id for c in cluster.product_candidates] == ["P001", "P020"]


def test_incompatible_intent_does_not_merge() -> None:
    store, now = _make_store()
    a = _assign(store, "c1", _vec(8, 0), now[0], intent="price")
    now[0] += 1.0
    b = _assign(store, "c2", _vec(8, 0), now[0], intent="shipping")

    assert a != b
    assert len(store.active_clusters(now[0])) == 2


def test_max_active_clusters_drops_smallest_deterministically() -> None:
    store, now = _make_store(max_clusters=2)
    ids = [_assign(store, f"c{i}", _vec(8, i), now[0] + float(i)) for i in range(3)]
    assert len(ids) == len(set(ids))

    active = store.active_clusters(now[0] + 10.0)

    assert len(active) == 2
    assert all(c.cluster_id != ids[0] for c in active)  # c0 (1 msg) evicted
    assert store.stats()["evicted_count"] == 1
    assert store.stats()["total_clusters_created"] == 3


def test_max_members_per_cluster_evicts_oldest_members() -> None:
    store, now = _make_store(max_members=3)
    for i in range(5):
        _assign(store, f"c{i}", _vec(8, 0), now[0] + float(i))

    cluster = next(iter(store.active_clusters(now[0] + 10.0)))

    assert cluster.member_ids == ["c2", "c3", "c4"]
    assert cluster.message_count == 3
    assert store.stats()["evicted_count"] == 2


def test_recompute_medoid_and_representatives() -> None:
    store, now = _make_store()
    ids = [_assign(store, f"c{i}", _vec(8, 0), now[0] + float(i)) for i in range(7)]
    cluster = store.get_cluster(ids[0])
    assert cluster is not None

    assert cluster.medoid_comment_id in cluster.member_ids
    assert set(cluster.representative_comment_ids) <= set(cluster.member_ids)
    assert len(cluster.representative_comment_ids) == 5
    assert cluster.representative_comment_ids[0] == cluster.medoid_comment_id
    assert 0.0 < cluster.cohesion <= 1.0


def test_active_clusters_excludes_old_but_keeps_them() -> None:
    store, now = _make_store(horizon=75.0)
    old_id = _assign(store, "c1", _vec(8, 0), now[0])
    now[0] += 76.0
    new_id = _assign(store, "c2", _vec(8, 1), now[0])

    active = store.active_clusters(now[0])

    assert [c.cluster_id for c in active] == [new_id]
    assert store.get_cluster(old_id) is not None  # retained until eviction
    assert store.stats()["active_cluster_count"] == 2


def test_livecluster_remove_member_recomputes() -> None:
    store, now = _make_store()
    cid = _assign(store, "c1", _vec(8, 0), now[0], viewer_key="v1")
    now[0] += 1.0
    _assign(store, "c2", _vec(8, 0), now[0], viewer_key="v2")
    cluster = store.get_cluster(cid)
    assert cluster is not None

    cluster.remove_member("c1")

    assert cluster.member_ids == ["c2"]
    assert cluster.message_count == 1
    assert cluster.unique_viewer_count == 1
    assert cluster.medoid_comment_id == "c2"
    assert cluster.representative_comment_ids == ["c2"]
