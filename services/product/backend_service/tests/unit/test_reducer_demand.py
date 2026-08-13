"""Unique-viewer demand, pivot share, fingerprints, comparison fixtures (6.5-6.8, 6.10).

Deterministic by construction: injected fake clock closures, direct basis
vectors (no embedder), and route_hints over the same EntityDocument fixtures
as test_routing_hints.py. The 6.7 anti-inflation numbers are locked to the
default DemandConfig — if the weights change, the expected ranking in
test_repeated_single_viewer_never_dominates MUST be re-verified with it.
"""

from __future__ import annotations

import math

import pytest

from backend.application.director.routing import route_hints
from backend.application.entity.models import EntityDocument, KnowledgeBlock
from backend.application.reducer import (
    AcceptedComment,
    ClusterStore,
    ClusterStoreConfig,
    DemandConfig,
    DemandScore,
    FastReducer,
    FastReducerConfig,
    ProductCandidate,
    cluster_fingerprint,
    product_demand,
    score_clusters,
    should_pivot,
)

_PRODUCTS = [
    EntityDocument(
        id="P001",
        entity_type="product",
        name="Pin sạc dự phòng 20000mAh",
        aliases=["pin"],
        knowledge_blocks=[KnowledgeBlock(id="P001-k0", content="sạc nhanh 65W")],
    ),
    EntityDocument(
        id="P020",
        entity_type="product",
        name="Tai nghe bluetooth",
        aliases=["tai nghe"],
        knowledge_blocks=[KnowledgeBlock(id="P020-k0", content="chống ồn")],
    ),
]


def _comment(comment_id: str, text: str, ts: float, viewer_key: str = "v1") -> AcceptedComment:
    return AcceptedComment(
        event_id=f"ev-{comment_id}",
        comment_id=comment_id,
        text=text,
        ts=ts,
        viewer_key=viewer_key,
    )


class _FakeEmbedder:
    """Recording embedder with per-text vectors (default: one shared topic vector)."""

    def __init__(self, vectors: dict[str, list[float]] | None = None) -> None:
        self.calls: list[list[str]] = []
        self._vectors = vectors or {}

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self._vectors.get(t, [1.0, 0.0, 0.0, 0.0]) for t in texts]


