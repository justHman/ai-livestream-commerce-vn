"""Tests for query-relevant context rendering (task 8.6)."""

from __future__ import annotations

from backend.application.entity.render import render_entity_context

from .fixtures import FASHION


def test_render_includes_identity_line() -> None:
    text = render_entity_context(FASHION)

    assert text.splitlines()[0] == f"#{FASHION.id} | {FASHION.name} | rev {FASHION.revision}"


def test_render_without_selectors_includes_all_facts() -> None:
    text = render_entity_context(FASHION)

    assert "commerce.price.current: 350000 VND" in text
    assert "custom.fashion.material: Nỉ cotton" in text


def test_render_without_selectors_includes_truncated_blocks() -> None:
    text = render_entity_context(FASHION, max_block_chars=20)

    assert "…" in text
    assert text.splitlines()[-1].startswith("  [kb:fashion-hoodie-heygen-description]")


def test_render_without_selectors_is_never_full_document() -> None:
    text = render_entity_context(FASHION, max_block_chars=400)

    assert "Áo hoodie trơn màu trắng kem" in text
    assert len(text) < 1500


def test_render_with_selectors_includes_only_selected_facts() -> None:
    text = render_entity_context(FASHION, selectors=["commerce.price.current"])

    assert "commerce.price.current: 350000 VND" in text
    assert "custom.fashion.material" not in text


def test_render_with_selectors_excludes_unrelated_blocks() -> None:
    text = render_entity_context(FASHION, selectors=["commerce.price.current"])

    assert "kb:fashion-hoodie-heygen-description" not in text


def test_render_selector_user_label_resolves_via_registry() -> None:
    text = render_entity_context(FASHION, selectors=["Giá hiện tại"])

    assert "commerce.price.current: 350000 VND" in text
    assert "commerce.shipping" not in text


def test_render_volatile_fact_shows_updated_at() -> None:
    text = render_entity_context(FASHION, selectors=["commerce.price.current"])

    assert "updated " in text


def test_render_stable_fact_has_no_freshness_suffix() -> None:
    text = render_entity_context(FASHION, selectors=["commerce.shipping"])

    assert "updated " not in text


def test_render_without_selectors_renders_every_block() -> None:
    text = render_entity_context(FASHION)

    assert text.count("[kb:") == len(FASHION.knowledge_blocks)
