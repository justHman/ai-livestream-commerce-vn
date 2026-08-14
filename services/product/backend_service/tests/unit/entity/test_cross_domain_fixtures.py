"""Unit tests for the cross-domain entity fixtures (task 8.13)."""

from __future__ import annotations

from backend.application.entity.models import EntityDocument
from backend.application.entity.registry import (
    COMMERCE_PRICE_CURRENT,
    COMMERCE_STOCK_AVAILABLE,
    COMMERCE_STOCK_QUANTITY,
    is_volatile,
    resolve_key,
)

from .fixtures import CROSS_DOMAIN_ENTITIES


def test_all_six_verticals_present() -> None:
    assert len(CROSS_DOMAIN_ENTITIES) == 6


def test_every_entity_is_a_product() -> None:
    for entity in CROSS_DOMAIN_ENTITIES:
        assert entity.entity_type == "product"


def test_volatile_price_and_stock_facts_present() -> None:
    for entity in CROSS_DOMAIN_ENTITIES:
        current = entity.get_fact(COMMERCE_PRICE_CURRENT)
        stock = entity.get_fact(COMMERCE_STOCK_QUANTITY)
        assert current is not None and current.freshness == "volatile"
        assert stock is not None and stock.freshness == "volatile"
        assert current.type == "int" and current.value > 0


def test_fact_revisions_are_positive() -> None:
    for entity in CROSS_DOMAIN_ENTITIES:
        for fact in entity.facts:
            assert fact.revision >= 1


def test_custom_real_estate_uses_custom_keys() -> None:
    real_estate = next(
        entity for entity in CROSS_DOMAIN_ENTITIES if entity.id.startswith("product:realestate")
    )
    assert real_estate.get_fact("custom.realestate.bedrooms") is not None
    assert real_estate.get_fact("custom.realestate.area") is not None


def test_custom_real_estate_round_trips() -> None:
    real_estate = next(
        entity for entity in CROSS_DOMAIN_ENTITIES if entity.id.startswith("product:realestate")
    )
    restored = EntityDocument.model_validate(real_estate.model_dump())
    assert restored == real_estate
    assert restored.get_fact("custom.realestate.bedrooms").value == 2
    assert restored.get_fact("custom.realestate.area").value == 63.0


def test_custom_keys_are_volatile_per_registry_lookup() -> None:
    assert resolve_key("Giá hiện tại") == COMMERCE_PRICE_CURRENT
    assert is_volatile(COMMERCE_PRICE_CURRENT)
    assert is_volatile(COMMERCE_STOCK_AVAILABLE)
    assert not is_volatile("custom.realestate.bedrooms")
