from backend.api.v1 import ProductEntityIn
from backend.application.entity.registry import COMMERCE_STOCK_AVAILABLE


def test_product_entity_omits_unknown_inventory_fact() -> None:
    entity = ProductEntityIn(id="P1", name="Kem Livento").to_entity()
    assert all(fact.key != COMMERCE_STOCK_AVAILABLE for fact in entity.facts)


def test_product_entity_preserves_observed_inventory_fact() -> None:
    entity = ProductEntityIn(id="P1", name="Kem Livento", in_stock=False).to_entity()
    stock = next(fact for fact in entity.facts if fact.key == COMMERCE_STOCK_AVAILABLE)
    assert stock.value is False
