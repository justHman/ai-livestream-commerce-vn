"""Tests for the universal entity document models (task 8.1)."""

import pytest
from pydantic import ValidationError

from backend.application.entity.models import (
    EntityDocument,
    Fact,
    KnowledgeBlock,
    Relation,
    new_id,
)


class TestFact:
    def test_accepts_each_supported_value_type(self) -> None:
        for type_, value in (("int", 3), ("float", 2.5), ("str", "x"), ("bool", True)):
            fact = Fact(key="custom.x", type=type_, value=value)
            assert fact.type == type_
            assert fact.value == value

    def test_rejects_unknown_type(self) -> None:
        with pytest.raises(ValidationError):
            Fact(key="custom.x", type="date", value="2026-08-14")


class TestKnowledgeBlock:
    def test_defaults(self) -> None:
        block = KnowledgeBlock(id="kb:1", content="dùng 2 lần mỗi ngày")
        assert block.kind == "custom"
        assert block.revision == 1

    def test_rejects_empty_content(self) -> None:
        with pytest.raises(ValidationError):
            KnowledgeBlock(id="kb:1", content="")


class TestRelation:
    def test_defaults(self) -> None:
        rel = Relation(target_entity_id="entity:shop:1")
        assert rel.relation_type == "custom"


class TestEntityDocument:
    def test_accepts_facts_blocks_relations(self) -> None:
        doc = EntityDocument(
            id="entity:product:1",
            entity_type="product",
            name="Áo thun",
            facts=[Fact(key="commerce.price.current", type="int", value=99_000)],
            knowledge_blocks=[KnowledgeBlock(id="kb:1", content="chất liệu cotton thoáng mát")],
            relations=[Relation(target_entity_id="entity:shop:1")],
        )
        assert doc.revision == 0
        assert doc.get_fact("commerce.price.current") is not None

    def test_get_fact_returns_none_when_absent(self) -> None:
        doc = EntityDocument(id="entity:product:1", entity_type="product", name="Áo thun")
        assert doc.get_fact("commerce.price.current") is None


def test_new_id_has_prefix_and_unique_suffix() -> None:
    a = new_id("entity")
    b = new_id("entity")
    assert a.startswith("entity:")
    assert b.startswith("entity:")
    assert a != b
