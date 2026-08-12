"""Authoritative gate context passed to every rule checker.

Rules are pure: ``check(text, context)`` where ``context`` is the
authoritative product/shop/persona data (task 3.7) plus deterministic
configuration (thresholds, allowlists, house style). Nothing here touches
the network or an LLM; a missing fact is a missing fact, never a lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "ProductFacts",
    "ScriptGateContext",
    "TransitionPolicy",
]

TransitionPolicy = Literal["ORDER_AWARE", "ORDER_AGNOSTIC"]


@dataclass(frozen=True)
class ProductFacts:
    """Authoritative facts a script MAY claim (task 3.7).

    ``prices`` and ``discounts`` are the only allowed numeric claims;
    ``product_names``/``skus`` are the only allowed identity references;
    ``allowed_claims`` are exact claim sentences known to be true.
    An empty list means "no authoritative value" (the matching rule then
    flags any occurrence, since an unverified claim is unsupported).
    """

    product_name: str = ""
    prices: tuple[str, ...] = ()
    discounts: tuple[str, ...] = ()
    skus: tuple[str, ...] = ()
    allowed_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScriptGateContext:
    """Everything a deterministic rule needs besides the checked text.

    Defaults are safe for a manual-draft workflow with no authoritative
    facts loaded: thresholds are lenient, allowlists empty, house style
    strict (em-dash rejected). ``transition_policy`` only matters to the
    full-script transition rule.
    """

    facts: ProductFacts = field(default_factory=ProductFacts)
    brand_allowlist: tuple[str, ...] = ()
    allow_em_dash: bool = False
    max_cta_per_segment: int = 3
    # Target spoken duration bounds, seconds (segment scope).
    target_min_seconds: float = 10.0
    target_max_seconds: float = 180.0
    # Target spoken duration bounds for the full compiled script, seconds.
    total_min_seconds: float = 300.0
    total_max_seconds: float = 3600.0
    transition_policy: TransitionPolicy = "ORDER_AGNOSTIC"
    other_product_names: tuple[str, ...] = ()
    # Required fact/topic coverage at full-script scope (from the plan).
    required_topics: tuple[str, ...] = ()
