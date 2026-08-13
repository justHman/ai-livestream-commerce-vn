"""Universal commerce entity context (cluster C8, tasks 8.1-8.3).

The small common envelope plus the Common Fact Registry: vertical-specific
attributes ride on facts/knowledge blocks instead of schema fields, so new
verticals need no Python/TypeScript changes (spec universal-commerce-entity-context).
"""

from backend.application.entity.models import (
    EntityDocument,
    Fact,
    KnowledgeBlock,
    Relation,
    new_id,
)
from backend.application.entity.registry import (
    COMMERCE_PRICE_CURRENT,
    COMMERCE_PRICE_ORIGINAL,
    COMMERCE_PROMOTION,
    COMMERCE_SHIPPING,
    COMMERCE_STOCK_AVAILABLE,
    COMMERCE_STOCK_QUANTITY,
    COMMERCE_WARRANTY,
    IDENTITY_BRAND,
    IDENTITY_SKU,
    RegistryEntry,
    is_volatile,
    lookup,
    resolve_key,
)

__all__ = [
    "COMMERCE_PRICE_CURRENT",
    "COMMERCE_PRICE_ORIGINAL",
    "COMMERCE_PROMOTION",
    "COMMERCE_SHIPPING",
    "COMMERCE_STOCK_AVAILABLE",
    "COMMERCE_STOCK_QUANTITY",
    "COMMERCE_WARRANTY",
    "IDENTITY_BRAND",
    "IDENTITY_SKU",
    "EntityDocument",
    "Fact",
    "KnowledgeBlock",
    "Relation",
    "RegistryEntry",
    "is_volatile",
    "lookup",
    "new_id",
    "resolve_key",
]
