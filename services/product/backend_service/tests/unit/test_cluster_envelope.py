"""Unit tests for the canonical ClusterEnvelope (tasks 7.1, 7.3).

The envelope is built from a REAL LiveCluster (add_member path) so these tests
exercise the same data flow the store produces. Vectors are basis vectors
plus tiny noise (as in test_cluster_store) — no embedder needed.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from backend.application.agentic_director.fast_path import ClusterEnvelope as FastPathEnvelope
from backend.application.reducer import (
    ClusterEnvelope,
    ClusterStore,
    ClusterStoreConfig,
    LiveCluster,
    ProductCandidate,
    build_envelope,
)


def _l2(vec: list[float]) -> list[float]:
    n = sum(x * x for x in vec) ** 0.5 or 1.0
    return [x / n for x in vec]


def _vec(dim: int, basis: int) -> list[float]:
    """Basis vector ``basis`` of ``dim`` plus tiny deterministic noise."""
    return _l2([1.0 if i == basis else 0.001 * i for i in range(dim)])


def _make_cluster(
    text: str = "giá bao nhiêu",
    intent: str = "price",
    viewer_key: str = "tiktok:s1:v1",
) -> LiveCluster:
    cluster = LiveCluster(cluster_id="cl-1", created_at=1000.0, updated_at=1000.0)
    cluster.add_member(
        comment_id="c1",
        text=text,
        vector=_vec(4, 0),
        ts=1000.0,
        viewer_key=viewer_key,
        intent=intent,
        product_candidates=[ProductCandidate("sp-001", 0.9)],
    )
    return cluster


def test_envelope_satisfies_fast_path_protocol() -> None:
    envelope = build_envelope(
        _make_cluster(),
        score_breakdown=(("size", 0.5),),
        ranking_score=0.7,
        novelty=0.2,
    )

    assert isinstance(envelope, FastPathEnvelope)


def test_envelope_derives_all_fields_from_cluster() -> None:
    cluster = _make_cluster(text="giá bao nhiêu", viewer_key="tiktok:s1:v1")
    cluster.add_member(
        comment_id="c2",
        text="còn hàng không",
        vector=_vec(4, 0),
        ts=1001.0,
        viewer_key="shopee:s1:v2",
        intent="price",
        product_candidates=[ProductCandidate("sp-002", 0.6)],
    )
    cluster.resolved_product_ids = ["sp-001", "sp-002"]

    envelope = build_envelope(
        cluster,
        score_breakdown=(("size", 0.5),),
        ranking_score=0.7,
        novelty=0.2,
        current_script_product_id="sp-001",
    )

    assert envelope == ClusterEnvelope(
        cluster_id="cl-1",
        intent="price",
        message_count=2,
        unique_viewer_count=2,
        representative_questions=("còn hàng không", "giá bao nhiêu"),
        product_candidates=(("sp-001", 0.9), ("sp-002", 0.6)),
        resolved_product_ids=("sp-001", "sp-002"),
        ranking_score=0.7,
        score_breakdown=(("size", 0.5),),
        novelty=0.2,
        current_script_product_id="sp-001",
        source_platform_counts=(("shopee", 1), ("tiktok", 1)),
    )


def test_score_breakdown_preserves_tuple_order() -> None:
    breakdown = (
        ("product_relevance", 0.1),
        ("intent_actionability", 0.2),
        ("size", 0.3),
        ("recency", 0.4),
        ("phase", 0.5),
        ("new_demand", 0.6),
        ("total", 0.7),
    )

    envelope = build_envelope(
        _make_cluster(), score_breakdown=breakdown, ranking_score=0.7, novelty=0.2
    )

    assert envelope.score_breakdown == breakdown


def test_score_breakdown_accepts_mapping() -> None:
    envelope = build_envelope(
        _make_cluster(),
        score_breakdown={"size": 0.3, "total": 0.7},
        ranking_score=0.7,
        novelty=0.2,
    )

    assert envelope.score_breakdown == (("size", 0.3), ("total", 0.7))


def test_representative_questions_capped_by_config() -> None:
    cluster = _make_cluster()
    for i in range(1, 5):
        cluster.add_member(
            comment_id=f"c{i}",
            text=f"câu hỏi {i}",
            vector=_vec(4, 0),
            ts=1000.0 + i,
            viewer_key="tiktok:s1:v1",
            intent="price",
            product_candidates=[],
        )

    envelope = build_envelope(
        cluster,
        score_breakdown=(),
        ranking_score=0.7,
        novelty=0.2,
        config=ClusterStoreConfig(max_representatives=3),
    )

    assert len(envelope.representative_questions) == 3


def test_representative_questions_only_from_representative_members() -> None:
    cluster = _make_cluster(text="rep text")
    cluster.add_member(
        comment_id="c2",
        text="non representative",
        vector=_vec(4, 1),
        ts=1001.0,
        viewer_key="tiktok:s1:v2",
        intent="price",
        product_candidates=[],
    )
    cluster.representative_comment_ids = ["c1"]

    envelope = build_envelope(cluster, score_breakdown=(), ranking_score=0.7, novelty=0.2)

    assert envelope.representative_questions == ("rep text",)


def test_representative_questions_fallback_to_medoid() -> None:
    cluster = _make_cluster(text="medoid text")
    cluster.representative_comment_ids = []

    envelope = build_envelope(cluster, score_breakdown=(), ranking_score=0.7, novelty=0.2)

    assert envelope.representative_questions == ("medoid text",)


def test_source_platform_counts_mixed_prefixes() -> None:
    cluster = _make_cluster(viewer_key="tiktok:s1:v1")
    cluster.add_member(
        comment_id="c2",
        text="x",
        vector=_vec(4, 0),
        ts=1001.0,
        viewer_key="tiktok:s1:v3",
        intent="price",
        product_candidates=[],
    )
    cluster.add_member(
        comment_id="c3",
        text="y",
        vector=_vec(4, 0),
        ts=1002.0,
        viewer_key="shopee:s1:v2",
        intent="price",
        product_candidates=[],
    )

    envelope = build_envelope(cluster, score_breakdown=(), ranking_score=0.7, novelty=0.2)

    assert envelope.source_platform_counts == (("shopee", 1), ("tiktok", 2))


def test_source_platform_counts_malformed_keys_unknown() -> None:
    cluster = _make_cluster(viewer_key="noprefix")
    cluster.add_member(
        comment_id="c2",
        text="x",
        vector=_vec(4, 0),
        ts=1001.0,
        viewer_key="tiktok:s1:v2",
        intent="price",
        product_candidates=[],
    )

    envelope = build_envelope(cluster, score_breakdown=(), ranking_score=0.7, novelty=0.2)

    assert envelope.source_platform_counts == (("noprefix", 1), ("tiktok", 1))


def test_current_script_product_id_passthrough_and_default() -> None:
    with_product = build_envelope(
        _make_cluster(),
        score_breakdown=(),
        ranking_score=0.7,
        novelty=0.2,
        current_script_product_id="sp-007",
    )
    without = build_envelope(_make_cluster(), score_breakdown=(), ranking_score=0.7, novelty=0.2)

    assert with_product.current_script_product_id == "sp-007"
    assert without.current_script_product_id is None


def test_envelope_is_frozen() -> None:
    envelope = build_envelope(_make_cluster(), score_breakdown=(), ranking_score=0.7, novelty=0.2)

    with pytest.raises(FrozenInstanceError):
        envelope.intent = "other"  # type: ignore[misc]


def test_envelope_from_store_assignment() -> None:
    now = [1000.0]
    store = ClusterStore("s1", now_fn=lambda: now[0])
    for i in range(3):
        store.assign(
            comment_id=f"c{i}",
            text=f"giá sp-00{i} bao nhiêu",
            vector=_vec(4, 0),
            ts=now[0] + i,
            viewer_key=f"tiktok:s1:v{i}",
            intent="price",
            product_candidates=[ProductCandidate(f"sp-00{i}", 0.9)],
        )

    envelope = build_envelope(
        store.active_clusters(now[0])[0],
        score_breakdown=(("size", 0.5),),
        ranking_score=0.7,
        novelty=0.2,
    )

    assert envelope.message_count == 3
    assert envelope.source_platform_counts == (("tiktok", 3),)


def test_envelope_never_embeds_raw_member_corpus() -> None:
    cluster = _make_cluster(text="giá bao nhiêu", viewer_key="tiktok:s1:v1")
    for i in range(1, 4):
        cluster.add_member(
            comment_id=f"c{i}",
            text=f"comment {i}",
            vector=_vec(4, 0),
            ts=1000.0 + i,
            viewer_key="tiktok:s1:v1",
            intent="price",
            product_candidates=[],
        )
    cluster.representative_comment_ids = ["c1"]

    envelope = build_envelope(cluster, score_breakdown=(), ranking_score=0.7, novelty=0.2)
    serialized = str(envelope)

    assert "c2" not in serialized
    assert "c3" not in serialized
    assert "comment 2" not in serialized
    assert "comment 3" not in serialized
