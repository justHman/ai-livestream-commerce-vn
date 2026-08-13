"""Evidence planner and cache (cluster C10).

Public surface consumed by the Agent runtime and by cluster C8's entity
repository: typed requests/bundles (``models``), the repository protocol
(``repository``), the revision+TTL cache (``cache``), and the cache-first
planner (``planner``).
"""

from backend.application.evidence.cache import CacheStatus, EvidenceCache
from backend.application.evidence.models import (
    EntityDocumentView,
    EntityRef,
    EvidenceBundle,
    EvidenceConfig,
    EvidenceDiagnostics,
    EvidenceRequest,
    EvidenceResult,
    Fact,
    FreshnessPolicy,
    VOLATILE_SELECTORS,
)
from backend.application.evidence.planner import EvidencePlanner
from backend.application.evidence.repository import EntityRepository

__all__ = [
    "CacheStatus",
    "EntityDocumentView",
    "EntityRef",
    "EntityRepository",
    "EvidenceBundle",
    "EvidenceCache",
    "EvidenceConfig",
    "EvidenceDiagnostics",
    "EvidencePlanner",
    "EvidenceRequest",
    "EvidenceResult",
    "Fact",
    "FreshnessPolicy",
    "VOLATILE_SELECTORS",
]
