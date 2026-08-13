"""Store-wiring tests for FastReducer (OpenSpec 5.3-5.4).

The fast lane now assigns every embedded comment into a per-session
ClusterStore and expires members outside the rolling horizon on the write
path. Deterministic by construction: fake clock via a mutable ``now``
closure, recording fake embedder with per-text deterministic vectors, and
vectors chosen so all comments land in one cluster.
"""

from __future__ import annotations

import pytest

from backend.application.reducer import AcceptedComment, FastReducer, FastReducerConfig


def _comment(comment_id: str, text: str, ts: float) -> AcceptedComment:
    return AcceptedComment(
        event_id=f"ev-{comment_id}",
        comment_id=comment_id,
        text=text,
        ts=ts,
        viewer_key="v1",
    )


class _FakeEmbedder:
    """Recording embedder with per-text vectors (default: one shared topic vector)."""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._vectors = vectors or {}

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vectors.get(t, [1.0, 0.0, 0.0, 0.0]) for t in texts]


def _make_reducer(
    horizon: float = 75.0, vectors: dict[str, list[float]] | None = None
) -> tuple[FastReducer, _FakeEmbedder, list[float]]:
    now = [1000.0]
    embedder = _FakeEmbedder(vectors)
    reducer = FastReducer(
        config=FastReducerConfig(rolling_horizon_sec=horizon),
        embedder=embedder,
        now_fn=lambda: now[0],
    )
    return reducer, embedder, now


async def _burst(reducer: FastReducer, now: list[float], items: list[AcceptedComment]) -> None:
    """Advance the clock slightly, notify a burst, and run the fast lane."""
    now[0] += 0.4
    for item in items:
        reducer.notify_new_events("s1", item)
    await reducer.run_once("s1", now[0])


@pytest.mark.asyncio
async def test_config_accepts_store_knobs_and_rejects_non_positive() -> None:
    reducer, _, _ = _make_reducer()

    assert reducer._config.cluster_merge_threshold == 0.375
    assert reducer._config.max_active_clusters == 40
    assert reducer._config.max_members_per_cluster == 200
    assert reducer._config.max_representatives == 5
    assert reducer._config.reconcile_unreconciled_threshold == 100
    assert reducer._config.reconcile_age_sec == 60.0

    for kwargs in (
        {"cluster_merge_threshold": 0.0},
        {"max_active_clusters": 0},
        {"max_members_per_cluster": 0},
        {"max_representatives": 0},
        {"reconcile_unreconciled_threshold": 0},
        {"reconcile_age_sec": 0.0},
    ):
        with pytest.raises(ValueError):
            FastReducerConfig(**kwargs).validate_runtime()


@pytest.mark.asyncio
async def test_run_once_assigns_comments_to_store() -> None:
    reducer, _, now = _make_reducer()
    await _burst(reducer, now, [_comment("c1", "giá bao nhiêu", now[0])])
    await _burst(reducer, now, [_comment("c2", "giá tốt quá", now[0])])

    active = reducer._get_store("s1").active_clusters(now[0])

    assert len(active) == 1
    assert sorted(active[0].member_ids) == ["c1", "c2"]
    assert active[0].message_count == 2


@pytest.mark.asyncio
async def test_same_topic_across_bursts_keeps_stable_cluster_id() -> None:
    reducer, _, now = _make_reducer()
    await _burst(reducer, now, [_comment("c1", "giá bao nhiêu", now[0])])
    first_id = reducer._get_store("s1").active_clusters(now[0])[0].cluster_id
    await _burst(reducer, now, [_comment("c2", "giá tốt quá", now[0])])

    active = reducer._get_store("s1").active_clusters(now[0])

    assert [c.cluster_id for c in active] == [first_id]
    assert active[0].message_count == 2


@pytest.mark.asyncio
async def test_revision_reassigns_same_comment_id_without_double_counting() -> None:
    reducer, _, now = _make_reducer()
    await _burst(reducer, now, [_comment("c1", "giá bao nhiêu", now[0])])
    cluster = reducer._get_store("s1").active_clusters(now[0])[0]

    assert cluster.message_count == 1
    assert cluster.member_ids == ["c1"]

    now[0] += 0.4
    reducer.notify_new_events("s1", _comment("c1", "giá bao nhiêu ạ", now[0]))
    await reducer.run_once("s1", now[0])

    cluster = reducer._get_store("s1").active_clusters(now[0])[0]
    assert cluster.member_ids == ["c1"]
    assert cluster.message_count == 1  # same comment_id is an in-place update
    assert cluster._member_texts["c1"] == "giá bao nhiêu ạ"
    assert cluster._member_ts["c1"] == now[0]


@pytest.mark.asyncio
async def test_expiry_evicts_old_members_and_drops_empty_clusters() -> None:
    reducer, _, now = _make_reducer(
        horizon=5.0,
        vectors={"cũ": [1.0, 0.0, 0.0, 0.0], "mới": [0.0, 1.0, 0.0, 0.0]},
    )
    await _burst(reducer, now, [_comment("old", "cũ", now[0])])
    now[0] += 6.0
    await _burst(reducer, now, [_comment("fresh", "mới", now[0])])

    store = reducer._get_store("s1")
    active = store.active_clusters(now[0])

    assert [c.member_ids for c in active] == [["fresh"]]
    assert store.stats()["evicted_members"] == 1
    assert store.stats()["evicted_clusters"] == 1
    assert store.stats()["active_cluster_count"] == 1


@pytest.mark.asyncio
async def test_memory_stays_horizon_bounded_across_advancing_bursts() -> None:
    reducer, _, now = _make_reducer(horizon=5.0)
    for i in range(12):
        await _burst(reducer, now, [_comment(f"c{i}", f"text {i}", now[0])])
        last_run = now[0]  # expiry happens on the write path, at this instant
        now[0] += 1.0

    store = reducer._get_store("s1")
    all_ts = [c._member_ts[cid] for c in store._clusters.values() for cid in c.member_ids]

    # Only members inside the 5s horizon survive the last write-path expiry.
    assert len(all_ts) == 4
    assert all(last_run - ts < 5.0 for ts in all_ts)
    assert store.stats()["evicted_members"] == 12 - len(all_ts)
    assert store.stats()["active_cluster_count"] == 1
