"""Data Studio form conversion tests (tasks 9.1-9.4): rows -> typed facts.

Pure conversion tests, no HTTP: FactRowIn -> Fact mapping (known labels via
the registry, unknown labels preserved as custom facts), common-field
coercion, KnowledgeBlockIn -> KnowledgeBlock, and full-replace save
semantics on ``to_entity`` (create rev 1, update rev stored+1, per-fact and
per-block revision reset).
"""

from __future__ import annotations

import pytest

from backend.api.v1.entities import (
    FactRowIn,
    KnowledgeBlockIn,
    SimpleEntityUpsertReq,
)
from backend.application.entity.extraction import (
    coerce_value,
    fact_from_common,
    fact_from_row,
)

from .fixtures import FASHION


def test_known_label_resolves_to_canonical_int_fact() -> None:
    fact = fact_from_row("Giá hiện tại", "350000", None)

    assert (fact.key, fact.type, fact.value) == ("commerce.price.current", "int", 350000)


def test_known_label_keeps_registry_unit_and_freshness() -> None:
    fact = fact_from_row("Giá hiện tại", "350000", None)

    assert (fact.unit, fact.freshness) == ("VND", "volatile")


def test_row_unit_overrides_registry_unit() -> None:
    fact = fact_from_row("Giá hiện tại", "350000", "VNĐ")

    assert fact.unit == "VNĐ"


def test_known_label_keeps_row_labels_and_source() -> None:
    fact = fact_from_row("Giá hiện tại", "350000", None)

    assert (fact.labels, fact.source) == (["Giá hiện tại"], "datastudio")


def test_unknown_label_becomes_custom_key() -> None:
    fact = fact_from_row("Dùng cho da dầu", "có", None)

    assert fact.key == "custom.dung-cho-da-dau"


def test_unknown_label_non_numeric_value_is_stored_as_str() -> None:
    fact = fact_from_row("Dùng cho da dầu", "có", None)

    assert (fact.type, fact.value) == ("str", "có")


def test_unknown_label_int_value_stays_int() -> None:
    fact = fact_from_row("Số tầng", "12", None)

    assert (fact.type, fact.value) == ("int", 12)


def test_unknown_label_float_value_with_unit_stays_float() -> None:
    fact = fact_from_row("Trọng lượng", "1.5", "kg")

    assert (fact.type, fact.value, fact.unit) == ("float", 1.5, "kg")


def test_unknown_label_uncoercible_value_falls_back_to_str() -> None:
    fact = fact_from_row("Trọng lượng", "nhẹ", "kg")

    assert (fact.type, fact.value) == ("str", "nhẹ")


def test_canonical_int_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        fact_from_row("Giá hiện tại", "đắt", None)


def test_common_stock_available_khong_coerces_false() -> None:
    fact = fact_from_common("commerce.stock.available", "không")

    assert fact.value is False


def test_common_stock_available_co_true_coerces_true() -> None:
    fact = fact_from_common("commerce.stock.available", "có")

    assert fact.value is True


def test_common_brand_uses_registry_first_label() -> None:
    fact = fact_from_common("identity.brand", "HeyGen")

    assert (fact.key, fact.labels, fact.type) == ("identity.brand", ["Thương hiệu"], "str")


def test_common_price_coerces_int_with_registry_unit() -> None:
    fact = fact_from_common("commerce.price.current", "350000")

    assert (fact.value, fact.unit, fact.freshness) == (350000, "VND", "volatile")


def test_common_price_bad_value_raises() -> None:
    with pytest.raises(ValueError):
        fact_from_common("commerce.price.current", "rẻ")


def test_common_stock_quantity_int_coercion() -> None:
    fact = fact_from_common("commerce.stock.quantity", "12")

    assert (fact.value, fact.unit) == (12, "items")


def test_bool_true_strings() -> None:
    assert coerce_value("1", "bool", key="x") is True
    assert coerce_value("TRUE", "bool", key="x") is True


def test_bool_false_strings() -> None:
    assert coerce_value("0", "bool", key="x") is False
    assert coerce_value("không", "bool", key="x") is False


def test_to_entity_create_gets_revision_1() -> None:
    req = SimpleEntityUpsertReq(id="p1", name="SP 1")

    entity = req.to_entity(None)

    assert entity.revision == 1


def test_to_entity_update_gets_stored_revision_plus_one() -> None:
    req = SimpleEntityUpsertReq(id="p1", name="SP 1 mới")

    entity = req.to_entity(FASHION)

    assert entity.revision == FASHION.revision + 1


def test_to_entity_replaces_facts_and_resets_their_revision() -> None:
    req = SimpleEntityUpsertReq(
        id="p1",
        name="SP 1",
        fact_rows=[FactRowIn(label="Giá hiện tại", value="100000", unit=None)],
    )

    entity = req.to_entity(FASHION)

    assert len(entity.facts) == 1
    assert entity.facts[0].revision == 1


def test_to_entity_replaces_blocks_and_resets_their_revision() -> None:
    req = SimpleEntityUpsertReq(
        id="p1",
        name="SP 1",
        knowledge_blocks=[
            KnowledgeBlockIn(kind="description", title="Mô tả", content="Kem dưỡng ẩm."),
        ],
    )

    entity = req.to_entity(FASHION)

    assert len(entity.knowledge_blocks) == 1
    assert entity.knowledge_blocks[0].revision == 1


def test_to_entity_preserves_stored_relations() -> None:
    req = SimpleEntityUpsertReq(id="p1", name="SP 1")

    entity = req.to_entity(FASHION)

    assert entity.relations == FASHION.relations


def test_to_entity_block_ids_generated_with_block_prefix() -> None:
    req = SimpleEntityUpsertReq(
        id="p1",
        name="SP 1",
        knowledge_blocks=[
            KnowledgeBlockIn(kind="usage", title="Cách dùng", content="Thoa sau khi rửa mặt."),
        ],
    )

    entity = req.to_entity(None)

    assert entity.knowledge_blocks[0].id.startswith("block:")
