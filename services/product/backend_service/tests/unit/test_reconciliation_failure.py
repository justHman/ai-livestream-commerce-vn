"""Unit tests for fail-safe reconciliation + typed diagnostics (OpenSpec 5.7).

A failed reconcile pass must restore the exact pre-pass state (last valid
fast-lane cluster state), must NOT reset the reconciliation trigger, must
record a typed content-safe ``ReconciliationFailure``, and must never crash
the fast lane. Failures are injected deterministically through the store's
test-only one-shot seam ``_fail_next_reconcile_at`` (consumed before any
mutation, then cleared).
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
    ReconciliationError,
)


def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _angle(deg: float) -> list[float]:
    """Unit vector at ``deg`` on the dims-0-1 plane (8-dim embedding)."""
    r = math.radians(deg)
    return [math.cos(r), math.sin(r), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]


def _assign(
    store: ClusterStore,
    comment_id: str,
    vec: list[float],
    ts: float,
    viewer_key: str = "v1",
    intent: str = "price",
) -> str:
    return store.assign(
        comment_id=comment_id,
        text=f"text {comment_id}",
        vector=vec,
        ts=ts,
        viewer_key=viewer_key,
        intent=intent,
        product_candidates=[],
    )


def _comment(comment_id: str, text: str, ts: float) -> AcceptedComment:
    return AcceptedComment(
        event_id=f"ev-{comment_id}",
        comment_id=comment_id,
        text=text,
        ts=ts,
        viewer_key="v1",
    )


class _FakeEmbedder:
    """Recording embedder with per-text vectors."""

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors[t] for t in texts]


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


def _make_store(**kwargs) -> tuple[ClusterStore, list[float]]:
    now = [1000.0]
    config = ClusterStoreConfig(merge_threshold=0.5, max_active_clusters=10)
    for key, value in kwargs.items():
        setattr(config, key, value)
    store = ClusterStore("s1", config=config, now_fn=lambda: now[0])
    return store, now


def _build_two_clusters(store: ClusterStore, now: list[float]) -> tuple[str, str]:
    """Two mergeable clusters: A={c1,c2,c3} near 0 deg, B={c4,c5} near 90 deg."""
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


def _cluster_state(store: ClusterStore) -> list[tuple[str, int, list[str]]]:
    """Deterministic fingerprint of the store's clusters: (id, count, members)."""
    return sorted(
        (c.cluster_id, len(c.member_ids), sorted(c.member_ids)) for c in store._clusters.values()
    )


# ---------------------------------------------------------------------------
# Store level: restore + typed failure + trigger preserved
# ---------------------------------------------------------------------------


def test_failed_reconcile_restores_exact_pre_pass_state() -> None:
    store, now = _make_store(reconcile_unreconciled_threshold=2, reconcile_age_sec=60.0)
    _build_two_clusters(store, now)
    before_state = _cluster_state(store)
    store._fail_next_reconcile_at = "merge_state_corruption"

    with pytest.raises(ReconciliationError) as exc_info:
        store.reconcile(now[0] + 1.0)

    assert _cluster_state(store) == before_state
    failure = exc_info.value.failure
    assert failure.failure_code == "merge_state_corruption"
    assert failure.restored is True
    assert failure.clusters_before == 2
    assert failure.members_before == 5
    assert failure.session_id == "s1"
    assert failure.at == now[0]  # injected clock time of the failed pass


def test_failed_reconcile_preserves_trigger_state() -> None:
    store, now = _make_store(reconcile_unreconciled_threshold=2, reconcile_age_sec=60.0)
    _assign(store, "c1", _angle(0), now[0])
    now[0] += 1.0
    _assign(store, "c2", _angle(20), now[0])
    assert store.reconcile_due(now[0]) is True
    store._fail_next_reconcile_at = "expiry_error"

    with pytest.raises(ReconciliationError):
        store.reconcile(now[0])

    # Trigger NOT reset by the failed pass: reconciliation stays due so a
    # later attempt can succeed.
    assert store.stats()["unreconciled_count"] == 2
    assert store.reconcile_due(now[0]) is True


