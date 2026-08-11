"""Deterministic streaming-fragment generation for the VieNeu benchmark corpus.

Same corpus text delivered four ways (OpenSpec 8.2): full text in one
fragment, character-sized, word-sized, and provider-like coalesced LLM
deltas. Pure stdlib, no dependency on backend modules, so a future benchmark
runner can consume it standalone. Every generator is deterministic and
guarantees exact reconstruction:

    "".join(fragments) == original text

Contract notes:
- Word fragmentation is whitespace-preserving: maximal whitespace runs
  (spaces, repeated spaces, tabs, newlines) are never merged, trimmed, or
  split lossily, so joining the fragments reproduces the original exactly.
- Provider-like deltas are word-aligned, coalesced by lexical word count,
  and never cut mid-word, so chunking algorithms that only emit boundaries
  at word boundaries stay faithful.
- No ``split()`` is used for fragmentation (it collapses whitespace); the
  word scanner here is a hand-rolled index splitter that yields words and
  maximal whitespace runs as their own fragments.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "vi-benchmark-corpus"
VERSION = 1
CORPUS_PATH = Path(__file__).with_name("vi_benchmark_corpus_v1.json")

_ITEM_KEYS = ("id", "category", "text")
# Stable across loads so IDs are checked against one definition.
_ID_PATTERN = re.compile(r"[a-z]+-\d{3}")

# Provider-like coalescing: lexical word count per delta, cycling through
# this deterministic pattern. At least one word per delta, so short inputs
# still produce a delta (never empty) and never drop text.
_DELTA_SIZES = (3, 1, 2)


@dataclass(frozen=True)
class Utterance:
    """One validated corpus utterance."""

    id: str
    category: str
    text: str


def _parse_corpus(path: Path) -> tuple[list, list[str]]:
    """Read and schema-check the corpus file; return utterances + categories.

    Raises:
        ValueError: on unreadable files, a non-object top level, a
            schema/version mismatch, or provenance that does not state the
            authored-synthetic, no-PII, no-ground-truth contract.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise ValueError(f"benchmark corpus missing: {path}") from None
    if not isinstance(data, dict):
        raise ValueError(f"corpus must be a JSON object: {path}")
    if data.get("schema") != SCHEMA or data.get("version") != VERSION:
        raise ValueError(f"corpus schema/version mismatch in {path}")
    provenance = data.get("provenance")
    expected_provenance = {
        "authored_synthetic": True,
        "contains_pii": False,
        "factual_ground_truth": False,
    }
    if not isinstance(provenance, dict) or any(
        provenance.get(key) is not value for key, value in expected_provenance.items()
    ):
        raise ValueError(f"corpus provenance must state {expected_provenance}: {path}")
    utterances = data.get("utterances")
    if not isinstance(utterances, list) or not utterances:
        raise ValueError(f"corpus has no utterances: {path}")
    declared = data.get("categories")
    if (
        not isinstance(declared, list)
        or not declared
        or any(not isinstance(category, str) or not category.strip() for category in declared)
    ):
        raise ValueError(f"corpus categories must be a non-empty list of strings: {path}")
    if len(set(declared)) != len(declared):
        raise ValueError("declared categories contain duplicates")
    return utterances, declared


def _validate_utterances(items: list) -> list[Utterance]:
    """Validate item schema/types and corpus-wide constraints.

    Raises:
        ValueError: on malformed items, blank or malformed IDs, duplicate
            IDs, duplicate or whitespace-only texts, or undeclared
            categories.
    """
    records = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError(f"utterance is not an object: {item!r}")
        if set(item) != set(_ITEM_KEYS):
            raise ValueError(f"utterance must have exactly {_ITEM_KEYS}: {item!r}")
        for key in _ITEM_KEYS:
            if not isinstance(item[key], str):
                raise ValueError(f"utterance {key!r} must be str: {item!r}")
        records.append(Utterance(item["id"], item["category"], item["text"]))
    ids = [record.id for record in records]
    if any(_ID_PATTERN.fullmatch(record.id) is None for record in records):
        raise ValueError(f"utterance id must match {_ID_PATTERN.pattern}: {ids}")
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate utterance id")
    texts = [record.text for record in records]
    if len(set(texts)) != len(texts):
        raise ValueError("duplicate utterance text")
    if any(not text.strip() for text in texts):
        raise ValueError("utterance text must be non-empty")
    return records


