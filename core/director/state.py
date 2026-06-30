"""Director state — phase machine + per-product tracking.

Backend-agnostic orchestration state. The Director decides WHAT the avatar
says and WHEN; the RenderBackend only executes. This module holds the
explicit, inspectable state (no "let the LLM remember from context").

Phases (challenge 2 + 3):
  OPENING  -> time-boxed greeting/engagement, no per-viewer personalization
  SELLING  -> one product at a time (current_product_index)
  CLOSING  -> wrap-up / follow / thanks

ProductState carries ref_image PER product_id (challenge 4: going back to a
previous product must restore the right asset, not a timeline position).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Optional


class Phase(str, enum.Enum):
    OPENING = "opening"
    SELLING = "selling"
    CLOSING = "closing"


class ProductStatus(str, enum.Enum):
    PENDING = "pending"      # not introduced yet
    ACTIVE = "active"        # currently being presented
    DONE = "done"            # already presented (can be revisited)


@dataclass
class ProductState:
    """One product in the sell list."""

    product_id: str
    name: str
    status: ProductStatus = ProductStatus.PENDING
    ref_image: Optional[str] = None        # asset keyed by product_id, NOT timeline
    cluster_count: int = 0                  # Q&A clusters answered for this product
    # Pre-embedded target vector (filled by the embedder); used for retrieval/scoring.
    embedding: Optional[list[float]] = None


@dataclass
class TrafficMode:
    """Realtime traffic signal (challenge 1)."""

    viewer_count: int = 0
    msg_rate: float = 0.0  # msgs/sec over the recent window

    def level(self, low_threshold: float, high_threshold: float) -> str:
        """low | medium | high based on msg_rate."""
        if self.msg_rate < low_threshold:
            return "low"
        if self.msg_rate >= high_threshold:
            return "high"
        return "medium"


@dataclass
class StreamState:
    """Full mutable state of one live session."""

    phase: Phase = Phase.OPENING
    products: list[ProductState] = field(default_factory=list)
    current_product_index: int = 0
    traffic: TrafficMode = field(default_factory=TrafficMode)

    # timers (seconds since session/phase start; injected — not wall-clock here,
    # so the module stays deterministic/testable)
    phase_elapsed_sec: float = 0.0
    product_elapsed_sec: float = 0.0
    sec_since_relevant_msg: float = 0.0

    # Phase B: rolling comments + embedding cache persisted cross-tick.
    # The coordinator merges new comments via add_comments(); Director.decide()
    # reads rolling_comments instead of receiving a fresh list every call.
    rolling_comments: list[Any] = field(default_factory=list)  # list[Comment]
    embeddings_cache: dict[str, list[float]] = field(default_factory=dict)  # comment_id -> vec

    def current_product(self) -> Optional[ProductState]:
        if 0 <= self.current_product_index < len(self.products):
            return self.products[self.current_product_index]
        return None

    def goto_product(self, product_id: str) -> bool:
        """Re-route current_index to a product by id (challenge 4)."""
        for i, p in enumerate(self.products):
            if p.product_id == product_id:
                self.current_product_index = i
                self.product_elapsed_sec = 0.0
                return True
        return False

    def all_products_done(self) -> bool:
        return all(p.status == ProductStatus.DONE for p in self.products)

    # ── Phase B helpers ──────────────────────────────────────────────

    def add_comments(self, new: list[Any]) -> None:
        """Merge new Comment objects into rolling_comments without duplicates.

        Deduplication uses the ``text + t`` pair as identity (Comment has no
        ``id`` field; the IncomingComment id is tracked in embeddings_cache).
        """
        existing = {(c.text, c.t) for c in self.rolling_comments}
        for c in new:
            key = (c.text, c.t)
            if key not in existing:
                self.rolling_comments.append(c)
                existing.add(key)
