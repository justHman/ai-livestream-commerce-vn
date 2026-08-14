"""Common Fact Registry (task 8.2): canonical keys, aliases, freshness policy.

The registry maps user labels (e.g. "Giá hiện tại") and canonical keys to
typed metadata. It is a static in-memory table — the codebase has no need for
a dynamic/DB-backed registry, and hardcoding keeps the policy queryable by
later clusters (8.6 evidence rendering, 8.9 approval freshness).

Unknown labels are never rejected: they resolve to ``custom.<slug>`` keys
(Decision 11), so vertical-specific attributes need no schema change.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Optional

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
    "RegistryEntry",
    "is_volatile",
    "lookup",
    "resolve_key",
]

Freshness = Literal["stable", "volatile"]
FactType = Literal["int", "float", "str", "bool"]

COMMERCE_PRICE_CURRENT = "commerce.price.current"
COMMERCE_PRICE_ORIGINAL = "commerce.price.original"
COMMERCE_STOCK_AVAILABLE = "commerce.stock.available"
COMMERCE_STOCK_QUANTITY = "commerce.stock.quantity"
COMMERCE_PROMOTION = "commerce.promotion"
COMMERCE_SHIPPING = "commerce.shipping"
COMMERCE_WARRANTY = "commerce.warranty"
IDENTITY_BRAND = "identity.brand"
IDENTITY_SKU = "identity.sku"

_CUSTOM_PREFIX = "custom."


@dataclass(frozen=True)
class RegistryEntry:
    """Expected shape of one canonical fact key (Decision 11).

    ``freshness`` is consumed by later clusters (evidence cache TTL vs
    revision-scoped stable facts); ``labels`` are the Vietnamese user-facing
    names the workbench maps to this key.
    """

    key: str
    type: FactType
    freshness: Freshness
    unit: Optional[str] = None
    labels: tuple[str, ...] = ()


# One entry per canonical key. Aliases are intentionally NOT canonicalized
# (case/whitespace/diacritics kept) — matching normalizes on lookup instead,
# so the table stays readable.
_REGISTRY: tuple[RegistryEntry, ...] = (
    RegistryEntry(
        COMMERCE_PRICE_CURRENT,
        type="int",
        freshness="volatile",
        unit="VND",
        labels=("Giá hiện tại", "Giá bán", "Giá", "Price"),
    ),
    RegistryEntry(
        COMMERCE_PRICE_ORIGINAL,
        type="int",
        freshness="volatile",
        unit="VND",
        labels=("Giá gốc", "Giá niêm yết", "Giá trước giảm", "Original price"),
    ),
    RegistryEntry(
        COMMERCE_STOCK_AVAILABLE,
        type="bool",
        freshness="volatile",
        labels=("Còn hàng", "Hết hàng", "Còn không"),
    ),
    RegistryEntry(
        COMMERCE_STOCK_QUANTITY,
        type="int",
        freshness="volatile",
        unit="items",
        labels=("Số lượng tồn", "Tồn kho", "Còn bao nhiêu cái"),
    ),
    RegistryEntry(
        COMMERCE_PROMOTION,
        type="str",
        freshness="volatile",
        labels=("Khuyến mãi", "Ưu đãi", "Sale", "Voucher", "Deal"),
    ),
    RegistryEntry(
        COMMERCE_SHIPPING,
        type="str",
        freshness="stable",
        labels=("Vận chuyển", "Giao hàng", "Phí ship", "Shipping"),
    ),
    RegistryEntry(
        COMMERCE_WARRANTY,
        type="str",
        freshness="stable",
        labels=("Bảo hành", "Đổi trả", "Warranty"),
    ),
    RegistryEntry(
        IDENTITY_BRAND,
        type="str",
        freshness="stable",
        labels=("Thương hiệu", "Hãng", "Brand"),
    ),
    RegistryEntry(
        IDENTITY_SKU,
        type="str",
        freshness="stable",
        labels=("Mã sản phẩm", "Mã SKU", "SKU"),
    ),
)

# Volatile facts (price/stock/promotion) carry exact numbers that can change
# mid-live; evidence cache must invalidate them fast. Stable facts (shipping,
# warranty, brand, sku) change rarely and are revision-scoped.
_VOLATILE_KEYS = frozenset(entry.key for entry in _REGISTRY if entry.freshness == "volatile")

# Canonical key -> registry entry.
_ENTRY_BY_KEY: dict[str, RegistryEntry] = {entry.key: entry for entry in _REGISTRY}


def _normalize(text: str) -> str:
    """Fold case, whitespace, and diacritics for alias matching.

    Keeps the table human-readable (no pre-folded keys) while letting
    "giá hiện tại" and "Giá hiện tại" resolve to the same key.
    """
    folded = unicodedata.normalize("NFD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", folded).strip().lower()


def _slugify(text: str) -> str:
    """Make a safe ``custom.<slug>`` key from an arbitrary label."""
    slug = re.sub(r"[^a-z0-9]+", "-", _normalize(text)).strip("-")
    return f"{_CUSTOM_PREFIX}{slug or 'unknown'}"


def resolve_key(label: str) -> str:
    """Map a user label (or canonical key) to a canonical key.

    Known aliases resolve to their canonical key; anything unknown becomes a
    valid ``custom.<slug>`` key. Canonical and custom keys pass through
    unchanged, so resolution is idempotent.
    """
    normalized = _normalize(label)
    if normalized in _LABEL_INDEX:
        return _LABEL_INDEX[normalized]
    if normalized in _ENTRY_BY_KEY:
        return normalized
    if normalized.startswith(_CUSTOM_PREFIX):
        return normalized
    return _slugify(label)


def lookup(key: str) -> Optional[RegistryEntry]:
    """Return the registry entry for a canonical key, or None for unknown keys."""
    return _ENTRY_BY_KEY.get(key)


def is_volatile(key: str) -> bool:
    """True when the key is a volatile fact (TTL/refresh invalidates it)."""
    return key in _VOLATILE_KEYS


# User label (normalized) -> canonical key. Built after ``_normalize`` so the
# module-level index construction reads top-down.
_LABEL_INDEX: dict[str, str] = {}
for entry in _REGISTRY:
    for label in entry.labels:
        _LABEL_INDEX[_normalize(label)] = entry.key
