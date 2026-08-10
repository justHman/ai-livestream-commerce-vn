"""Contract tests for the versioned VieNeu benchmark corpus (OpenSpec 8.1-8.2).

Validates schema/version, provenance metadata, item schema and types,
stable and unique IDs, unique exact texts, declared category consistency
and coverage, corpus size, loader rejection of malformed corpus files,
delivery form key set/order, exact codepoint and UTF-8 byte
reconstruction, maximal whitespace-run tokenization, provider multi-word
coalescing, determinism, immutability of loaded records, and no-empty /
short / whitespace-only edge cases. Pure stdlib, no backend imports.
"""

from __future__ import annotations

import json
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from .benchmark_fixtures.fragments import (
    CORPUS_PATH,
    SCHEMA,
    VERSION,
    Utterance,
    character_fragments,
    fragment_deliveries,
    full_fragments,
    load_by_category,
    load_categories,
    load_texts,
    load_utterances,
    provider_like_fragments,
    word_fragments,
)

EXPECTED_CATEGORIES = (
    "short_conversational",
    "long_conversational",
    "clauses",
    "multi_sentence_paragraph",
    "prices_currency_percent",
    "numbers_decimal_grouped",
    "product_names_skus",
    "acronyms",
    "mixed_vi_en",
    "complete_script",
)
CATEGORY_COUNT = 4
CORPUS_COUNT = len(EXPECTED_CATEGORIES) * CATEGORY_COUNT
ID_PATTERN = r"[a-z]+-\d{3}"
DELIVERY_FORMS = ("full", "character", "word", "provider_like")
SHORT_TEXTS = ("", " ", "  ", "\t", "\n", "\t\n ")
WHITESPACE_ONLY_TEXTS = (" ", "  ", "\t", "\n", "\t\n ")
PROVIDER_PATTERN_SAMPLE = "Một hai ba bốn năm sáu bảy"
PROVIDER_PATTERN_DELTAS = ["Một hai ba", " bốn", " năm sáu", " bảy"]

# Edge forms that must actually occur in the authored corpus (finding 7).
EDGE_FORM_RE = {
    "balanced quote": r"“[^”]*”",
    "balanced parentheses": r"\([^)]*\)",
    "reserved URL": r"https://shop\.example\.invalid/\S*",
    "reserved email": r"hotro@example\.invalid",
    "newline": r"\n",
    "repeated spaces": r" {2}",
    "tab": r"\t",
    "decimal number": r"\d+,\d+",
    "grouped number": r"\d{1,3}(?:\.\d{3})+",
    "price": r"\d{3}\.\d{3}(?:\.\d{3})?đ",
    "currency word": r"\d+(?:\.\d{3})* đồng",
    "percent": r"\d+%",
    "fictional product name": r"“NovaWarm X2”|AirPure 3\.0",
    "SKU": r"SKU-[A-Z]{2}-\d{4}|CB-2026|KM-88",
    "acronym": r"\bWiFi\b|\bHEPA\b|\bQR\b|\bNFC\b|\bSD\b|\bUSB-C\b|\b4K\b|\bAI\b",
    "mixed VI/EN": r"\b(app|sale|up to|quality|online|website|Android|iOS)\b",
}


# ---------- 8.1 corpus schema ----------


def _corpus_data() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


def test_corpus_json_parses_and_matches_schema_version() -> None:
    data = _corpus_data()
    assert data["schema"] == SCHEMA
    assert data["version"] == VERSION
    assert tuple(data["categories"]) == EXPECTED_CATEGORIES
    assert data["categories"] == load_categories()


def test_corpus_has_exact_authored_size() -> None:
    utterances = load_utterances()
    assert len(utterances) == CORPUS_COUNT == 40
    assert len(set(record.id for record in utterances)) == CORPUS_COUNT
    assert len(set(record.text for record in utterances)) == CORPUS_COUNT


def test_every_category_has_exactly_four_utterances() -> None:
    by_category = load_by_category()
    assert tuple(by_category) == EXPECTED_CATEGORIES
    for category in EXPECTED_CATEGORIES:
        assert len(by_category[category]) == CATEGORY_COUNT
        assert all(record.category == category for record in by_category[category])


def test_load_by_category_keeps_original_json_order() -> None:
    utterances = load_utterances()
    by_category = load_by_category()
    for category, records in by_category.items():
        assert [record.id for record in records] == [
            record.id for record in utterances if record.category == category
        ]


def test_load_utterances_preserves_exact_raw_json_order(tmp_path: Path) -> None:
    data = _base_corpus()
    original_ids = [item["id"] for item in data["utterances"]]
    interleaved = []
    for first, second in zip(data["utterances"][:20], data["utterances"][20:]):
        interleaved.append(second)
        interleaved.append(first)
    data["utterances"] = interleaved
    assert [item["id"] for item in interleaved] != original_ids
    assert [record.id for record in load_utterances(_write_corpus(tmp_path, data))] == [
        item["id"] for item in interleaved
    ]


