"""core.director — backend-agnostic viewer-comment orchestration.

Sits between viewer comments and the RenderBackend: clusters comments with a VN
bi-encoder, retrieves the live product by vector cosine (no BM25), scores
clusters (phase + intent + size + recency — one scoring function reused for
pick / interrupt-gate / evict), and drives a phase state machine
(opening / selling / closing) with a dashboard-writable StreamConfig.

Public API:
  Director, Decision        — the FSM and its per-cycle output
  StreamState, ProductState, Phase, ProductStatus, TrafficMode
  StreamConfig              — dashboard-tunable policy
  HookPool                  — pre-generated engagement lines (hybrid traffic-mode)
  Comment, cluster_comments — clustering primitives
  build_embedder, cosine    — embeddings
"""

from .catalog import Product, ProductVariant, route_intent_to_field
from .cluster import Comment, Cluster, cluster_comments
from .config import StreamConfig
from .director import Decision, Director
from .embedder import build_embedder, cosine
from .hooks import HookPool
from .runtime import DirectorRuntime
from .scorer import rank_clusters, retrieve_product
from .state import (
    Phase,
    ProductState,
    ProductStatus,
    StreamState,
    TrafficMode,
)

__all__ = [
    "Director",
    "Decision",
    "DirectorRuntime",
    "StreamState",
    "ProductState",
    "ProductStatus",
    "Phase",
    "TrafficMode",
    "StreamConfig",
    "HookPool",
    "Product",
    "ProductVariant",
    "route_intent_to_field",
    "Comment",
    "Cluster",
    "cluster_comments",
    "rank_clusters",
    "retrieve_product",
    "build_embedder",
    "cosine",
]