def _load(path: Path) -> tuple[list[Utterance], list[str]]:
    """Parse, validate, and return (records, declared categories).

    The corpus file is parsed exactly once per call, then every corpus-wide
    constraint is checked against the same records.

    Raises:
        ValueError: if the file is missing or malformed, the schema/version
            does not match this module, items fail validation, or a
            declared category has no representative utterance.
    """
    items, declared = _parse_corpus(path)
    records = _validate_utterances(items)
    categories = {record.category for record in records}
    undeclared = categories - set(declared)
    if undeclared:
        raise ValueError(f"utterances use undeclared categories: {sorted(undeclared)}")
    missing = [category for category in declared if category not in categories]
    if missing:
        raise ValueError(f"categories without utterances: {missing}")
    return records, declared


def load_utterances(path: Path = CORPUS_PATH) -> list[Utterance]:
    """Load and validate the corpus; return records in original JSON order."""
    return _load(path)[0]


def load_by_category(path: Path = CORPUS_PATH) -> dict[str, list[Utterance]]:
    """Load and validate the corpus; return utterances grouped by category.

    Group order follows the declared ``categories`` list; within a group the
    corpus order is kept.
    """
    records, declared = _load(path)
    return {category: [r for r in records if r.category == category] for category in declared}


def load_texts(path: Path = CORPUS_PATH) -> list[str]:
    """Return the utterance texts in corpus order."""
    return [record.text for record in _load(path)[0]]


def load_categories(path: Path = CORPUS_PATH) -> list[str]:
    """Return the declared category names in corpus order."""
    return _load(path)[1]


def _split_words(text: str) -> Iterator[str]:
    """Yield words and maximal whitespace runs in order, preserving everything.

    Unlike ``str.split()`` this never collapses or trims whitespace: spaces,
    repeated spaces, tabs, and newlines stay exactly as written and join
    back to the original text losslessly.
    """
    n = len(text)
    i = 0
    while i < n:
        j = i
        while j < n and not text[j].isspace():
            j += 1
        if j > i:
            yield text[i:j]
        k = j
        while k < n and text[k].isspace():
            k += 1
        if k > j:
            yield text[j:k]
        i = k


def word_fragments(text: str) -> list[str]:
    """Split ``text`` into words plus exact maximal whitespace runs."""
    return list(_split_words(text))


def character_fragments(text: str) -> list[str]:
    """Split ``text`` into one-codepoint fragments."""
    return list(text)


def full_fragments(text: str) -> list[str]:
    """Deliver the whole ``text`` as a single fragment."""
    return [text]


def provider_like_fragments(text: str) -> list[str]:
    """Coalesce word units into provider-like streaming deltas.

    Deterministic, word-aligned, never empty, never mid-word. Each delta
    takes ``_DELTA_SIZES[i % len(_DELTA_SIZES)]`` words, cycling through the
    pattern. Whitespace runs attach to the following delta (like the
    leading-space tokens of real provider streams), a trailing run stays on
    the last delta, and whitespace-only text yields a single whitespace
    delta, so the join reproduces the source exactly and no text is dropped.
    """
    deltas: list[str] = []
    pending_ws = ""
    words_taken = 0
    size_index = 0
    for unit in word_fragments(text):
        if unit.isspace():
            pending_ws += unit
            continue
        if words_taken == 0:
            deltas.append(pending_ws + unit)
        else:
            deltas[-1] += pending_ws + unit
        pending_ws = ""
        words_taken += 1
        if words_taken >= _DELTA_SIZES[size_index]:
            words_taken = 0
            size_index = (size_index + 1) % len(_DELTA_SIZES)
    if pending_ws:
        if deltas:
            deltas[-1] += pending_ws
        else:
            deltas.append(pending_ws)
    return deltas


def fragment_deliveries(text: str) -> dict[str, list[str]]:
    """All deterministic delivery forms for one text, keyed by form name.

    Order is stable: ``full``, ``character``, ``word``, ``provider_like``.
    """
    return {
        "full": full_fragments(text),
        "character": character_fragments(text),
        "word": word_fragments(text),
        "provider_like": provider_like_fragments(text),
    }
