"""Stream-analysis fixture: clustering/embeddings/scoring/routing over a fixed corpus.

Records the full deterministic pipeline the Director runs per cycle on a
fixed comment corpus: routing -> clustering -> ranking -> the top decision.
Outputs are the exact cluster partition, scores, and the chosen decision —
the canonical backend must reproduce them from the same inputs.
"""

from __future__ import annotations

from typing import Any

from backend.application.director.clustering import cluster_comments
from backend.application.director.decision import Director
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.scoring import rank_clusters

from ..corpus import (
    T0,
    build_state,
    cfg_with_qa_window,
    corpus_comments,
    corpus_products,
    empty_hook_pool,
    jsonable,
    routed_comments,
)


def scenario() -> dict[str, Any]:
    embedder = HashingEmbedder()
    products = corpus_products()
    for product, vector in zip(products, embedder.encode([p.embedding_text() for p in products])):
        product.embedding = vector

    comments = corpus_comments(embedder)
    current = products[0]
    routed = routed_comments(comments, current.id)
    state = build_state()
    state.cursor.opening_completed = True
    state.cursor.phase = "selling"
    state.cursor.talking_point_idx = 1
    state.product_elapsed_sec = 60.0

    window = [c for c in routed if T0 + 8.0 - c.t <= 75.0]
    clusters = cluster_comments(window, merge_threshold=0.55)
    ranked = rank_clusters(clusters, state, cfg_with_qa_window(), now=T0 + 8.0)

    director = Director(
        state=state,
        cfg=cfg_with_qa_window(),
        hook_pool=empty_hook_pool(),
        catalog={p.id: p for p in products},
    )
    decision = director.decide(routed, now=T0 + 8.0)

    inputs = {
        "embedder": {"name": embedder.name, "dim": embedder.dim},
        "merge_threshold": 0.55,
        "comments": [
            {
                "id": c.id,
                "text": c.text,
                "t": c.t,
                "category": c.category,
                "intent": c.intent,
                "product_id": c.product_id,
                "actionable": c.actionable,
            }
            for c in routed
        ],
        "now": T0 + 8.0,
    }
    outputs = {
        "clusters": [
            {
                "member_ids": c.member_ids,
                "members": c.members,
                "centroid": [round(x, 6) for x in c.centroid],
                "product_id": c.product_id,
                "intent": c.intent,
                "category": c.category,
                "size": c.size,
            }
            for c in clusters
        ],
        "ranked": [
            {
                "product_id": item.cluster.product_id,
                "intent": item.cluster.intent,
                "score": round(item.score, 4),
                "size": item.cluster.size,
                "member_ids": item.cluster.member_ids,
            }
            for item in ranked
        ],
        "decision": {
            "action": decision.action,
            "product_id": decision.product_id,
            "field": decision.field,
            "score": round(decision.score, 4),
            "cluster_members": list(decision.cluster_members),
            "topic": decision.topic,
        },
    }
    return {"inputs": jsonable(inputs), "outputs": jsonable(outputs)}
