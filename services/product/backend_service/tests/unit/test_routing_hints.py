"""Unit tests for soft product routing + cluster-level resolution (OpenSpec 6).

``route_hints`` keeps the hard single-product partition out of pre-cluster
routing: intent + confidence-bearing product candidates only, ambiguity
preserved. ``LiveCluster.resolve_products`` turns merged candidates into a
deterministic zero/one/many resolution under a confidence threshold + margin
on the top score. Deterministic by construction: fake clock closure and
direct basis vectors, no embedder.
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
    FastReducer,
    FastReducerConfig,
    ProductCandidate,
)


def _product(
    pid: str,
    name: str,
    aliases: list[str] | None = None,
    blocks: list[str] | None = None,
) -> EntityDocument:
    return EntityDocument(
        id=pid,
        entity_type="product",
        name=name,
        aliases=aliases or [],
        knowledge_blocks=[
            KnowledgeBlock(id=f"{pid}-k{i}", content=c) for i, c in enumerate(blocks or [])
        ],
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


# ---------------------------------------------------------------------------
# route_hints — soft routing (OpenSpec 6.1/6.2)
# ---------------------------------------------------------------------------


def test_ambiguous_reference_yields_no_product_candidates() -> None:
    products = [_product("P001", "Pin sạc dự phòng 20000mAh", ["powerbank"])]

    hints = route_hints("cái này pin tốt không?", products, "P001")

    assert hints.product_candidates == []
    assert hints.actionable is True


def test_explicit_id_match_is_high_confidence() -> None:
    products = [_product("P001", "Pin sạc dự phòng 20000mAh", ["pin"])]

    hints = route_hints("P001 giá bao nhiêu vậy?", products)

    assert len(hints.product_candidates) == 1
    pid, score, evidence = hints.product_candidates[0]
    assert pid == "P001"
    assert score >= 2.0
    assert "explicit id/name/alias match" in evidence


def test_explicit_name_alias_match_is_high_confidence() -> None:
    products = [
        _product("P001", "Pin sạc dự phòng 20000mAh", ["sạc dự phòng"]),
        _product("P020", "Kem chống nắng 50ml", ["kem chống nắng"]),
    ]

    hints = route_hints("sạc dự phòng này dùng sao ạ?", products)

    assert len(hints.product_candidates) == 1
    pid, score, evidence = hints.product_candidates[0]
    assert pid == "P001"
    assert score >= 2.0
    assert "explicit id/name/alias match" in evidence


def test_id_only_matches_full_token_not_substring() -> None:
    products = [_product("P001", "Pin sạc dự phòng 20000mAh", ["powerbank"])]

    hints = route_hints("P0010 có pin tốt không?", products)

    assert hints.product_candidates == []


def test_comparison_yields_both_products_within_margin() -> None:
    products = [
        _product("P001", "Pin sạc dự phòng 20000mAh", ["pin"]),
        _product("P020", "Tai nghe bluetooth", ["tai nghe"]),
    ]

    hints = route_hints("P001 hay P020 tốt hơn?", products)

    by_id = {pid: score for pid, score, _ in hints.product_candidates}
    assert set(by_id) == {"P001", "P020"}
    assert abs(by_id["P001"] - by_id["P020"]) < 1.0
    assert all(score >= 2.0 for score in by_id.values())


def test_spam_off_topic_social_classification_preserved() -> None:
    cases = {
        "mua hàng qua link bit.ly ngay hôm nay": (["spam"], []),
        "tối nay xem bóng đá không": (["off_topic"], []),
        "hello chị ơi": (["social"], []),
    }
    products = [_product("P001", "Pin sạc dự phòng 20000mAh", ["powerbank"])]

    for text, (intents, candidates) in cases.items():
        hints = route_hints(text, products)
        assert hints.intent_candidates == intents
        assert hints.product_candidates == candidates


def test_current_product_id_is_not_implicit_candidate() -> None:
    products = [_product("P001", "Pin sạc dự phòng 20000mAh", ["powerbank"])]

    hints = route_hints("giá bao nhiêu vậy?", products, current_product_id="P001")

    assert hints.product_candidates == []


def test_non_explicit_matches_are_low_score_candidates() -> None:
    products = [
        _product("P001", "Pin sạc dự phòng 20000mAh", ["powerbank"], blocks=["sạc nhanh 65W"]),
        _product("P020", "Tai nghe bluetooth", ["tai nghe"], blocks=["chống ồn"]),
    ]

    hints = route_hints("sạc nhanh thì có tốt không?", products)

    by_id = {pid: score for pid, score, _ in hints.product_candidates}
    assert set(by_id) == {"P001"}
    assert by_id["P001"] <= 2.0
    assert "explicit id/name/alias match" not in hints.product_candidates[0][2]


# ---------------------------------------------------------------------------
# Cluster-level product resolution (OpenSpec 6.3/6.4)
# ---------------------------------------------------------------------------


def test_cluster_strong_explicit_match_resolves_to_one_product() -> None:
    store = ClusterStore("s1", config=ClusterStoreConfig(), now_fn=lambda: 1000.0)

    cid = store.assign(
        comment_id="c1",
        text="P001 giá bao nhiêu",
        vector=_vec(8, 0),
        ts=1000.0,
        viewer_key="v1",
        intent="price",
        product_candidates=[ProductCandidate("P001", 3.0, "explicit id/name/alias match")],
    )

    cluster = store.get_cluster(cid)
    assert cluster is not None
    assert cluster.resolved_product_ids == ["P001"]
    assert cluster.product_resolution_confidence > 0.0


def test_cluster_weak_scores_below_threshold_resolve_to_nothing() -> None:
    store = ClusterStore("s1", config=ClusterStoreConfig(), now_fn=lambda: 1000.0)

    cid = store.assign(
        comment_id="c1",
        text="cái này pin tốt không",
        vector=_vec(8, 0),
        ts=1000.0,
        viewer_key="v1",
        intent="price",
        product_candidates=[ProductCandidate("P001", 1.0, "pin")],
    )

    cluster = store.get_cluster(cid)
    assert cluster is not None
    assert cluster.resolved_product_ids == []
    assert cluster.product_resolution_confidence == 0.0


def test_cluster_comparison_resolves_both_products_with_margin() -> None:
    store = ClusterStore("s1", config=ClusterStoreConfig(), now_fn=lambda: 1000.0)

    cid = store.assign(
        comment_id="c1",
        text="P001 hay P020 tốt hơn",
        vector=_vec(8, 0),
        ts=1000.0,
        viewer_key="v1",
        intent="comparison",
        product_candidates=[
            ProductCandidate("P001", 3.0, "explicit id/name/alias match"),
            ProductCandidate("P020", 3.0, "explicit id/name/alias match"),
        ],
    )

    cluster = store.get_cluster(cid)
    assert cluster is not None
    assert cluster.resolved_product_ids == ["P001", "P020"]
    assert cluster.product_resolution_confidence > 0.0


def test_cluster_low_scorer_outside_margin_is_not_resolved() -> None:
    store = ClusterStore("s1", config=ClusterStoreConfig(), now_fn=lambda: 1000.0)

    cid = store.assign(
        comment_id="c1",
        text="P001 và sạc nhanh",
        vector=_vec(8, 0),
        ts=1000.0,
        viewer_key="v1",
        intent="usage",
        product_candidates=[
            ProductCandidate("P001", 3.0, "explicit id/name/alias match"),
            ProductCandidate("P020", 1.0, "sạc nhanh"),
        ],
    )

    cluster = store.get_cluster(cid)
    assert cluster is not None
    assert cluster.resolved_product_ids == ["P001"]


def test_resolution_knobs_validate() -> None:
    with pytest.raises(ValueError):
        ClusterStoreConfig(product_resolution_threshold=0.0).validate_runtime()
    with pytest.raises(ValueError):
        ClusterStoreConfig(product_resolution_margin=-1.0).validate_runtime()
    with pytest.raises(ValueError):
        FastReducerConfig(product_resolution_threshold=0.0).validate_runtime()
    with pytest.raises(ValueError):
        FastReducerConfig(product_resolution_margin=-1.0).validate_runtime()


# ---------------------------------------------------------------------------
# FastReducer wiring (OpenSpec 6.4): hints flow into the store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_assigns_hints_and_resolution_into_store() -> None:
    products = [
        _product("P001", "Pin sạc dự phòng 20000mAh", ["pin"]),
        _product("P020", "Tai nghe bluetooth", ["tai nghe"]),
    ]
    reducer, now = _make_reducer(products)

    await _burst(reducer, now, [_comment("c1", "P001 giá bao nhiêu vậy", now[0])])
    await _burst(reducer, now, [_comment("c2", "P020 giá bao nhiêu vậy", now[0])])

    cluster = reducer._get_store("s1").active_clusters(now[0])[0]
    assert {p.product_id for p in cluster.product_candidates} == {"P001", "P020"}
    assert cluster.resolved_product_ids == ["P001", "P020"]
    assert cluster.product_resolution_confidence > 0.0
    assert cluster.intent == "price"


@pytest.mark.asyncio
async def test_same_routed_intent_and_similar_vectors_share_one_cluster() -> None:
    products = [
        _product("P001", "Pin sạc dự phòng 20000mAh", ["pin"]),
        _product("P020", "Tai nghe bluetooth", ["tai nghe"]),
    ]
    reducer, now = _make_reducer(products)

    await _burst(reducer, now, [_comment("c1", "P001 giá bao nhiêu vậy", now[0])])
    await _burst(reducer, now, [_comment("c2", "P020 giá bao nhiêu vậy", now[0])])

    active = reducer._get_store("s1").active_clusters(now[0])
    assert len(active) == 1
    assert sorted(active[0].member_ids) == ["c1", "c2"]


@pytest.mark.asyncio
async def test_fast_lane_without_products_still_assigns_empty_candidates() -> None:
    reducer, now = _make_reducer()

    await _burst(reducer, now, [_comment("c1", "giá bao nhiêu vậy", now[0])])

    cluster = reducer._get_store("s1").active_clusters(now[0])[0]
    assert cluster.product_candidates == []
    assert cluster.resolved_product_ids == []
    assert cluster.product_resolution_confidence == 0.0
    assert cluster.intent == "price"
