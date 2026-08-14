"""Decision-preparation fixture: comment batch + run-plan -> prepared decision input.

Records the exact Director Decision produced from a fixed comment batch and
run-plan (the deterministic FSM path): action, product, field, stage,
task_id, may_interrupt, score, cluster members. ``turn_id`` and other
uuid-derived fields are pinned to deterministic values by the input contract.
"""

from __future__ import annotations

from typing import Any

from backend.application.director.catalog import embedding_text
from backend.application.director.decision import Director
from backend.application.director.embeddings import HashingEmbedder
from backend.application.schemas.run_plan import RunPlan, SellingTask

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


def _run_plan() -> RunPlan:
    return RunPlan(
        phases=["opening", "selling", "closing"],
        selling=[
            {
                "product_id": "P004",
                "product_name": "Áo hoodie HeyGen màu trắng",
                "key_selling_points": ["cotton dày", "form rộng", "giá 299k"],
                "tasks": [
                    SellingTask(
                        stage="intro",
                        task_id="P004:intro",
                        instruction="Mở sản phẩm bằng 1-2 câu hoàn chỉnh.",
                    ),
                    SellingTask(
                        stage="benefit",
                        task_id="P004:benefit",
                        instruction="Nêu lợi ích nổi bật của áo hoodie.",
                    ),
                    SellingTask(
                        stage="offer",
                        task_id="P004:offer",
                        instruction="Nêu giá và ưu đãi rõ ràng.",
                    ),
                ],
            },
            {
                "product_id": "P002",
                "product_name": "Serum Vitamin C 20%",
                "key_selling_points": ["sáng da", "vitamin C 20%"],
                "tasks": [
                    SellingTask(
                        stage="intro",
                        task_id="P002:intro",
                        instruction="Mở sản phẩm serum.",
                    ),
                ],
            },
        ],
    )


def scenario() -> dict[str, Any]:
    embedder = HashingEmbedder()
    comments = corpus_comments(embedder)
    # Seed the catalog embeddings for retrieval.
    products = corpus_products()
    for product, vector in zip(products, embedder.encode([embedding_text(p) for p in products])):
        product.embedding = vector

    plan = _run_plan()
    state = build_state(run_plan=plan)
    state.traffic.viewer_count = 90
    state.traffic.msg_rate = 4.2
    state.cursor.opening_completed = True
    state.cursor.phase = "selling"
    state.cursor.talking_point_idx = 1
    state.phase_elapsed_sec = 300.0
    state.product_elapsed_sec = 60.0

    current = state.current_product()
    routed = routed_comments(comments, current.product_id if current else None)
    state.add_comments(routed)

    director = Director(
        state=state,
        cfg=cfg_with_qa_window(),
        hook_pool=empty_hook_pool(),
        catalog={p.id: p for p in products},
    )
    decision = director.decide(routed, now=T0 + 8.0)

    inputs = {
        "run_plan": plan.model_dump(),
        "comment_batch": [
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
        "action": decision.action,
        "product_id": decision.product_id,
        "field": decision.field,
        "stage": decision.stage,
        "task_id": decision.task_id,
        "may_interrupt": decision.may_interrupt,
        "reason": decision.reason,
        "score": round(decision.score, 4),
        "cluster_members": list(decision.cluster_members),
        "topic": decision.topic,
        "text": decision.text,
    }
    return {"inputs": jsonable(inputs), "outputs": jsonable(outputs)}
