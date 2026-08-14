"""Task 18.7 guards: image context never carries control-plane material.

Positive guard runs on the real corpus + harness source; negative tests use
SIMULATED corpus files (tmp_path) — never the real tree (same pattern as
``test_envelope_boundary_guards``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .context_compression_benchmark.benchmark_runner import CORPUS_PATH
from .context_compression_benchmark.guards import (
    INSTRUCTION_MARKERS,
    TOOL_SCHEMA_MARKERS,
    VOLATILE_FACT_KEYS,
    assert_image_context_safe,
    forbidden_marker,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"
HARNESS_SOURCE = Path(__file__).parent / "context_compression_benchmark" / "benchmark_runner.py"


def _write_corpus(root: Path, descriptive_text: str) -> Path:
    payload = {
        "schema": "vi-context-compression-corpus",
        "version": 1,
        "provenance": {
            "authored_synthetic": True,
            "contains_pii": False,
            "factual_ground_truth": False,
        },
        "task_classes": ["grounding"],
        "descriptive_context": [
            {"id": "desc-bad", "kind": "long_description", "text": descriptive_text}
        ],
        "fixtures": [
            {
                "id": "fix-001",
                "task_class": "grounding",
                "question": "q",
                "answer": "a",
                "evidence": ["desc-bad"],
            }
        ],
    }
    path = root / "corpus.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# -- marker scanner --------------------------------------------------------------


def test_forbidden_marker_returns_none_for_clean_text() -> None:
    assert forbidden_marker("Vải canvas dày, phủ lớp chống thấm nhẹ.") is None


@pytest.mark.parametrize("marker", TOOL_SCHEMA_MARKERS)
def test_forbidden_marker_detects_tool_schema_markers(marker: str) -> None:
    assert forbidden_marker(f"mô tả {marker} của hệ thống") == marker


@pytest.mark.parametrize("marker", INSTRUCTION_MARKERS)
def test_forbidden_marker_detects_instruction_markers(marker: str) -> None:
    assert forbidden_marker(f"nội dung {marker}") == marker


@pytest.mark.parametrize("key", VOLATILE_FACT_KEYS)
def test_forbidden_marker_detects_volatile_fact_keys(key: str) -> None:
    assert forbidden_marker(f"giá {key} hôm nay") == key


def test_forbidden_marker_detects_marker_case_insensitively() -> None:
    assert forbidden_marker("Response Schema JSON") == "response schema"


# -- positive guard on the real tree ---------------------------------------------


def test_guard_passes_on_real_corpus_and_harness() -> None:
    assert_image_context_safe(CORPUS_PATH, HARNESS_SOURCE)


# -- negative guards on simulated corpora ----------------------------------------


def test_guard_fails_on_tool_schema_in_descriptive_text(tmp_path) -> None:
    corpus = _write_corpus(tmp_path, "Mô tả tool schema JSON đầy đủ của hệ thống.")
    with pytest.raises(RuntimeError, match="tool schema"):
        assert_image_context_safe(corpus)


def test_guard_fails_on_response_schema_in_descriptive_text(tmp_path) -> None:
    corpus = _write_corpus(tmp_path, "Kết quả trả về theo response schema đã định.")
    with pytest.raises(RuntimeError, match="response schema"):
        assert_image_context_safe(corpus)


def test_guard_fails_on_instruction_hierarchy_in_descriptive_text(tmp_path) -> None:
    corpus = _write_corpus(tmp_path, "Tuân theo instruction hierarchy của hệ thống.")
    with pytest.raises(RuntimeError, match="instruction hierarchy"):
        assert_image_context_safe(corpus)


def test_guard_fails_on_volatile_fact_in_descriptive_text(tmp_path) -> None:
    corpus = _write_corpus(tmp_path, "Giá hiện tại và stock còn lại cập nhật liên tục.")
    with pytest.raises(RuntimeError, match="stock"):
        assert_image_context_safe(corpus)


def test_guard_ignores_volatile_word_in_fixture_answers(tmp_path) -> None:
    # Tool names and answer values are scoring ground truth, never context;
    # a volatile-fact key in an answer must not fail the guard.
    corpus = _write_corpus(tmp_path, "Mô tả dài yên tĩnh về sản phẩm.")
    payload = json.loads(corpus.read_text(encoding="utf-8"))
    payload["fixtures"][0]["answer"] = "promotion_lookup"
    corpus.write_text(json.dumps(payload), encoding="utf-8")
    assert_image_context_safe(corpus)


# -- negative guard on simulated harness source -----------------------------------


def test_guard_fails_on_forbidden_marker_in_source(tmp_path) -> None:
    corpus = _write_corpus(tmp_path, "Mô tả dài yên tĩnh về sản phẩm.")
    source = tmp_path / "harness.py"
    source.write_text(
        'def render() -> str:\n    return "system instruction được nhúng vào ảnh"\n',
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="system instruction"):
        assert_image_context_safe(corpus, source)


def test_guard_ignores_comments_in_source(tmp_path) -> None:
    corpus = _write_corpus(tmp_path, "Mô tả dài yên tĩnh về sản phẩm.")
    source = tmp_path / "harness.py"
    source.write_text(
        '# TODO: "system instruction" mẫu tham khảo\n"vải canvas dày"\n',
        encoding="utf-8",
    )
    assert_image_context_safe(corpus, source)
