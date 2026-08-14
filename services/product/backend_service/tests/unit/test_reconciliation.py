"""Unit tests for reconciliation trigger + bounded repair (OpenSpec 5.5-5.6).

Deterministic by construction: injected fake clock, direct basis vectors.
Reconciliation NEVER runs inside run_once's hot path — the fast lane assigns
every comment immediately while the trigger state merely counts.

Merge tests use angle vectors (unit circle in dims 0-1) so clusters form
separately at assign time (member >= 60 deg apart) and then drift together as
members join — centroid cosine eventually crosses the merge threshold.
"""

from __future__ import annotations

import math

import pytest

from backend.application.reducer import (
    AcceptedComment,
    ClusterStore,
    ClusterStoreConfig,
    FastReducer,
    FastReducerConfig,
    ProductCandidate,
)


def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _vec(dim: int, basis: int) -> list[float]:
    """Basis vector ``basis`` of ``dim`` plus tiny deterministic noise."""
    return _l2([1.0 if i == basis else 0.001 * i for i in range(dim)])


def _angle(deg: float) -> list[float]:
    """Unit vector at ``deg`` on the dims-0-1 plane (8-dim embedding)."""
    r = math.radians(deg)
    return [math.cos(r), math.sin(r), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def _make_store(
    merge_threshold: float = 0.5,
    horizon: float = 75.0,
    max_clusters: int = 10,
    max_members: int = 200,
    reconcile_threshold: int = 100,
    reconcile_age: float = 60.0,
    cohesion_split: float = 0.15,
) -> tuple[ClusterStore, list[float]]:
    now = [1000.0]
    store = ClusterStore(
        "s1",
        config=ClusterStoreConfig(
            merge_threshold=merge_threshold,
            rolling_horizon_sec=horizon,
            max_active_clusters=max_clusters,
            max_members_per_cluster=max_members,
            reconcile_unreconciled_threshold=reconcile_threshold,
            reconcile_age_sec=reconcile_age,
            cohesion_split_threshold=cohesion_split,
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


class _FakeEmbedder:
    """Recording embedder with per-text vectors."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.calls: list[list[str]] = []
        self._vectors = vectors

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vectors.get(t, [1.0, 0.0, 0.0, 0.0]) for t in texts]


def _make_reducer(
    vectors: dict[str, list[float]],
    reconcile_threshold: int,
) -> tuple[FastReducer, _FakeEmbedder, list[float]]:
    now = [1000.0]
    embedder = _FakeEmbedder(vectors)
    reducer = FastReducer(
        config=FastReducerConfig(
            cluster_merge_threshold=0.5,
            reconcile_unreconciled_threshold=reconcile_threshold,
            reconcile_age_sec=60.0,
        ),
        embedder=embedder,
        now_fn=lambda: now[0],
    )
    return reducer, embedder, now


async def _burst(reducer: FastReducer, now: list[float], items: list[AcceptedComment]) -> None:
    now[0] += 0.4
    for item in items:
        reducer.notify_new_events("s1", item)
    await reducer.run_once("s1", now[0])


def _comment(comment_id: str, text: str, ts: float) -> AcceptedComment:
    return AcceptedComment(
        event_id=f"ev-{comment_id}",
        comment_id=comment_id,
        text=text,
        ts=ts,
        viewer_key="v1",
    )


# ---------------------------------------------------------------------------
# 5.5 — Trigger state: count / age, fast lane never withheld
# ---------------------------------------------------------------------------


def test_config_validation_rejects_non_positive_reconciliation_knobs() -> None:
    for kwargs in (
        {"reconcile_unreconciled_threshold": 0},
        {"reconcile_age_sec": 0.0},
        {"cohesion_split_threshold": 0.0},
    ):
        with pytest.raises(ValueError):
            ClusterStoreConfig(**kwargs).validate_runtime()


def test_trigger_by_count_flips_at_threshold_and_fast_lane_runs_throughout() -> None:
    store, now = _make_store(reconcile_threshold=100)
    assigned_ids = []

    for i in range(100):
        assigned_ids.append(_assign(store, f"c{i}", _vec(8, 0), now[0] + float(i)))

    assert store.reconcile_due(now[0] + 99.0) is True
    assert store.stats()["unreconciled_count"] == 100
    # Fast lane never withheld: every comment was assigned, all in one cluster.
    assert len(assigned_ids) == 100
    assert len({c.cluster_id for c in store.active_clusters(now[0] + 99.0)}) == 1


def test_trigger_not_due_below_count_or_age() -> None:
    store, now = _make_store(reconcile_threshold=100, reconcile_age=600.0)
    for i in range(99):
        _assign(store, f"c{i}", _vec(8, 0), now[0] + float(i))

    assert store.reconcile_due(now[0] + 98.0) is False


def test_trigger_by_age_low_traffic_with_fast_lane_active() -> None:
    store, now = _make_store(reconcile_threshold=100, reconcile_age=60.0)
    for i in range(14):
        _assign(store, f"c{i}", _vec(8, 0), now[0] + float(i))

    assert store.reconcile_due(now[0] + 59.0) is False
    assert store.reconcile_due(now[0] + 61.0) is True
    # The 14 comments were assigned to a cluster the whole time — never
    # withheld while waiting for the age trigger.
    assert len(store.active_clusters(now[0] + 61.0)) == 1
    assert store.active_clusters(now[0] + 61.0)[0].message_count == 14


def test_reconcile_resets_trigger_and_new_assigns_restart_count() -> None:
    store, now = _make_store(reconcile_threshold=3, reconcile_age=60.0)
    _assign(store, "c1", _vec(8, 0), now[0])
    _assign(store, "c2", _vec(8, 0), now[0] + 1.0)
    _assign(store, "c3", _vec(8, 0), now[0] + 2.0)
    assert store.reconcile_due(now[0] + 2.0) is True

    result = store.reconcile(now[0] + 2.0)

    assert result.clusters_before == 1
    assert store.stats()["unreconciled_count"] == 0
    assert store.reconcile_due(now[0] + 2.0) is False
    assert store.reconcile_due(now[0] + 3.0) is False  # age anchored at None

    _assign(store, "c4", _vec(8, 0), now[0] + 3.0)
    assert store.stats()["unreconciled_count"] == 1
    assert store.reconcile_due(now[0] + 3.0) is False


# ---------------------------------------------------------------------------
# 5.6 — Merge
# ---------------------------------------------------------------------------

# A = {c1@0, c2@20, c3@40} (centroid drifts to ~20 deg) and
# B = {c4@85, c5@60} (centroid ~72.5 deg): B's first member is > 60 deg from
# A's centroid (new cluster), then B's second member pulls B's centroid to
# within 60 deg of A -> centroid cosine ~0.61 >= 0.5 -> mergeable.


def _build_two_clusters(store: ClusterStore, now: list[float]) -> tuple[str, str]:
    cid_a = _assign(store, "c1", _angle(0), now[0])
    now[0] += 1.0
    _assign(store, "c2", _angle(20), now[0])
    now[0] += 1.0
    _assign(store, "c3", _angle(40), now[0])
    now[0] += 1.0
    cid_b = _assign(store, "c4", _angle(85), now[0])
    now[0] += 1.0
    _assign(store, "c5", _angle(60), now[0])
    return cid_a, cid_b


def test_merge_compatible_clusters_keeps_survivor_and_sums_members() -> None:
    store, now = _make_store(merge_threshold=0.5)
    cid_a, cid_b = _build_two_clusters(store, now)
    assert cid_a != cid_b

    result = store.reconcile(now[0])

    assert result.merged == 1
    active = store.active_clusters(now[0])
    assert len(active) == 1
    survivor = active[0]
    # Survivor is the higher-message_count cluster (A has 3 > B's 2).
    assert survivor.cluster_id == cid_a
    assert survivor.message_count == 5
    assert sorted(survivor.member_ids) == ["c1", "c2", "c3", "c4", "c5"]  # no dupes
    assert survivor.medoid_comment_id in survivor.member_ids
    # The absorbed cluster is gone from the store.
    assert store.get_cluster(cid_b) is None


def test_merge_shared_product_candidate_keeps_both() -> None:
    store, now = _make_store(merge_threshold=0.5)
    cid_a = _assign(
        store, "c1", _angle(0), now[0], products=[ProductCandidate("P001", 0.9, "explicit")]
    )
    now[0] += 1.0
    _assign(store, "c2", _angle(20), now[0])
    now[0] += 1.0
    _assign(store, "c3", _angle(40), now[0])
    now[0] += 1.0
    _assign(store, "c4", _angle(85), now[0], products=[ProductCandidate("P001", 0.8, "mention")])
    now[0] += 1.0
    _assign(store, "c5", _angle(60), now[0])

    result = store.reconcile(now[0])

    assert result.merged == 1
    survivor = store.get_cluster(cid_a)
    assert survivor is not None
    assert {p.product_id for p in survivor.product_candidates} == {"P001"}


def test_no_merge_when_resolved_products_conflict() -> None:
    store, now = _make_store(merge_threshold=0.5)
    cid_a = _assign(
        store, "c1", _angle(0), now[0], products=[ProductCandidate("P001", 0.9, "explicit")]
    )
    now[0] += 1.0
    _assign(store, "c2", _angle(20), now[0])
    now[0] += 1.0
    _assign(store, "c3", _angle(40), now[0])
    now[0] += 1.0
    cid_b = _assign(
        store, "c4", _angle(85), now[0], products=[ProductCandidate("P002", 0.9, "explicit")]
    )
    now[0] += 1.0
    _assign(store, "c5", _angle(60), now[0])
    store.get_cluster(cid_a).resolved_product_ids = ["P001"]
    store.get_cluster(cid_b).resolved_product_ids = ["P002"]

    result = store.reconcile(now[0])

    assert result.merged == 0
    assert len(store.active_clusters(now[0])) == 2


# ---------------------------------------------------------------------------
# 5.6 — Split
# ---------------------------------------------------------------------------


def test_split_moves_near_members_to_better_cluster() -> None:
    # A pathological cluster: c3 belongs next to B, the rest span
    # nearly-orthogonal directions (arrival-order artifact). Its real cohesion
    # (~0.35, medoid sim of near-orthogonal members) is far below the merge
    # threshold; the split knob is raised above it so the repair triggers.
    store, now = _make_store(merge_threshold=0.5, cohesion_split=0.5)
    _assign(store, "c1", _vec(8, 0), now[0])
    now[0] += 1.0
    _assign(store, "c2", _vec(8, 0), now[0])
    a = store._create_cluster(now[0])
    a.add_member("c3", "text c3", _vec(8, 0), now[0], "v1", "price", [])
    for i, basis in enumerate(range(1, 8), start=4):
        a.add_member(f"c{i}", f"text c{i}", _vec(8, basis), now[0] + 1.0 + i, "v1", "price", [])
    assert a.cohesion < 0.5  # near-orthogonal members: far below merge bar

    result = store.reconcile(now[0] + 10.0)

    assert result.split == 1
    active = store.active_clusters(now[0] + 10.0)
    assert len(active) == 2
    # Every member survives exactly once - no identity lost or duplicated.
    members = sorted(m for c in active for m in c.member_ids)
    assert sorted(members) == sorted(f"c{i}" for i in range(1, 11))
    # c3 moved next to B (cosine ~1), the far members stayed in A.
    b = min(active, key=lambda c: len(c.member_ids))
    assert sorted(b.member_ids) == ["c1", "c2", "c3"]


def test_no_split_when_cohesion_above_threshold() -> None:
    store, now = _make_store(merge_threshold=0.5, cohesion_split=0.15)
    _assign(store, "c1", _vec(8, 0), now[0])
    now[0] += 1.0
    _assign(store, "c2", _vec(8, 0), now[0])

    result = store.reconcile(now[0])

    assert result.split == 0
    assert len(store.active_clusters(now[0])) == 1


# ---------------------------------------------------------------------------
# 5.6 — Bounds + recompute
# ---------------------------------------------------------------------------


def test_merge_enforces_max_members_per_cluster() -> None:
    store, now = _make_store(merge_threshold=0.5, max_members=3)
    _assign(store, "c1", _angle(0), now[0])
    now[0] += 1.0
    _assign(store, "c2", _angle(45), now[0])
    now[0] += 1.0
    _assign(store, "c3", _angle(-40), now[0])
    now[0] += 1.0
    _assign(store, "c4", _angle(-10), now[0])

    store.reconcile(now[0])

    for cluster in store.active_clusters(now[0]):
        assert len(cluster.member_ids) <= 3


def test_reconcile_recomputes_survivor_centroid_and_medoid() -> None:
    store, now = _make_store(merge_threshold=0.5)
    cid_a, _ = _build_two_clusters(store, now)

    store.reconcile(now[0])

    survivor = store.get_cluster(cid_a)
    assert survivor is not None
    assert survivor.message_count == 5
    assert survivor.medoid_comment_id in survivor.member_ids
    assert set(survivor.representative_comment_ids) <= set(survivor.member_ids)
    assert len(survivor.centroid) == 8


# ---------------------------------------------------------------------------
# 5.5-5.6 — FastReducer integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_reducer_reconciliation_due_and_reconcile() -> None:
    vectors = {
        "a0": _angle(0),
        "a1": _angle(20),
        "a2": _angle(40),
        "b0": _angle(85),
        "b1": _angle(60),
    }
    reducer, _, now = _make_reducer(vectors, reconcile_threshold=5)
    for i in range(3):
        await _burst(reducer, now, [_comment(f"a{i}", f"a{i}", now[0])])
    for i in range(2):
        await _burst(reducer, now, [_comment(f"b{i}", f"b{i}", now[0])])

    assert reducer.reconciliation_due("s1", now[0]) is True
    result = await reducer.reconcile("s1", now[0])

    assert result.merged == 1
    stats = reducer.stats("s1")
    assert stats["reconciles_run"] == 1
    assert stats["reconcile_merged_total"] == 1
    assert stats["last_reconcile"]["clusters_after"] == 1
    assert stats["last_reconcile"]["members_removed"] == 0


@pytest.mark.asyncio
async def test_reconcile_noop_when_not_due_and_accumulates_across_runs() -> None:
    vectors = {
        "a0": _angle(0),
        "a1": _angle(20),
        "a2": _angle(40),
        "b0": _angle(85),
        "b1": _angle(60),
        "x0": _angle(180),
        "x1": _angle(200),
        "x2": _angle(220),
        "y0": _angle(265),
        "y1": _angle(240),
    }
    reducer, _, now = _make_reducer(vectors, reconcile_threshold=5)
    # 4 comments: below the count trigger -> reconcile is a no-op.
    for i in range(3):
        await _burst(reducer, now, [_comment(f"a{i}", f"a{i}", now[0])])
    await _burst(reducer, now, [_comment("b0", "b0", now[0])])
    assert reducer.reconciliation_due("s1", now[0]) is False

    noop = await reducer.reconcile("s1", now[0])

    assert noop.merged == 0
    assert reducer.stats("s1")["reconciles_run"] == 0

    # 5th comment crosses the threshold -> first reconciliation merges.
    await _burst(reducer, now, [_comment("b1", "b1", now[0])])
    assert reducer.reconciliation_due("s1", now[0]) is True
    first = await reducer.reconcile("s1", now[0])
    assert first.merged == 1
    assert reducer.stats("s1")["reconciles_run"] == 1
    assert reducer.stats("s1")["reconcile_merged_total"] == 1

    # Trigger reset: not due anymore, reconcile is a no-op.
    assert reducer.reconciliation_due("s1", now[0]) is False
    await reducer.reconcile("s1", now[0])
    assert reducer.stats("s1")["reconciles_run"] == 1

    # A second burst cycle (fresh far groups, same session) merges again and
    # accumulates counters.
    for i in range(3):
        await _burst(reducer, now, [_comment(f"x{i}", f"x{i}", now[0])])
    await _burst(reducer, now, [_comment("y0", "y0", now[0])])
    await _burst(reducer, now, [_comment("y1", "y1", now[0])])
    assert reducer.reconciliation_due("s1", now[0]) is True
    second = await reducer.reconcile("s1", now[0])
    assert second.merged == 1
    assert reducer.stats("s1")["reconciles_run"] == 2
    assert reducer.stats("s1")["reconcile_merged_total"] == 2
    assert reducer.stats("s1")["last_reconcile"]["merged"] == 1
