"""Playback fixture: run-plan -> playback schedule.

Records the deterministic proactive sales-turn schedule the Director would
play back from a run plan: stage, task_id, instruction for each planned
turn, in operator order, per product. The canonical backend must reproduce
the same schedule.
"""

from __future__ import annotations

from typing import Any

from backend.application.director.decision import Director
from backend.application.schemas.run_plan import RunPlan, SellingTask

from ..corpus import (
    build_state,
    cfg_with_qa_window,
    corpus_products,
    empty_hook_pool,
    jsonable,
)


def _run_plan() -> RunPlan:
    return RunPlan(
        phases=["opening", "selling", "closing"],
        selling=[
            {
                "product_id": "P004",
                "product_name": "Áo hoodie HeyGen màu trắng",
                "key_selling_points": ["cotton dày", "form rộng"],
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
                    SellingTask(
                        stage="cta",
                        task_id="P002:cta",
                        instruction="Kêu gọi chốt đơn tự nhiên.",
                    ),
                ],
            },
        ],
    )


def scenario() -> dict[str, Any]:
    products = corpus_products()
    plan = _run_plan()
    state = build_state(run_plan=plan)
    state.cursor.opening_completed = True
    state.cursor.phase = "selling"

    director = Director(
        state=state,
        cfg=cfg_with_qa_window(),
        hook_pool=empty_hook_pool(),
        catalog={p.id: p for p in products},
    )
    schedule: list[dict[str, Any]] = []
    for product in state.products:
        tasks = director._sales_tasks(product.product_id)
        for index, (stage, task_id, instruction) in enumerate(tasks):
            schedule.append(
                {
                    "product_id": product.product_id,
                    "stage": stage,
                    "task_id": task_id,
                    "instruction": instruction,
                    "turn_index": index,
                }
            )

    inputs = {"run_plan": plan.model_dump()}
    outputs = {"schedule": schedule}
    return {"inputs": jsonable(inputs), "outputs": jsonable(outputs)}
