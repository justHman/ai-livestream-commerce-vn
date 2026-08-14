"""Benchmark commerce clustering thresholds with the production VN embedder."""

from __future__ import annotations

from backend.application.director.clustering import Comment, cluster_comments
from backend.application.director.embeddings import (
    BiEncoderEmbedder,
    DEFAULT_MODEL_ID,
    HashingEmbedder,
)
from backend.application.director.routing import route_comment


FIXTURE = [
    ("hoodie-size-1", "Áo hoodie còn cỡ XL không?", "hoodie-size"),
    ("hoodie-size-2", "Mẫu hoodie có size XL chứ shop?", "hoodie-size"),
    ("hoodie-price-1", "Áo hoodie giá bao nhiêu?", "hoodie-price"),
    ("hoodie-price-2", "Mẫu áo có giá thế nào vậy chị?", "hoodie-price"),
    ("serum-price-1", "Serum Vitamin C giá bao nhiêu?", "serum-price"),
    ("serum-price-2", "Chai vitamin C này nhiêu tiền shop?", "serum-price"),
    ("serum-stock-1", "Serum vitamin C còn hàng không?", "serum-stock"),
    ("serum-stock-2", "Chai serum này đã hết chưa chị?", "serum-stock"),
    ("social", "Chào chị Lan", "social"),
    ("spam", "Click bit.ly/xxx mua bên em", "spam"),
    ("complaint", "Đơn serum giao sai hàng", "complaint"),
    ("unknown", "Sản phẩm này có phù hợp với em không?", "unknown"),
]
EXPECTED_PAIRS = {
    frozenset(("hoodie-size-1", "hoodie-size-2")),
    frozenset(("hoodie-price-1", "hoodie-price-2")),
    frozenset(("serum-price-1", "serum-price-2")),
    frozenset(("serum-stock-1", "serum-stock-2")),
}


def benchmark(embedder, threshold: float) -> dict:
    from benchmarks.fixtures.products import CORPUS_PRODUCTS

    products = CORPUS_PRODUCTS
    texts = [text for _, text, _ in FIXTURE]
    vectors = embedder.encode(texts)
    comments = [
        route_comment(
            Comment(text=text, embedding=vector, t=0.0, id=comment_id),
            products,
            "P004",
        )
        for (comment_id, text, _), vector in zip(FIXTURE, vectors)
    ]
    clusters = cluster_comments(comments, merge_threshold=threshold)
    merged_pairs = {frozenset(cluster.member_ids) for cluster in clusters if cluster.size == 2}
    unexpected = merged_pairs - EXPECTED_PAIRS
    missed = EXPECTED_PAIRS - merged_pairs
    return {
        "embedder": embedder.name,
        "threshold": threshold,
        "comments": len(comments),
        "clusters": len(clusters),
        "singleton_ratio": round(sum(cluster.size == 1 for cluster in clusters) / len(clusters), 3),
        "expected_merges": len(merged_pairs & EXPECTED_PAIRS),
        "missed_merges": len(missed),
        "unexpected_merges": len(unexpected),
    }


def main() -> None:
    embedders = [HashingEmbedder(), BiEncoderEmbedder(DEFAULT_MODEL_ID)]
    print("embedder\tthreshold\tclusters\tsingletons\texpected\tmissed\tunexpected")
    for embedder in embedders:
        for threshold in (0.30, 0.35, 0.375, 0.40, 0.45, 0.55, 0.65, 0.75):
            result = benchmark(embedder, threshold)
            print(
                f"{result['embedder']}\t{threshold:.2f}\t{result['clusters']}\t"
                f"{result['singleton_ratio']:.3f}\t{result['expected_merges']}\t"
                f"{result['missed_merges']}\t{result['unexpected_merges']}"
            )


if __name__ == "__main__":
    main()
