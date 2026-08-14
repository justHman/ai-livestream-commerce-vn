"""Tests for entity search (task 8.5): match rules + fact-selector filter."""

from __future__ import annotations

from backend.application.entity.search import search_entities

from .fixtures import CROSS_DOMAIN_ENTITIES, CUSTOM_REAL_ESTATE, ELECTRONICS, FASHION


def test_exact_id_match_is_highest_ranked() -> None:
    results = search_entities(CROSS_DOMAIN_ENTITIES, FASHION.id)

    assert results[0].id == FASHION.id


def test_exact_name_match() -> None:
    results = search_entities(CROSS_DOMAIN_ENTITIES, "Tai nghe chống ồn Sony WH-1000XM5")

    assert results == [ELECTRONICS]


def test_name_match_is_diacritic_insensitive() -> None:
    results = search_entities(CROSS_DOMAIN_ENTITIES, "tai nghe chong on sony wh-1000xm5")

    assert results == [ELECTRONICS]


def test_alias_match() -> None:
    results = search_entities(CROSS_DOMAIN_ENTITIES, "Hoodie trắng kem")

    assert results == [FASHION]


def test_alias_match_is_diacritic_insensitive() -> None:
    results = search_entities(CROSS_DOMAIN_ENTITIES, "can ho vinhomes ocean park")

    assert results == [CUSTOM_REAL_ESTATE]


def test_tag_match() -> None:
    results = search_entities(CROSS_DOMAIN_ENTITIES, "đặc sản")

    assert results[0].id == "product:food-ca-phe-robusta-daklak"


def test_entity_type_filter_excludes_other_types() -> None:
    results = search_entities(CROSS_DOMAIN_ENTITIES, "hoodie", entity_type="shop")

    assert results == []


def test_fact_selector_filters_to_entities_with_the_fact() -> None:
    results = search_entities(
        CROSS_DOMAIN_ENTITIES, "hoodie", fact_selectors=["custom.fashion.material"]
    )

    assert results == [FASHION]


def test_fact_selector_user_label_resolves_via_registry() -> None:
    results = search_entities(CROSS_DOMAIN_ENTITIES, "hoodie", fact_selectors=["Giá hiện tại"])

    assert results == [FASHION]


def test_fact_selector_custom_key_matches_real_estate() -> None:
    results = search_entities(
        CROSS_DOMAIN_ENTITIES, "căn hộ", fact_selectors=["custom.realestate.area"]
    )

    assert results == [CUSTOM_REAL_ESTATE]


def test_empty_query_returns_no_results() -> None:
    assert search_entities(CROSS_DOMAIN_ENTITIES, "") == []
