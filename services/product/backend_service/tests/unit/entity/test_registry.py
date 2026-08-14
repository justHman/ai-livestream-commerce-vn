"""Tests for the Common Fact Registry (tasks 8.2-8.3)."""

from backend.application.entity import (
    COMMERCE_PRICE_CURRENT,
    COMMERCE_PROMOTION,
    COMMERCE_SHIPPING,
    IDENTITY_SKU,
    is_volatile,
    lookup,
    resolve_key,
)
from backend.application.entity.models import EntityDocument, Fact


class TestResolveKey:
    def test_known_label_maps_to_canonical_key(self) -> None:
        assert resolve_key("Giá hiện tại") == COMMERCE_PRICE_CURRENT

    def test_label_matching_ignores_case_and_diacritics(self) -> None:
        assert resolve_key("  GIA HIEN TAI ") == COMMERCE_PRICE_CURRENT

    def test_canonical_key_passes_through(self) -> None:
        assert resolve_key(COMMERCE_PROMOTION) == COMMERCE_PROMOTION

    def test_unknown_label_becomes_custom_key(self) -> None:
        assert resolve_key("Dùng cho da dầu") == "custom.dung-cho-da-dau"


class TestLookup:
    def test_known_key_returns_entry_with_type_and_unit(self) -> None:
        entry = lookup(COMMERCE_PRICE_CURRENT)
        assert entry is not None
        assert entry.type == "int"
        assert entry.freshness == "volatile"
        assert entry.unit == "VND"

    def test_unknown_key_returns_none(self) -> None:
        assert lookup("custom.whatever") is None

    def test_six_volatile_and_four_stable_keys_are_registered(self) -> None:
        volatile = [
            "commerce.price.current",
            "commerce.price.original",
            "commerce.stock.available",
            "commerce.stock.quantity",
            "commerce.promotion",
        ]
        stable = [
            "commerce.shipping",
            "commerce.warranty",
            "identity.brand",
            "identity.sku",
        ]
        assert all(is_volatile(key) for key in volatile)
        assert not any(is_volatile(key) for key in stable)

    def test_sku_entry_is_stable(self) -> None:
        entry = lookup(IDENTITY_SKU)
        assert entry is not None
        assert entry.freshness == "stable"


class TestCustomFacts:
    def test_custom_fact_round_trips_without_schema_change(self) -> None:
        doc = EntityDocument(
            id="entity:product:1",
            entity_type="product",
            name="Kem dưỡng",
            facts=[
                Fact(
                    key=resolve_key("Dùng cho da dầu"),
                    type="str",
                    value="da dầu, da hỗn hợp",
                    labels=["Dùng cho da dầu"],
                )
            ],
        )
        fact = doc.get_fact("custom.dung-cho-da-dau")
        assert fact is not None
        assert fact.value == "da dầu, da hỗn hợp"

        # Round-trip through pydantic's own serialization keeps the custom key.
        reloaded = EntityDocument.model_validate_json(doc.model_dump_json())
        assert reloaded.get_fact("custom.dung-cho-da-dau") is not None

    def test_promotion_is_volatile(self) -> None:
        assert is_volatile(COMMERCE_PROMOTION) is True

    def test_shipping_is_stable(self) -> None:
        assert is_volatile(COMMERCE_SHIPPING) is False
