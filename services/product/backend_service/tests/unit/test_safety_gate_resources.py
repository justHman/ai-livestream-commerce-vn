"""Curated safety-gate pattern-set resources (task 3.3 + 3.6 guard).

Covers provenance completeness + activation guard, per-kind load + required
Vietnamese slang/diacritic entries, and the false-positive foundation
(task 3.7): common benign commerce words must never appear in any curated
set.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.application.safety_gate.resources import (
    CuratedPatternSet,
    load_all_curated_patterns,
    load_curated_patterns,
    match_curated,
)

_RESOURCES_DIR = Path(__file__).resolve().parents[2] / "resources" / "safety"
_RESOURCE_FILES = (
    "curated_toxicity_v1.json",
    "curated_harassment_v1.json",
    "curated_unsafe_content_v1.json",
)


def _load_resource(filename: str) -> dict:
    path = _RESOURCES_DIR / filename
    assert path.is_file(), f"missing curated resource {path}"
    return json.loads(path.read_text(encoding="utf-8"))


# -- provenance + activation guard (mirrors the profanity lexicon guard) ----


@pytest.mark.parametrize("filename", _RESOURCE_FILES)
def test_resource_provenance_complete(filename: str) -> None:
    resource = _load_resource(filename)
    provenance = resource["provenance"]
    for key in (
        "version",
        "source",
        "license",
        "curated_by",
        "retrieval_version",
        "activation_status",
        "false_positive_review",
    ):
        assert provenance.get(key), f"{filename}: provenance missing {key!r}"
    assert "ai-livestream-commerce-vn" in provenance["curated_by"]


@pytest.mark.parametrize("filename", _RESOURCE_FILES)
def test_resource_activation_guard(filename: str) -> None:
    # Task 3.6 guard: incomplete OR inactive provenance refuses runtime use.
    with pytest.raises(ValueError, match="provenance"):
        CuratedPatternSet.from_resource({"patterns": ["x"]})
    inactive = {
        "provenance": {
            "version": "9",
            "source": "s",
            "license": "l",
            "curated_by": "c",
            "activation_status": "draft",
        },
        "patterns": ["x"],
    }
    with pytest.raises(ValueError, match="not activated"):
        CuratedPatternSet.from_resource(inactive)


# -- curated content --------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "filename"),
    [
        ("toxicity", "curated_toxicity_v1.json"),
        ("harassment", "curated_harassment_v1.json"),
        ("unsafe_content", "curated_unsafe_content_v1.json"),
    ],
)
def test_load_curated_patterns_default_and_nonempty(kind: str, filename: str) -> None:
    resource = _load_resource(filename)
    assert resource["provenance"]["activation_status"] == "active"
    assert len(resource["patterns"]) >= 10

    pattern_set = load_curated_patterns(kind=kind)
    assert len(pattern_set.patterns) == len(resource["patterns"])
    assert pattern_set.version == resource["provenance"]["version"]


def test_toxicity_contains_required_vietnamese_entries() -> None:
    # Task 3.7 fixtures: Vietnamese slang + diacritics are required.
    pattern_set = load_curated_patterns(kind="toxicity")
    assert "ngu" in pattern_set.patterns  # abusive slang
    assert "mất dạy" in pattern_set.patterns  # diacritics
    assert "vô văn hóa" in pattern_set.patterns  # diacritics


def test_load_all_curated_patterns_keys() -> None:
    all_sets = load_all_curated_patterns()
    assert set(all_sets) == {"toxicity", "harassment", "unsafe_content"}
    assert all(len(s.patterns) > 0 for s in all_sets.values())


# -- false-positive foundation (task 3.7) -----------------------------------

_BENIGN_COMMERCE_WORDS = [
    "mua",
    "giá",
    "ship",
    "shop",
    "em",
    "anh",
    "chị",
    "kem",
    "dưỡng",
    "da",
    "xinh",
    "đẹp",
    "ngay",
]


@pytest.mark.parametrize("word", _BENIGN_COMMERCE_WORDS)
def test_benign_commerce_words_not_in_any_curated_set(word: str) -> None:
    all_sets = load_all_curated_patterns()
    for kind, pattern_set in all_sets.items():
        assert word not in pattern_set.patterns, f"false positive: {word!r} in {kind}"


# -- VN slang + diacritics fixtures (task 3.7) --------------------------------
# Entries below were read from the actual curated v1 resources; every assertion
# pins a real curated phrase, including diacritic-heavy and no-diacritics ones.

_TOXICITY_ENTRIES = [
    "ngu",  # short abusive slang, common in live chat
    "ngu si",
    "khùng",
    "mất dạy",  # diacritics
    "vô văn hóa",  # diacritics
    "khốn nạn",
    "đồ chó",
    "cút đi",
    "câm mồm",
]


@pytest.mark.parametrize("entry", _TOXICITY_ENTRIES)
def test_toxicity_set_contains_slang_and_diacritics(entry: str) -> None:
    assert entry in load_curated_patterns(kind="toxicity").patterns


_HARASSMENT_ENTRIES = [
    "đe dọa",  # diacritics
    "đe doạ",  # variant spelling (ọ vs ạ)
    "đánh chết",
    "hăm dọa",
    "sàm sỡ",
    "quấy rối tình dục",
    "doxxing",  # latin loanword kept verbatim
    "khủng bố tinh thần",
    "bắt nạt online",
    "xúc phạm danh dự",
]


@pytest.mark.parametrize("entry", _HARASSMENT_ENTRIES)
def test_harassment_set_contains_slang_and_diacritics(entry: str) -> None:
    assert entry in load_curated_patterns(kind="harassment").patterns


_UNSAFE_ENTRIES = [
    "lừa đảo",  # the flagship commerce-scam phrase
    "lừa người mua",  # buyer-scam, directly relevant to livestream commerce
    "bịp",
    "đánh cắp tài khoản",
    "cờ bạc",
    "phim sex",  # no-diacritics form
    "khiêu dâm",  # diacritics
    "tự tử",
    "mua bán vũ khí",
    "18+",
]


@pytest.mark.parametrize("entry", _UNSAFE_ENTRIES)
def test_unsafe_content_set_contains_slang_and_diacritics(entry: str) -> None:
    assert entry in load_curated_patterns(kind="unsafe_content").patterns


# -- false-positive regression: VN livestream-commerce register (task 3.7) ---
# Engine-level matching lands with the consuming check (extra_checks); until
# then the resource-level regression below is the gate's foundation: a benign
# phrase must not match ANY curated pattern under the loader's contract
# (lowercased substring containment).

_BENIGN_COMMERCE_PHRASES = [
    "em ơi giá bao nhiêu",
    "shop ship đi anh ơi",
    "còn hàng không chị",
    "mua 2 tặng 1 nhé",
    "cho em xin link",
    "kem dưỡng ẩm giá bao nhiêu",
    "cái này có size lớn không",
    "bao giờ có hàng lại ạ",
]


@pytest.mark.parametrize("phrase", _BENIGN_COMMERCE_PHRASES)
def test_benign_commerce_phrases_match_no_curated_pattern(phrase: str) -> None:
    # Resource-level regression: not a single curated pattern may be
    # contained in the lowered phrase (the loader contract). Engine-level
    # rejection coverage lands with the consuming check that wires
    # match_curated into the gate.
    all_sets = load_all_curated_patterns()
    assert match_curated(phrase, all_sets) == []


# -- match_curated helper -----------------------------------------------------


def test_match_curated_reports_matching_kind() -> None:
    all_sets = load_all_curated_patterns()
    assert match_curated("mày ngu vừa thôi", all_sets) == ["toxicity"]


def test_match_curated_case_insensitive() -> None:
    all_sets = load_all_curated_patterns()
    assert match_curated("ĐE DỌA GÌ", all_sets) == ["harassment"]


def test_match_curated_multiple_kinds() -> None:
    all_sets = load_all_curated_patterns()
    assert sorted(match_curated("bịp bợm đe dọa khách", all_sets)) == [
        "harassment",
        "unsafe_content",
    ]


def test_match_curated_multiword_pattern_not_split_on_words() -> None:
    # "da" is not a curated pattern; only the full curated phrases match.
    all_sets = load_all_curated_patterns()
    assert match_curated("da em khô lắm", all_sets) == []
