"""Deterministic fixture corpus for the legacy Director (task 1.77).

Fixed seed, fixed clock (``t`` offsets), no randomness in outputs:
comment ids and decision turn_ids are pinned by the caller (ingest helper
always supplies explicit ids; turn_id is stripped from recorded decisions).

Every scenario is an "input contract": given exactly these inputs, the
legacy Director produces exactly these outputs. The canonical backend must
reproduce the recorded output.
"""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Any

from backend.application.director.catalog import Product
from backend.application.director.clustering import Comment
from backend.application.director.config import StreamConfig
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.hooks import HookPool
from backend.application.director.routing import route_comment
from backend.application.director.state import Phase, ProductState, StreamState

from .products import CORPUS_PRODUCTS

# Fixed clock baseline (seconds since session start).
T0 = 100.0
# Fixed seed so any RNG-based future additions stay deterministic.
SEED = 20260805

# Deterministic VN commerce comment corpus (same across all scenarios).
COMMENT_CORPUS: list[dict[str, str]] = [
    {"id": "c-001", "text": "Áo hoodie giá bao nhiêu vậy shop?", "t": "101.0"},
    {"id": "c-002", "text": "Mẫu hoodie có size XL không?", "t": "101.5"},
    {"id": "c-003", "text": "Hoodie này chất liệu gì vậy?", "t": "102.0"},
    {"id": "c-004", "text": "Serum Vitamin C còn hàng không?", "t": "102.5"},
    {"id": "c-005", "text": "Chai vitamin C này nhiêu tiền vậy chị?", "t": "103.0"},
    {"id": "c-006", "text": "Serum này hết chưa ạ?", "t": "103.5"},
    {"id": "c-007", "text": "Chào chị Lan ơi", "t": "104.0"},
    {"id": "c-008", "text": "Click bit.ly/xxx mua bên em", "t": "104.5"},
    {"id": "c-009", "text": "Bóng đá tối nay ai đá vậy?", "t": "105.0"},
    {"id": "c-010", "text": "Áo hoodie còn size XL không ạ?", "t": "106.0"},
    {"id": "c-011", "text": "Freeship không chị?", "t": "107.0"},
    {"id": "c-012", "text": "Màu trắng có hết không?", "t": "108.0"},
]


def corpus_products() -> list[Product]:
    """Deep-copied product catalog (embeddings filled by the caller)."""
    return copy.deepcopy(CORPUS_PRODUCTS)


def corpus_comments(embedder: HashingEmbedder) -> list[Comment]:
    """Deterministic Comment objects with pinned ids and hash embeddings."""
    vectors = embedder.encode([row["text"] for row in COMMENT_CORPUS])
    return [
        Comment(text=row["text"], embedding=vector, t=float(row["t"]), id=row["id"])
        for row, vector in zip(COMMENT_CORPUS, vectors)
    ]


def build_state(
    *,
    phase: Phase = Phase.SELLING,
    current_product_index: int = 0,
    run_plan: Any = None,
) -> StreamState:
    """StreamState over the corpus catalog; timers injected, never wall-clock."""
    products = corpus_products()
    states = [
        ProductState(
            product_id=p.id,
            name=p.name,
            embedding=p.embedding,
            ref_image=p.ref_image,
        )
        for p in products
    ]
    state = StreamState(products=states, run_plan=run_plan)
    state.phase = phase
    state.current_product_index = current_product_index
    state.cursor.phase = phase.value
    state.cursor.profile_revision = 1
    state.cursor.catalog_revision = 1
    return state


def routed_comments(comments: list[Comment], current_product_id: str | None) -> list[Comment]:
    """Route the corpus through the deterministic commerce router."""
    products = corpus_products()
    return [route_comment(c, products, current_product_id) for c in comments]


def jsonable(obj: Any) -> Any:
    """Convert dataclasses/enums/sets/tuples to JSON-safe structures."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if hasattr(obj, "__dataclass_fields__"):
        return {k: jsonable(v) for k, v in asdict(obj).items()}
    if hasattr(obj, "value") and isinstance(obj.value, str):  # enum
        return obj.value
    return obj


def cfg_with_qa_window() -> StreamConfig:
    """Config tuned for a deterministic Q&A window scenario."""
    cfg = StreamConfig()
    cfg.max_qa_clusters_per_window = 2
    cfg.qa_window_hard_timeout_sec = 45.0
    cfg.qa_topic_cooldown_sec = 120.0
    cfg.cluster_merge_threshold = 0.55
    cfg.validate_runtime()
    return cfg


def empty_hook_pool() -> HookPool:
    """HookPool with the built-in default pool (deterministic lines)."""
    return HookPool()
