"""Task 8.9 tests: entity-derived approval-freshness versions.

Any fact revision/updated_at change MUST change ``entity_facts_version``
(the approval-freshness contract); promotion changes MUST change only
``promotion_version``; stable facts excluded from a promotion scope do NOT
change it; identical input is deterministic.
"""

from __future__ import annotations

from backend.application.entity.fingerprints import (
    entity_facts_version,
    promotion_version,
)
from backend.application.entity.models import EntityDocument, Fact

from .fixtures import FASHION


def _entity_with_price(revision: int, updated_at: str) -> EntityDocument:
    return EntityDocument(
        id="product:p1",
        entity_type="product",
        revision=5,
        name="P1",
        facts=[
            Fact(
                key="commerce.price.current",
                type="int",
                value=350000,
                unit="VND",
                revision=revision,
                freshness="volatile",
                updated_at=updated_at,
            ),
        ],
    )


def test_fact_revision_change_changes_facts_version() -> None:
    assert entity_facts_version(_entity_with_price(1, "2026-08-01T00:00:00+00:00")) != (
        entity_facts_version(_entity_with_price(2, "2026-08-01T00:00:00+00:00"))
    )


def test_fact_updated_at_change_changes_facts_version() -> None:
    assert entity_facts_version(_entity_with_price(1, "2026-08-01T00:00:00+00:00")) != (
        entity_facts_version(_entity_with_price(1, "2026-08-02T00:00:00+00:00"))
    )


def test_facts_version_deterministic_for_identical_input() -> None:
    assert entity_facts_version(FASHION) == entity_facts_version(FASHION)
    assert len(entity_facts_version(FASHION)) == 64


def test_promotion_version_changes_with_promotion_fact() -> None:
    changed = FASHION.model_copy(
        deep=True,
        update={
            "facts": [
                Fact(
                    key="commerce.promotion",
                    type="str",
                    value="Giảm 40%",
                    revision=3,
                    freshness="volatile",
                    updated_at="2026-08-02T00:00:00+00:00",
                ),
                *(fact for fact in FASHION.facts if fact.key != "commerce.promotion"),
            ]
        },
    )
    assert promotion_version(FASHION) != promotion_version(changed)


def test_promotion_version_ignores_non_promotion_facts() -> None:
    changed = FASHION.model_copy(
        deep=True,
        update={
            "facts": [
                Fact(
                    key="commerce.price.current",
                    type="int",
                    value=1,
                    unit="VND",
                    revision=99,
                    freshness="volatile",
                    updated_at="2026-09-01T00:00:00+00:00",
                ),
                *(fact for fact in FASHION.facts if fact.key != "commerce.price.current"),
            ]
        },
    )
    assert promotion_version(FASHION) == promotion_version(changed)


def test_promotion_version_empty_entity_is_stable() -> None:
    empty = EntityDocument(
        id="product:no-promo", entity_type="product", revision=1, name="No promo"
    )
    assert promotion_version(empty) == promotion_version(empty)