def test_store_records_typed_failure_in_stats_no_raw_text() -> None:
    store, now = _make_store(reconcile_unreconciled_threshold=2, reconcile_age_sec=60.0)
    _assign(store, "c1", _angle(0), now[0], viewer_key="viewer-secret")
    now[0] += 1.0
    _assign(store, "c2", _angle(20), now[0])
    store._fail_next_reconcile_at = "merge_state_corruption"

    with pytest.raises(ReconciliationError):
        store.reconcile(now[0])

    stats = store.stats()
    assert stats["reconciliation_failures"] == 1
    failure = stats["last_reconciliation_failure"]
    assert failure["failure_code"] == "merge_state_corruption"
    assert failure["restored"] is True
    assert failure["session_id"] == "s1"
    assert set(failure) == {
        "session_id",
        "failure_code",
        "error_message",
        "at",
        "clusters_before",
        "members_before",
        "restored",
    }
    # Content-safe diagnostics: raw viewer text never appears.
    assert "viewer-secret" not in str(failure)


def test_failed_reconcile_keeps_fast_lane_operational() -> None:
    store, now = _make_store(reconcile_unreconciled_threshold=2, reconcile_age_sec=60.0)
    cid_a, cid_b = _build_two_clusters(store, now)
    store._fail_next_reconcile_at = "merge_state_corruption"

    with pytest.raises(ReconciliationError):
        store.reconcile(now[0])

    # assign() still works on the restored state: the new member lands in the
    # best-matching cluster and the trigger keeps counting.
    now[0] += 1.0
    cid = _assign(store, "c6", _angle(10), now[0])
    assert store.stats()["member_ids_count"] == 6
    assert store.get_cluster(cid) is not None
    assert cid == cid_a  # 10 deg is closest to cluster A
    assert store.get_cluster(cid_b) is not None  # pre-pass clusters intact
    assert store.stats()["unreconciled_count"] == 6


def test_reconcile_succeeds_after_failure_and_keeps_last_failure() -> None:
    store, now = _make_store(reconcile_unreconciled_threshold=2, reconcile_age_sec=60.0)
    _build_two_clusters(store, now)
    store._fail_next_reconcile_at = "merge_state_corruption"

    with pytest.raises(ReconciliationError):
        store.reconcile(now[0])

    # Seam is one-shot and self-cleared: the retry runs cleanly.
    result = store.reconcile(now[0])

    assert result.merged == 1
    assert store.reconcile_due(now[0]) is False
    assert store.stats()["unreconciled_count"] == 0
    stats = store.stats()
    assert stats["reconciliation_failures"] == 1
    assert stats["last_reconciliation_failure"]["restored"] is True


def test_clean_reconciliation_records_no_failure() -> None:
    store, now = _make_store(reconcile_unreconciled_threshold=2, reconcile_age_sec=60.0)
    _assign(store, "c1", _angle(0), now[0])
    now[0] += 1.0
    _assign(store, "c2", _angle(20), now[0])

    store.reconcile(now[0])

    assert store.last_reconciliation_failure is None
    assert store.stats()["reconciliation_failures"] == 0
    assert store.stats()["last_reconciliation_failure"] is None


# ---------------------------------------------------------------------------
# FastReducer level: never propagates, diagnostics exposed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reducer_reconcile_failure_does_not_propagate_and_records_diagnostics() -> None:
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
    reducer._get_store("s1")._fail_next_reconcile_at = "merge_state_corruption"

    result = await reducer.reconcile("s1", now[0])

    # No exception: the fast lane keeps operating; the failure is observable
    # only through diagnostics.
    assert result.merged == 0
    stats = reducer.stats("s1")
    assert stats["reconciliation_failures"] == 1
    assert stats["reconciles_run"] == 0
    failure = stats["last_reconciliation_failure"]
    assert failure["failure_code"] == "merge_state_corruption"
    assert failure["restored"] is True
    assert failure["clusters_before"] == 2


@pytest.mark.asyncio
async def test_reducer_records_no_failure_after_clean_reconcile() -> None:
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

    await reducer.reconcile("s1", now[0])

    stats = reducer.stats("s1")
    assert stats["reconciliation_failures"] == 0
    assert stats["last_reconciliation_failure"] is None
    assert stats["reconciles_run"] == 1
    assert stats["last_reconcile"]["merged"] == 1


@pytest.mark.asyncio
async def test_reducer_unknown_session_stats_include_failure_fields() -> None:
    reducer, _, _ = _make_reducer({}, reconcile_threshold=5)

    stats = reducer.stats("missing")

    assert stats["reconciliation_failures"] == 0
    assert stats["last_reconciliation_failure"] is None
