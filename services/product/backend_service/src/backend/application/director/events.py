"""Director events — diagnostics projection + persistence delegation.

Emits frontend-safe WS events (never prompts, customer data, or comment
text) and invokes the injected persistence adapter. The coordinator and
session context delegate here so event shape and the fire-and-forget
persistence policy live in one place.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

__all__ = ["decision_to_event", "speech_item", "persist_decision"]

logger = logging.getLogger(__name__)


def decision_to_event(decision: Any) -> dict:
    """Project a Director Decision to a frontend-friendly WS event payload.

    Drops non-serializable fields and keeps only safe decision metadata —
    never rendered prompt text, shop/product data, or comment text.
    """
    return {
        "turn_id": decision.turn_id,
        "action": decision.action,
        "product": decision.product_id,
        "field": decision.field,
        "stage": decision.stage,
        "task_id": decision.task_id,
        "may_interrupt": decision.may_interrupt,
        "reason": decision.reason,
    }


def speech_item(decision: Any, state: str = "queued") -> dict:
    """Safe speech-plan item for a decision (no prompt/input/score leakage)."""
    return {
        "turn_id": decision.turn_id,
        "state": state,
        "action": decision.action,
        "product_id": decision.product_id,
        "stage": decision.stage,
        "task_id": decision.task_id,
        "revision_token": decision.revision_token,
        "attempt": decision.attempt,
        "script": decision.prepared_script,
        # ponytail: latency_spans kept for diagnostics; prompt/input/score
        # dropped to avoid leaking customer data in WS events.
        "latency_spans": dict(decision.latency_spans),
    }


async def persist_decision(
    pg_store: Any,
    session_id: str,
    decision: Any,
    speech: str,
    *,
    phase: Optional[str] = None,
) -> None:
    """Persist a Director decision row to the runtime DB (fire-and-forget).

    No-op when pg_store is None/disabled. A failure is logged at warning and
    swallowed — a broken runtime DB must never stall the speak loop.
    """
    if pg_store is None or not getattr(pg_store, "enabled", False):
        return
    try:
        await pg_store.insert_director_decision(
            session_id,
            decision.action,
            product_id=decision.product_id,
            score=decision.score,
            phase=phase,
            utterance=speech,
            reason=decision.reason,
        )
    except Exception:
        logger.warning(
            "Postgres persistence failed session=%s operation=insert_director_decision",
            session_id,
        )
