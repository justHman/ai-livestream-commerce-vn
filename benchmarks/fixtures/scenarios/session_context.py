"""Session-context fixture: lifecycle states -> context snapshots.

Records how the legacy Director builds per-session context from the product
catalog + run plan: phase transitions, current product, cursor, traffic mode,
and per-product stage/turn state. Output is the full inspectable state the
canonical backend must reproduce.
"""

from __future__ import annotations

from typing import Any

from backend.application.director.state import Phase, ProductStatus

from ..corpus import build_state, jsonable
from ..products import CORPUS_PRODUCTS


def _block_items(entity, tag: str) -> list[str]:
    """Items of a knowledge block carrying ``tag`` (color/size), in order."""
    for block in entity.knowledge_blocks:
        if tag in block.tags:
            return [part.strip() for part in block.content.split(",") if part.strip()]
    return []


def scenario() -> dict[str, Any]:
    state = build_state(phase=Phase.SELLING, current_product_index=0)
    state.traffic.viewer_count = 120
    state.traffic.msg_rate = 3.5
    state.phase_elapsed_sec = 400.0
    state.product_elapsed_sec = 120.0
    state.sec_since_relevant_msg = 10.0
    state.cursor.talking_point_idx = 2
    state.cursor.opening_completed = True
    state.cursor.phase = "selling"
    # Product lifecycle bookkeeping after an intro + one stage.
    for product in state.products:
        if product.product_id == "P004":
            product.status = ProductStatus.ACTIVE
            product.is_introduced = True
            product.stage = "benefit"
            product.stage_turn_index = 2
            product.spoken_turns = 3
            product.cluster_count = 2
        elif product.product_id == "P002":
            product.status = ProductStatus.PENDING
            product.is_introduced = False

    inputs = {
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "price": (
                    p.get_fact("commerce.price.current").value
                    if p.get_fact("commerce.price.current") is not None
                    else None
                ),
                "original_price": (
                    p.get_fact("commerce.price.original").value
                    if p.get_fact("commerce.price.original") is not None
                    else None
                ),
                "shipping": (
                    p.get_fact("commerce.shipping").value
                    if p.get_fact("commerce.shipping") is not None
                    else None
                ),
                "sizes": _block_items(p, "size"),
                "colors": _block_items(p, "color"),
                "stock_total": (
                    p.get_fact("commerce.stock.quantity").value
                    if p.get_fact("commerce.stock.quantity") is not None
                    else None
                ),
            }
            for p in CORPUS_PRODUCTS
        ],
        "traffic": {"viewer_count": 120, "msg_rate": 3.5},
        "phase": "selling",
        "current_product_index": 0,
        "phase_elapsed_sec": 400.0,
        "product_elapsed_sec": 120.0,
        "sec_since_relevant_msg": 10.0,
        "cursor": {
            "talking_point_idx": 2,
            "opening_completed": True,
        },
    }
    outputs = {
        "phase": state.phase.value,
        "current_product_id": state.current_product().product_id
        if state.current_product()
        else None,
        "current_product_index": state.current_product_index,
        "traffic_level": state.traffic.level(state.traffic.msg_rate, state.traffic.msg_rate),
        "products": [
            {
                "product_id": p.product_id,
                "status": p.status.value,
                "is_introduced": p.is_introduced,
                "stage": p.stage,
                "stage_turn_index": p.stage_turn_index,
                "spoken_turns": p.spoken_turns,
                "cluster_count": p.cluster_count,
            }
            for p in state.products
        ],
        "cursor_phase": state.cursor.phase,
    }
    return {"inputs": jsonable(inputs), "outputs": jsonable(outputs)}
