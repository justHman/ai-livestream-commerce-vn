"""Suggestion seam tests (tasks 9.5-9.6): the extraction function as a pure seam.

The seam must never raise and never depend on a real engine: a fake engine
(duck-typed ``.generate``) returns structured suggestions from valid JSON,
empty suggestions from malformed JSON or engine failures, and no engine (or
a stub engine) short-circuits to empty.
"""

from __future__ import annotations

import asyncio

from backend.application.entity.extraction import (
    is_stub_llm,
    parse_suggestion_json,
    suggest_facts,
)
from llm.engines.base import LLMResponse


class FakeLLM:
    """Duck-typed engine: returns a canned response text."""

    name = "fake"

    def __init__(self, text: str) -> None:
        self._text = text

    def generate(self, req) -> LLMResponse:
        return LLMResponse(text=self._text)


def test_none_engine_returns_empty_suggestions() -> None:
    result = asyncio.run(suggest_facts(None, "Kem ABC giá 100k.", "product", "custom", ""))

    assert result.suggestions == []
    assert result.note is None


def test_stub_engine_returns_empty_suggestions() -> None:
    from llm.engines.base import _NoopEngine

    result = asyncio.run(
        suggest_facts(_NoopEngine.from_config({}), "text", "product", "custom", "")
    )

    assert result.suggestions == []


def test_is_stub_llm_true_for_noop_engine() -> None:
    from llm.engines.base import _NoopEngine

    assert is_stub_llm(_NoopEngine.from_config({})) is True


def test_is_stub_llm_false_for_real_engine() -> None:
    assert is_stub_llm(FakeLLM("[]")) is False


def test_valid_json_produces_suggestions() -> None:
    llm = FakeLLM(
        '[{"label": "Giá hiện tại", "value": "120000", "unit": "VND"}, '
        '{"label": "Thương hiệu", "value": "ABC", "unit": null}]'
    )
    result = asyncio.run(suggest_facts(llm, "Kem ABC giá 120.000.", "product", "custom", ""))

    assert len(result.suggestions) == 2
    first = result.suggestions[0]
    assert (first.key, first.type, first.value, first.unit) == (
        "commerce.price.current",
        "int",
        "120000",
        "VND",
    )


def test_suggestion_label_is_operator_typed_label() -> None:
    llm = FakeLLM('[{"label": "Giá bán", "value": "99"}]')

    result = asyncio.run(suggest_facts(llm, "text", "product", "custom", ""))

    assert result.suggestions[0].label == "Giá bán"


def test_malformed_json_returns_empty_with_parse_failed_note() -> None:
    llm = FakeLLM("Các facts gồm: khong phai json")

    result = asyncio.run(suggest_facts(llm, "text", "product", "custom", ""))

    assert result.suggestions == []
    assert result.note == "parse_failed"


def test_fenced_json_array_is_parsed() -> None:
    llm = FakeLLM('```json\n[{"label": "Tồn kho", "value": "5"}]\n```')

    result = asyncio.run(suggest_facts(llm, "text", "product", "custom", ""))

    assert result.suggestions[0].key == "commerce.stock.quantity"


def test_leading_prose_before_json_is_skipped() -> None:
    llm = FakeLLM('Đây là kết quả: [{"label": "Giá gốc", "value": "200000"}]')

    result = asyncio.run(suggest_facts(llm, "text", "product", "custom", ""))

    assert result.suggestions[0].key == "commerce.price.original"


def test_unknown_label_suggestion_is_dropped() -> None:
    llm = FakeLLM('[{"label": "Màu sắc", "value": "trắng"}]')

    result = asyncio.run(suggest_facts(llm, "text", "product", "custom", ""))

    assert result.suggestions == []


def test_engine_raising_returns_empty_suggestions() -> None:
    class ExplodingLLM:
        name = "fake"

        def generate(self, req) -> LLMResponse:  # pragma: no cover - always raises
            raise RuntimeError("engine down")

    result = asyncio.run(suggest_facts(ExplodingLLM(), "text", "product", "custom", ""))

    assert result.suggestions == []


def test_non_object_json_entries_are_skipped() -> None:
    llm = FakeLLM('[{"label": "Giá hiện tại", "value": "99"}, "junk", 42]')

    result = asyncio.run(suggest_facts(llm, "text", "product", "custom", ""))

    assert len(result.suggestions) == 1


def test_parse_suggestion_json_returns_none_for_no_array() -> None:
    assert parse_suggestion_json("không có gì") is None