def test_utterances_are_immutable_typed_records() -> None:
    records = load_utterances()
    assert all(isinstance(record, Utterance) for record in records)
    assert all(isinstance(record.id, str) for record in records)
    with pytest.raises(FrozenInstanceError):
        records[0].id = "short-999"


def test_utterance_ids_match_stable_pattern() -> None:
    assert all(re.fullmatch(ID_PATTERN, record.id) for record in load_utterances())


def test_corpus_size_is_reasonable() -> None:
    assert CORPUS_PATH.stat().st_size < 64 * 1024


# ---------- provenance and coverage ----------


def test_provenance_metadata_is_truthful() -> None:
    data = _corpus_data()
    provenance = data["provenance"]
    assert provenance == {
        "authored_synthetic": True,
        "contains_pii": False,
        "factual_ground_truth": False,
        "purpose": "benchmark fixture for adaptive speech-text chunking",
    }
    assert data["created_by"].startswith("ai-livestream-commerce-vn backend tests")


@pytest.mark.parametrize(
    "mutations",
    [
        {"authored_synthetic": False},
        {"contains_pii": True},
        {"factual_ground_truth": True},
        {"authored_synthetic": "true"},
    ],
)
def test_loader_rejects_wrong_provenance_values(tmp_path: Path, mutations: dict) -> None:
    data = _base_corpus()
    data["provenance"].update(mutations)
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


@pytest.mark.parametrize(
    "provenance",
    [
        {},
        {"purpose": "benchmark fixture for adaptive speech-text chunking"},
        {"authored_synthetic": True},
        ["authored_synthetic", "contains_pii", "factual_ground_truth"],
        "authored",
        None,
    ],
)
def test_loader_rejects_missing_or_non_dict_provenance(tmp_path: Path, provenance) -> None:
    data = _base_corpus()
    data["provenance"] = provenance
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


def test_no_real_brands_and_no_pii() -> None:
    corpus_text = " ".join(load_texts())
    for banned in ("HeyGen", "La Roche-Posay", "GoldenCare", "RelaxPro"):
        assert banned not in corpus_text


def test_every_edge_form_occurs_in_the_corpus() -> None:
    corpus_text = " ".join(load_texts())
    missing = [
        name for name, pattern in EDGE_FORM_RE.items() if not re.search(pattern, corpus_text)
    ]
    assert not missing, f"edge forms missing from corpus: {missing}"


# ---------- loader validation ----------


def _write_corpus(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _base_corpus() -> dict:
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "mutations",
    [
        {"schema": "other"},
        {"version": 2},
        {"utterances": []},
        {"utterances": None},
        {"categories": []},
        {"categories": None},
    ],
)
def test_loader_rejects_wrong_schema_version_or_missing_utterances(
    tmp_path: Path, mutations: dict
) -> None:
    data = _base_corpus()
    data.update(mutations)
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


def test_loader_rejects_non_list_utterances(tmp_path: Path) -> None:
    data = _base_corpus()
    data["utterances"] = {"id": "short-001"}
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


@pytest.mark.parametrize(
    "mutations",
    [
        {"schema": "other", "version": 2},
        {"utterances": "not a list"},
        {"utterances": [1, 2, 3]},
    ],
)
def test_loader_rejects_wrong_schema_version_or_bad_utterance_items(
    tmp_path: Path, mutations: dict
) -> None:
    data = _base_corpus()
    data.update(mutations)
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


