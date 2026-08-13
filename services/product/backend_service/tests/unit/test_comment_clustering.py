"""Deterministic Vietnamese commerce routing and clustering fixtures."""

from __future__ import annotations

from backend.application.entity.models import EntityDocument
from backend.application.director.catalog import product_to_entity
from backend.application.director.clustering import Comment, cluster_comments
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.routing import route_comment
from backend.application.director.scoring import rank_clusters
from backend.application.director.config import StreamConfig
from backend.application.director.state import Phase, ProductState, StreamState


def _comment(text: str, vector: list[float], comment_id: str) -> Comment:
    return Comment(text=text, embedding=vector, t=10.0, id=comment_id)


def _products() -> list[EntityDocument]:
    return [
        product_to_entity(
            {
                "id": "P004",
                "name": "Áo hoodie HeyGen màu trắng",
                "features": ["hoodie", "size XL"],
            }
        ),
        product_to_entity(
            {
                "id": "P002",
                "name": "Serum Vitamin C 20%",
                "features": ["serum", "vitamin C"],
            }
        ),
    ]


def test_commerce_router_covers_required_categories_and_intents() -> None:
    products = _products()
    cases = {
        "Áo hoodie giá bao nhiêu?": ("commerce", "price", True),
        "Hoodie còn size XL không?": ("commerce", "size_color", True),
        "Serum còn hàng không?": ("commerce", "stock", True),
        "Chốt một chai serum": ("commerce", "buy_intent", True),
        "Đơn serum giao sai hàng": ("commerce", "complaint", True),
        "Chào chị Lan": ("social", "social", False),
        "Click bit.ly/xxx mua bên em": ("spam", "spam", False),
        "Bóng đá tối nay mấy giờ?": ("off_topic", "off_topic", False),
    }

    for index, (text, expected) in enumerate(cases.items()):
        routed = route_comment(_comment(text, [1.0, 0.0], str(index)), products, "P004")
        assert (routed.category, routed.intent, routed.actionable) == expected


def test_unknown_comment_requires_semantic_product_retrieval() -> None:
    routed = route_comment(
        _comment("unrelated topic", [0.0, 1.0], "unknown"),
        [product_to_entity({"id": "P004", "name": "Áo hoodie"})],
        "P004",
    )

    assert (routed.intent, routed.product_id) == ("unknown", None)


def test_generic_product_noun_routes_away_from_current_product() -> None:
    products = [
        product_to_entity({"id": "P004", "name": "Áo hoodie HeyGen màu trắng"}),
        product_to_entity({"id": "P001", "name": "Kem chống nắng La Roche-Posay SPF50+"}),
    ]

    routed = route_comment(
        _comment("Kem này bao nhiêu tiền vậy chị?", [0.0, 1.0], "cream-price"),
        products,
        "P004",
    )

    assert (routed.intent, routed.product_id) == ("price", "P001")


def test_product_and_intent_partitions_prevent_false_merges() -> None:
    products = _products()
    raw = [
        _comment("Áo hoodie giá bao nhiêu?", [1.0, 0.0], "hoodie-price"),
        _comment("Serum giá bao nhiêu?", [1.0, 0.0], "serum-price"),
        _comment("Áo hoodie còn size XL không?", [1.0, 0.0], "hoodie-size"),
    ]
    routed = [route_comment(item, products, "P004") for item in raw]

    clusters = cluster_comments(routed, merge_threshold=0.55)

    assert len(clusters) == 3
    assert {(cluster.product_id, cluster.intent) for cluster in clusters} == {
        ("P004", "price"),
        ("P002", "price"),
        ("P004", "size_color"),
    }


def test_hash_baseline_records_fixture_cluster_quality() -> None:
    embedder = HashingEmbedder()
    products = _products()
    texts = [
        "Áo hoodie còn cỡ XL không?",
        "Mẫu hoodie có size XL chứ shop?",
        "Áo hoodie giá bao nhiêu?",
        "Serum giá bao nhiêu?",
        "Chào chị Lan",
        "Click bit.ly/xxx mua bên em",
    ]
    vectors = embedder.encode(texts)
    routed = [
        route_comment(_comment(text, vector, str(index)), products, "P004")
        for index, (text, vector) in enumerate(zip(texts, vectors))
    ]

    clusters = cluster_comments(routed, merge_threshold=0.55)
    singleton_ratio = sum(cluster.size == 1 for cluster in clusters) / len(clusters)

    # Lexical hashing is the recorded degraded baseline, not the production gate.
    assert len(clusters) == 6
    assert singleton_ratio == 1.0
    assert all(
        len({cluster.product_id, "P004"}) > 1 or cluster.intent != "price"
        for cluster in clusters
        if "Serum" in cluster.members[0]
    )


def test_semantic_threshold_gate_when_model_is_available() -> None:
    pytest = __import__("pytest")
    sentence_transformers = pytest.importorskip("sentence_transformers")
    assert sentence_transformers is not None
    from backend.application.director.embeddings import BiEncoderEmbedder, DEFAULT_MODEL_ID

    embedder = BiEncoderEmbedder(DEFAULT_MODEL_ID)
    products = _products()
    cases = [
        ("hoodie-size-1", "Áo hoodie còn cỡ XL không?"),
        ("hoodie-size-2", "Mẫu hoodie có size XL chứ shop?"),
        ("hoodie-price-1", "Áo hoodie giá bao nhiêu?"),
        ("hoodie-price-2", "Mẫu áo có giá thế nào vậy chị?"),
        ("serum-price-1", "Serum Vitamin C giá bao nhiêu?"),
        ("serum-price-2", "Chai vitamin C này nhiêu tiền shop?"),
        ("serum-stock-1", "Serum vitamin C còn hàng không?"),
        ("serum-stock-2", "Chai serum này đã hết chưa chị?"),
    ]
    vectors = embedder.encode([text for _, text in cases])
    routed = [
        route_comment(_comment(text, vector, comment_id), products, "P004")
        for (comment_id, text), vector in zip(cases, vectors)
    ]

    clusters = cluster_comments(routed, merge_threshold=StreamConfig().cluster_merge_threshold)

    assert {frozenset(cluster.member_ids) for cluster in clusters} == {
        frozenset(("hoodie-size-1", "hoodie-size-2")),
        frozenset(("hoodie-price-1", "hoodie-price-2")),
        frozenset(("serum-price-1", "serum-price-2")),
        frozenset(("serum-stock-1", "serum-stock-2")),
    }


def test_same_product_intent_paraphrases_merge_and_noise_is_not_ranked() -> None:
    products = _products()
    raw = [
        _comment("Áo hoodie còn cỡ XL không?", [1.0, 0.0], "size-1"),
        _comment("Mẫu hoodie có size XL chứ shop?", [1.0, 0.0], "size-2"),
        _comment("Chào chị Lan", [1.0, 0.0], "social"),
        _comment("Click bit.ly/xxx mua bên em", [1.0, 0.0], "spam"),
    ]
    routed = [route_comment(item, products, "P004") for item in raw]
    clusters = cluster_comments(routed, merge_threshold=0.55)
    state = StreamState(
        phase=Phase.SELLING,
        products=[
            ProductState(product_id="P004", name="Áo hoodie", embedding=[1.0, 0.0]),
            ProductState(product_id="P002", name="Serum", embedding=[0.0, 1.0]),
        ],
    )

    ranked = rank_clusters(clusters, state, StreamConfig(), now=10.0)

    assert any(cluster.size == 2 and cluster.intent == "size_color" for cluster in clusters)
    assert len(ranked) == 1
    assert ranked[0].cluster.intent == "size_color"
