"""Event-driven fast reducer for the persistent live-demand pipeline (OpenSpec 4).

Public surface of the reducer package: the ``FastReducer`` (one instance per
app, session-scoped state), its typed ``FastReducerConfig`` knobs, the
``AcceptedComment`` item the ingestion pipeline hands it, and the bounded
per-session cluster store (``ClusterStore`` / ``LiveCluster``) that consumes
embedded comments (OpenSpec 5.1-5.2).
"""

from .cluster_store import (
    ClusterStore,
    ClusterStoreConfig,
    LiveCluster,
    ProductCandidate,
    ReconciliationError,
    ReconciliationFailure,
    ReconciliationResult,
)
from .fast_reducer import AcceptedComment, FastReducer, FastReducerConfig

__all__ = [
    "AcceptedComment",
    "FastReducer",
    "FastReducerConfig",
    "LiveCluster",
    "ClusterStore",
    "ClusterStoreConfig",
    "ProductCandidate",
    "ReconciliationError",
    "ReconciliationFailure",
    "ReconciliationResult",
]