def test_loader_rejects_non_object_top_level(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(["a", "b", "c"], ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        load_utterances(path)


@pytest.mark.parametrize(
    "categories",
    [
        [],
        None,
        [None],
        [5],
        [""],
        ["  "],
        ["clauses", "clauses"],
    ],
)
def test_loader_rejects_blank_duplicate_or_non_string_categories(
    tmp_path: Path, categories
) -> None:
    data = _base_corpus()
    data["categories"] = categories
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


@pytest.mark.parametrize(
    "mutated",
    [
        {"id": "", "category": "clauses", "text": "Nội dung hợp lệ."},
        {"id": "   ", "category": "clauses", "text": "Nội dung hợp lệ."},
        {"id": "CLAU-001", "category": "clauses", "text": "Nội dung hợp lệ."},
        {"id": "clauses", "category": "clauses", "text": "Nội dung hợp lệ."},
        {"id": "clause-01", "category": "clauses", "text": "Nội dung hợp lệ."},
    ],
)
def test_loader_rejects_blank_or_malformed_ids(tmp_path: Path, mutated: dict) -> None:
    data = _base_corpus()
    data["utterances"] = [mutated]
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


@pytest.mark.parametrize(
    "mutated",
    [
        {"id": "clause-001", "text": "Thiếu category."},
        {"id": "clause-001", "category": "clauses"},
        {"id": "clause-001", "category": "clauses", "text": 5},
        {"id": 5, "category": "clauses", "text": "Sai kiểu."},
        {"id": "clause-001", "category": 5, "text": "Sai kiểu."},
        {"id": "clause-001", "category": "clauses", "text": "Có extra field.", "extra": 1},
    ],
)
def test_loader_rejects_missing_extra_or_wrong_typed_item_fields(
    tmp_path: Path, mutated: dict
) -> None:
    data = _base_corpus()
    data["utterances"] = [mutated]
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


def test_loader_rejects_duplicate_utterance_ids(tmp_path: Path) -> None:
    data = _base_corpus()
    data["utterances"][0] = dict(data["utterances"][0], id=data["utterances"][1]["id"])
    with pytest.raises(ValueError, match="duplicate utterance id"):
        load_utterances(_write_corpus(tmp_path, data))


def test_loader_rejects_duplicate_utterance_texts(tmp_path: Path) -> None:
    data = _base_corpus()
    data["utterances"][0] = dict(data["utterances"][0], text=data["utterances"][1]["text"])
    with pytest.raises(ValueError, match="duplicate utterance text"):
        load_utterances(_write_corpus(tmp_path, data))


@pytest.mark.parametrize("blank", ["", "  ", "\t"])
def test_loader_rejects_blank_utterance_texts(tmp_path: Path, blank: str) -> None:
    data = _base_corpus()
    data["utterances"][0] = dict(data["utterances"][0], text=blank)
    with pytest.raises(ValueError, match="utterance text must be non-empty"):
        load_utterances(_write_corpus(tmp_path, data))


def test_loader_rejects_undeclared_categories(tmp_path: Path) -> None:
    data = _base_corpus()
    data["utterances"] = [
        {"id": "short-001", "category": "mystery_category", "text": "Không khai báo."}
    ]
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


def test_loader_rejects_uncovered_declared_categories(tmp_path: Path) -> None:
    data = _base_corpus()
    data["utterances"] = [
        record for record in data["utterances"] if record["category"] != "clauses"
    ]
    with pytest.raises(ValueError):
        load_utterances(_write_corpus(tmp_path, data))


# ---------- 8.2 deterministic fragmentation ----------


def test_exact_codepoint_reconstruction_for_every_delivery_form() -> None:
    for text in load_texts():
        for fragments in fragment_deliveries(text).values():
            assert "".join(fragments) == text


def test_exact_utf8_byte_reconstruction_for_every_delivery_form() -> None:
    for text in load_texts():
        for fragments in fragment_deliveries(text).values():
            assert "".join(fragments).encode("utf-8") == text.encode("utf-8")


def test_delivery_form_keys_and_order_are_stable() -> None:
    for text in load_texts():
        assert tuple(fragment_deliveries(text)) == DELIVERY_FORMS


def test_fragmentation_is_deterministic_across_repeated_regeneration() -> None:
    for text in load_texts():
        for form, fragments in fragment_deliveries(text).items():
            assert fragment_deliveries(text)[form] == fragments


def test_no_empty_fragments_for_nonempty_corpus_texts() -> None:
    for text in load_texts():
        for fragments in fragment_deliveries(text).values():
            assert all(fragment for fragment in fragments)


@pytest.mark.parametrize("text", SHORT_TEXTS)
def test_short_and_whitespace_only_inputs_reconstruct_exactly(text: str) -> None:
    assert full_fragments(text) == [text]
    assert "".join(character_fragments(text)) == text
    assert "".join(word_fragments(text)) == text
    assert "".join(provider_like_fragments(text)) == text


@pytest.mark.parametrize("text", WHITESPACE_ONLY_TEXTS)
def test_whitespace_only_inputs_return_exactly_one_provider_delta(text: str) -> None:
    assert provider_like_fragments(text) == [text]


def test_empty_provider_like_input_returns_empty_list() -> None:
    assert provider_like_fragments("") == []


def test_word_fragmentation_matches_maximal_whitespace_regex_split() -> None:
    for text in load_texts():
        assert word_fragments(text) == re.findall(r"\s+|\S+", text)


def test_word_fragments_preserve_maximal_whitespace_runs_exactly() -> None:
    sample = "Xin chào  mọi\t\tngười.\n\nCảm ơn!\nRất vui.  "
    fragments = word_fragments(sample)
    assert fragments == re.findall(r"\s+|\S+", sample)
    assert "  " in fragments
    assert "\t\t" in fragments
    assert "\n\n" in fragments
    assert "  " in fragments[-1]
    assert "".join(fragments) == sample


def test_character_fragments_are_single_codepoints() -> None:
    for text in load_texts():
        assert all(len(fragment) == 1 for fragment in character_fragments(text))


def test_provider_like_fragments_are_word_aligned_and_non_empty() -> None:
    for text in load_texts():
        for fragment in provider_like_fragments(text):
            assert fragment.strip()


def test_provider_like_fragments_use_three_one_two_word_pattern() -> None:
    deltas = provider_like_fragments(PROVIDER_PATTERN_SAMPLE)
    assert deltas == PROVIDER_PATTERN_DELTAS
    assert [len(delta.split()) for delta in deltas] == [3, 1, 2, 1]


def test_fragment_deliveries_do_not_mutate_source_text() -> None:
    original = load_texts()
    for text in original:
        fragment_deliveries(text)
    assert load_texts() == original
