"""Event/persistence fixture: events -> persisted state deltas.

Records the deterministic projection of a Decision into the frontend-safe
WS event payload (the persistence row shape) — the exact shape the
coordinator emits and the runtime DB row mirrors. Canonical must reproduce
the same projection from the same Decision.
"""

from __future__ import annotations

from typing import Any

from backend.application.director.decision import Decision

from ..corpus import jsonable


def scenario() -> dict[str, Any]:
    decision = Decision(
        action="answer_fact",
        text="Giá Áo hoodie HeyGen màu trắng: 299.000đ (giá gốc 399.000đ).",
        prompt=None,
        product_id="P004",
        field="price",
        may_interrupt=False,
        reason="top cluster score=2.10",
        score=2.10,
        stage="qa",
        task_id="P004:qa:c-001",
        turn_id="turn-0001",
        cluster_members=("Áo hoodie giá bao nhiêu vậy shop?",),
        cluster_member_ids=("c-001",),
        topic="price",
        decided_at=103.0,
    )

    inputs = {
        "decision": {
            "action": decision.action,
            "product_id": decision.product_id,
            "field": decision.field,
            "stage": decision.stage,
            "task_id": decision.task_id,
            "turn_id": decision.turn_id,
            "may_interrupt": decision.may_interrupt,
            "reason": decision.reason,
            "score": decision.score,
            "cluster_members": list(decision.cluster_members),
            "cluster_member_ids": list(decision.cluster_member_ids),
            "topic": decision.topic,
        }
    }
    outputs = {
        "event": {
            "turn_id": decision.turn_id,
            "action": decision.action,
            "product": decision.product_id,
            "field": decision.field,
            "stage": decision.stage,
            "task_id": decision.task_id,
            "may_interrupt": decision.may_interrupt,
            "reason": decision.reason,
        },
        "persistence_row": {
            "session_id": "sess-001",
            "action": decision.action,
            "product_id": decision.product_id,
            "score": decision.score,
            "phase": "selling",
            "utterance": decision.text,
            "reason": decision.reason,
        },
    }
    return {"inputs": jsonable(inputs), "outputs": jsonable(outputs)}
