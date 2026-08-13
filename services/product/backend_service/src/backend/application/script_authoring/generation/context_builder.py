"""Authoritative context builder for a generation operation (task 6.1).

A generation prompt needs ONLY the shop/persona/campaign/product/
promotion/fact data its operation may reference — never the full catalog.
``build_authoritative_context`` selects exactly the required slices from a
supplied versioned authoritative-context dict and returns a typed
``AuthoritativeContext``; unknown keys are rejected loudly instead of being
silently dropped. Pure selection — no network, no filesystem, no LLM.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from backend.application.entity.models import EntityDocument
from backend.application.entity.registry import is_volatile

__all__ = [
    "AuthoritativeContext",
    "MissingContextKeyError",
    "build_authoritative_context",
    "build_authoritative_context_from_entity",
]


class AuthoritativeContext(BaseModel):
    """Typed, minimal authoritative context for one generation operation.

    Every field comes from the supplied authoritative-context dict; nothing
    here is invented or inferred. Empty string/tuple means "no authoritative
    value" (an unverified claim is then unsupported).
    """

    shop: dict[str, str] = Field(default_factory=dict)
    persona: dict[str, str] = Field(default_factory=dict)
    campaign: dict[str, str] = Field(default_factory=dict)
    product: dict[str, str] = Field(default_factory=dict)
    promotions: tuple[dict[str, str], ...] = ()
    facts: tuple[dict[str, str], ...] = ()


class MissingContextKeyError(KeyError):
    """Raised when a required authoritative-context key is absent.

    Failing loudly beats generating from partial context: a missing
    product/promotion/fact must never silently become an authoritative
    claim (spec: plan references unknown fact are rejected).
    """


_REQUIRED_TOP_KEYS: tuple[str, ...] = ("shop", "persona", "campaign", "product")


def _extract_str_section(source: dict, key: str) -> dict[str, str]:
    """Extract one string-valued context section; reject non-dict values."""
    raw = source.get(key, {})
    if not isinstance(raw, dict):
        raise MissingContextKeyError(
            f"authoritative context section {key!r} must be a dict, got {type(raw).__name__}"
        )
    return {k: str(v) for k, v in raw.items()}


def _extract_record_sections(source: dict, key: str) -> tuple[dict[str, str], ...]:
    """Extract a list of record sections (promotions/facts); reject garbage."""
    raw = source.get(key, [])
    if not isinstance(raw, list):
        raise MissingContextKeyError(
            f"authoritative context section {key!r} must be a list, got {type(raw).__name__}"
        )
    records: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise MissingContextKeyError(
                f"authoritative context {key!r} entries must be dicts, got {type(item).__name__}"
            )
        records.append({k: str(v) for k, v in item.items()})
    return tuple(records)


def build_authoritative_context(source: dict) -> AuthoritativeContext:
    """Select only the required context slices from ``source``.

    The caller supplies the versioned authoritative-context dict (product
    facts, promotions, persona/brief, shop, campaign). Only the slices a
    generation operation may reference are copied into the returned model;
    unrelated catalog data is never injected into a prompt.

    Raises:
        MissingContextKeyError: a required top-level key is absent or an
            entry has the wrong shape.
    """
    missing = [key for key in _REQUIRED_TOP_KEYS if key not in source]
    if missing:
        raise MissingContextKeyError(
            f"missing required authoritative context key(s): {', '.join(sorted(missing))}"
        )
    return AuthoritativeContext(
        shop=_extract_str_section(source, "shop"),
        persona=_extract_str_section(source, "persona"),
        campaign=_extract_str_section(source, "campaign"),
        product=_extract_str_section(source, "product"),
        promotions=_extract_record_sections(source, "promotions"),
        facts=_extract_record_sections(source, "facts"),
    )


def build_authoritative_context_from_entity(
    entity: EntityDocument,
    shop: Optional[EntityDocument] = None,
    persona_text: str = "",
    campaign: Optional[EntityDocument] = None,
) -> AuthoritativeContext:
    """Build authoritative context from EntityDocuments (task 8.9).

    The entity's facts render into the ``product`` dict; the full facts list
    becomes the ``facts`` records (the planner's authoritative fact IDs —
    each record carries its ``id``); promotion facts become ``promotions``
    records. Shop/persona are required, campaign is optional (in the
    ``_REQUIRED_TOP_KEYS`` spirit — the strict-dict builder keeps them
    required because the dict shape has no notion of absent entities).
    """
    persona = {"text": persona_text} if persona_text else {}
    if shop is None:
        raise MissingContextKeyError("missing required shop entity for authoritative context")
    if entity is None:
        raise MissingContextKeyError("missing required product entity for authoritative context")
    facts = tuple(
        {
            "id": f"{entity.id}:{fact.key}",
            "key": fact.key,
            "value": f"{fact.value} {fact.unit}".rstrip() if fact.unit else str(fact.value),
            "updated_at": fact.updated_at if is_volatile(fact.key) else "",
        }
        for fact in sorted(entity.facts, key=lambda f: f.key)
    )
    promotions = tuple(dict(record) for record in facts if record["key"] == "commerce.promotion")
    return AuthoritativeContext(
        shop={"name": shop.name},
        persona=persona,
        campaign={"name": campaign.name} if campaign is not None else {},
        product={"id": entity.id, "name": entity.name},
        promotions=promotions,
        facts=facts,
    )