def _l2(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def _vec(dim: int, basis: int) -> list[float]:
    """Basis vector ``basis`` of ``dim`` plus tiny deterministic noise."""
    return _l2([1.0 if i == basis else 0.001 * i for i in range(dim)])


def _make_reducer(
    products: list[EntityDocument] | None = None,
    current_product_id: str | None = None,
) -> tuple[FastReducer, list[float]]:
    now = [1000.0]
    reducer = FastReducer(
        config=FastReducerConfig(
            products=products or [],
            current_product_id=current_product_id,
        ),
        embedder=_FakeEmbedder(),
        now_fn=lambda: now[0],
    )
    return reducer, now


async def _burst(reducer: FastReducer, now: list[float], items: list[AcceptedComment]) -> None:
    """Advance the clock slightly, notify a burst, and run the fast lane."""
    now[0] += 0.4
    for item in items:
        reducer.notify_new_events("s1", item)
    await reducer.run_once("s1", now[0])


def _make_store(merge_threshold: float = 0.5) -> tuple[ClusterStore, list[float]]:
    now = [1000.0]
    store = ClusterStore(
        "s1",
        config=ClusterStoreConfig(merge_threshold=merge_threshold),
        now_fn=lambda: now[0],
    )
    return store, now


def _candidate(product_id: str, score: float = 3.0) -> ProductCandidate:
    return ProductCandidate(product_id, score, "explicit id/name/alias match")


# ---------------------------------------------------------------------------
# 6.5 — comparison fixtures: both products resolve AND survive reconciliation
# ---------------------------------------------------------------------------


def test_comparison_cluster_resolves_both_products() -> None:
    store, now = _make_store()
    hints = route_hints("P001 hay P020 cái nào tốt hơn", _PRODUCTS)

    assert {pid for pid, _, _ in hints.product_candidates} == {"P001", "P020"}

    cid = store.assign(
        comment_id="c1",
        text="P001 hay P020 cái nào tốt hơn",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="comparison",
        product_candidates=[
            ProductCandidate(pid, score, evidence)
            for pid, score, evidence in hints.product_candidates
        ],
    )

    cluster = store.get_cluster(cid)
    assert cluster is not None
    assert cluster.resolved_product_ids == ["P001", "P020"]
    assert cluster.product_resolution_confidence > 0.0


def test_reconciliation_does_not_merge_comparison_into_single_product_cluster() -> None:
    store, now = _make_store()
    store.assign(
        comment_id="c1",
        text="P001 hay P020 cái nào tốt hơn",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="comparison",
        product_candidates=[_candidate("P001"), _candidate("P020")],
    )
    now[0] += 1.0
    store.assign(
        comment_id="c2",
        text="P001 giá bao nhiêu",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v2",
        intent="price",
        product_candidates=[_candidate("P001")],
    )
    now[0] += 1.0

    result = store.reconcile(now[0])

    assert result.merged == 0  # resolved sets {P001,P020} vs {P001} are disjoint
    active = store.active_clusters(now[0])
    assert len(active) == 2
    by_intent = {c.intent: c for c in active}
    assert by_intent["comparison"].resolved_product_ids == ["P001", "P020"]
    assert by_intent["price"].resolved_product_ids == ["P001"]


# ---------------------------------------------------------------------------
# 6.6 — DemandConfig validation + score determinism
# ---------------------------------------------------------------------------


def test_demand_config_rejects_non_positive_knobs() -> None:
    for kwargs in (
        {"recency_half_life_sec": 0.0},
        {"unique_viewer_saturation": 0.0},
        {"message_saturation": 0.0},
    ):
        with pytest.raises(ValueError):
            DemandConfig(**kwargs).validate_runtime()


def test_score_clusters_excludes_spam_from_actionable_ranking() -> None:
    store, now = _make_store()
    cid = store.assign(
        comment_id="c1",
        text="spam",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="spam",
    )
    cluster = store.get_cluster(cid)
    assert cluster is not None

    ranked = score_clusters([cluster], None, now[0], DemandConfig())

    assert ranked == []


def test_score_clusters_sorts_descending_then_by_cluster_id() -> None:
    store, now = _make_store()
    # Identical demand shape (1 viewer, 1 message, same intent, same ts):
    # equal scores, so the deterministic tie-break is cluster_id ascending.
    a = store.assign(
        comment_id="a1",
        text="P001 giá bao nhiêu",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="price",
        product_candidates=[_candidate("P001")],
    )
    b = store.assign(
        comment_id="b1",
        text="P020 giá bao nhiêu",
        vector=_vec(8, 1),
        ts=now[0],
        viewer_key="v2",
        intent="price",
        product_candidates=[_candidate("P020")],
    )
    shuffled = [store.get_cluster(b), store.get_cluster(a)]
    assert all(c is not None for c in shuffled)

    ranked = score_clusters([c for c in shuffled if c is not None], None, now[0], DemandConfig())

    assert [d.cluster_id for d in ranked] == sorted([a, b])
    for d in ranked:
        assert isinstance(d, DemandScore)
        assert len(d.breakdown()) == 8


# ---------------------------------------------------------------------------
# 6.7 — repeated-single-viewer anti-inflation
# ---------------------------------------------------------------------------


def test_repeated_single_viewer_never_dominates_broad_demand() -> None:
    store, now = _make_store()
    for i in range(20):
        store.assign(
            comment_id=f"r{i}",
            text="P001 giá bao nhiêu",
            vector=_vec(8, 0),
            ts=now[0] + float(i) * 0.1,
            viewer_key="flood",
            intent="price",
            product_candidates=[_candidate("P001")],
        )
    now[0] += 5.0
    for i in range(6):
        store.assign(
            comment_id=f"d{i}",
            text="P020 giá bao nhiêu",
            vector=_vec(8, 1),
            ts=now[0] + float(i) * 0.1,
            viewer_key=f"v{i}",
            intent="price",
            product_candidates=[_candidate("P020")],
        )

    active = store.active_clusters(now[0])
    flood = next(c for c in active if c.unique_viewer_count == 1)
    broad = next(c for c in active if c.unique_viewer_count == 6)
    assert flood.unique_viewer_count == 1
    assert flood.message_count == 20
    assert broad.unique_viewer_count == 6
    assert broad.message_count == 6

    ranked = score_clusters([flood, broad], None, now[0], DemandConfig())
    scores = {d.cluster_id: d for d in ranked}

    # Unique-viewer demand is primary: 6 independent viewers outrank 20 repeats.
    assert scores[broad.cluster_id].score > scores[flood.cluster_id].score


def test_repeated_messages_nudge_recency_without_flipping_ranking() -> None:
    store, now = _make_store()
    for i in range(20):
        store.assign(
            comment_id=f"r{i}",
            text="P001 giá bao nhiêu",
            vector=_vec(8, 0),
            ts=now[0] + float(i) * 0.1,
            viewer_key="flood",
            intent="price",
            product_candidates=[_candidate("P001")],
        )
    now[0] += 5.0
    for i in range(6):
        store.assign(
            comment_id=f"d{i}",
            text="P020 giá bao nhiêu",
            vector=_vec(8, 1),
            ts=now[0] + float(i) * 0.1,
            viewer_key=f"v{i}",
            intent="price",
            product_candidates=[_candidate("P020")],
        )

    active = store.active_clusters(now[0])
    flood = next(c for c in active if c.unique_viewer_count == 1)
    broad = next(c for c in active if c.unique_viewer_count == 6)
    flood_newest = flood.newest_t

    # One more repeat from the SAME viewer, later: recency nudges, demand not.
    store.assign(
        comment_id="r20",
        text="P001 giá bao nhiêu",
        vector=_vec(8, 0),
        ts=now[0] + 3.0,
        viewer_key="flood",
        intent="price",
        product_candidates=[_candidate("P001")],
    )

    assert flood.newest_t == now[0] + 3.0
    assert flood.newest_t > flood_newest
    assert flood.unique_viewer_count == 1  # still one viewer

    ranked = score_clusters([flood, broad], None, now[0], DemandConfig())
    scores = {d.cluster_id: d for d in ranked}

    assert scores[broad.cluster_id].score > scores[flood.cluster_id].score


# ---------------------------------------------------------------------------
# 6.8 — pivot on unique-viewer demand, not raw message counts
# ---------------------------------------------------------------------------


def test_pivot_one_viewer_flood_never_enters() -> None:
    # 50 messages of B from ONE viewer: share (1/6) AND minimum (1 < 3) fail.
    products = {
        "P001": {"unique_viewers": 5, "messages": 6},
        "P020": {"unique_viewers": 1, "messages": 50},
    }

    assert should_pivot(products, "P020", "P001") is None
    # Even a relaxed minimum cannot help: the unique-viewer share is the gate.
    assert should_pivot(products, "P020", "P001", min_unique_viewers=1) is None


def test_pivot_enter_on_four_distinct_viewers() -> None:
    products = {
        "P001": {"unique_viewers": 2, "messages": 4},
        "P020": {"unique_viewers": 4, "messages": 6},
    }

    assert should_pivot(products, "P020", "P001") == "enter"


def test_pivot_prefers_enter_over_exit() -> None:
    products = {
        "P001": {"unique_viewers": 1, "messages": 5},
        "P020": {"unique_viewers": 6, "messages": 6},
    }

    assert should_pivot(products, "P020", "P001") == "enter"


def test_pivot_exit_when_current_unique_share_drops() -> None:
    # Target below the unique-viewer minimum: no enter; current share 1/3 < 0.45 -> exit.
    products = {
        "P001": {"unique_viewers": 1, "messages": 50},
        "P020": {"unique_viewers": 2, "messages": 3},
    }

    assert should_pivot(products, "P020", "P001") == "exit"
    # With the pivot already on P020, P001 (share 1/3) is below the exit bar
    # AND P020 is still above its enter gates -> no exit from the pivot.
    assert should_pivot(products, "P001", "P020") is None


def test_product_demand_aggregates_unique_viewers_per_resolved_product() -> None:
    store, now = _make_store()
    store.assign(
        comment_id="c1",
        text="P001 giá bao nhiêu",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="price",
        product_candidates=[_candidate("P001")],
    )
    now[0] += 1.0
    store.assign(
        comment_id="c2",
        text="P020 giá bao nhiêu",
        vector=_vec(8, 1),
        ts=now[0],
        viewer_key="v2",
        intent="price",
        product_candidates=[_candidate("P020")],
    )

    demand = product_demand(store.active_clusters(now[0]), now[0])

    assert demand["P001"] == {"unique_viewers": 1, "messages": 1}
    assert demand["P020"] == {"unique_viewers": 1, "messages": 1}


def test_product_demand_counts_comparison_viewers_for_both_products() -> None:
    store, now = _make_store()
    store.assign(
        comment_id="c1",
        text="P001 hay P020 cái nào tốt hơn",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="comparison",
        product_candidates=[_candidate("P001"), _candidate("P020")],
    )

    demand = product_demand(store.active_clusters(now[0]), now[0])

    assert demand["P001"] == {"unique_viewers": 1, "messages": 1}
    assert demand["P020"] == {"unique_viewers": 1, "messages": 1}


# ---------------------------------------------------------------------------
# 6.10 — stable semantic fingerprint for cooldown identity
# ---------------------------------------------------------------------------


def test_fingerprint_stable_across_same_membership_updates() -> None:
    """Revision updates (same comment ids) keep the fingerprint: ids are stable.

    A same-topic revision re-assigns the SAME comment_id, so the member id
    set — and thus the fingerprint — is unchanged across the fast-lane update.
    """
    store, now = _make_store()
    cid = store.assign(
        comment_id="c1",
        text="P001 giá bao nhiêu",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="price",
        product_candidates=[_candidate("P001")],
    )
    first = store.get_cluster(cid)
    assert first is not None
    first_fp = first.novelty_fingerprint
    assert first_fp != ""

    now[0] += 1.0
    store.assign(
        comment_id="c1",  # same id, revised text (revision re-embeds)
        text="P001 giá bao nhiêu ạ",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="price",
        product_candidates=[_candidate("P001")],
    )

    cluster = store.get_cluster(cid)
    assert cluster is not None
    assert cluster.cluster_id == cid
    assert cluster.novelty_fingerprint == first_fp
    assert cluster_fingerprint(cluster) == first_fp


def test_fingerprint_differs_for_distinct_questions_same_product_intent() -> None:
    store, now = _make_store()
    a = store.assign(
        comment_id="a1",
        text="P001 giá bao nhiêu",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="price",
        product_candidates=[_candidate("P001")],
    )
    now[0] += 1.0
    b = store.assign(
        comment_id="b1",
        text="P001 giá bao nhiêu",
        vector=_vec(8, 1),
        ts=now[0],
        viewer_key="v2",
        intent="price",
        product_candidates=[_candidate("P001")],
    )

    fa = cluster_fingerprint(store.get_cluster(a))
    fb = cluster_fingerprint(store.get_cluster(b))

    assert fa != fb  # same product+intent, different members -> different topic


def test_fingerprint_deterministic_across_refresh() -> None:
    store, now = _make_store()
    cid = store.assign(
        comment_id="c1",
        text="cái này giá bao nhiêu",
        vector=_vec(8, 0),
        ts=now[0],
        viewer_key="v1",
        intent="price",
        product_candidates=[],
    )
    cluster = store.get_cluster(cid)
    assert cluster is not None

    before = cluster.novelty_fingerprint
    store.refresh_novelty(cid)
    after = cluster.novelty_fingerprint

    assert before == after == cluster_fingerprint(cluster)
    assert isinstance(after, str)
    assert len(after) == 64
