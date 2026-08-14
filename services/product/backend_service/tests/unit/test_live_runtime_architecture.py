"""Architecture guard tests: no second chunker, no sentence=TextChunk coupling (task 1.5).

The sentence scheduler above ``TextChunker`` (future
``backend/application/live_runtime/``, cluster C13) must not create a
script-specific chunker nor treat ``TextChunk`` as the sentence concept.
These tests prove the guard passes on the current tree and fails on each
forbidden pattern (one assertion per test, repo style).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.application.live_runtime_guards import assert_no_script_specific_chunker

# tests/unit -> .../backend_service/src
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_guard_passes_on_current_source_tree() -> None:
    assert_no_script_specific_chunker(SRC_ROOT)


def test_guard_fails_on_second_chunker_module(tmp_path: Path) -> None:
    _write(tmp_path, "application/live_runtime/sentence_chunker.py", "def split():\n    ...\n")
    with pytest.raises(RuntimeError, match="sentence_chunker"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_fails_on_chunker_package_directory(tmp_path: Path) -> None:
    _write(tmp_path, "application/live_runtime/chunking/__init__.py", "")
    with pytest.raises(RuntimeError, match="chunking"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_fails_on_second_chunker_import(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/live_runtime/cursor.py",
        "from backend.application.live_runtime.sentence_chunker import split_sentences\n",
    )
    with pytest.raises(RuntimeError, match="second chunker import"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_fails_on_sentence_textchunk_annotation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/live_runtime/cursor.py",
        "def mark(sentence: TextChunk) -> None:\n    ...\n",
    )
    with pytest.raises(RuntimeError, match="TextChunk-as-sentence coupling"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_fails_on_sentence_textchunk_assignment(tmp_path: Path) -> None:
    _write(tmp_path, "application/live_runtime/cursor.py", "sentence = TextChunk(text)\n")
    with pytest.raises(RuntimeError, match="TextChunk-as-sentence coupling"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_fails_on_sentence_type_alias(tmp_path: Path) -> None:
    _write(tmp_path, "application/live_runtime/cursor.py", "Sentence = TextChunk\n")
    with pytest.raises(RuntimeError, match="TextChunk-as-sentence coupling"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_fails_on_sentence_kwarg_from_chunk(tmp_path: Path) -> None:
    _write(tmp_path, "application/live_runtime/cursor.py", "arbiter.decide(sentence=chunk)\n")
    with pytest.raises(RuntimeError, match="TextChunk-as-sentence coupling"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_fails_on_isinstance_sentence_textchunk(tmp_path: Path) -> None:
    _write(tmp_path, "application/live_runtime/cursor.py", "if isinstance(sentence, TextChunk):\n")
    with pytest.raises(RuntimeError, match="TextChunk-as-sentence coupling"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_fails_on_textchunk_import_line_mentioning_sentence(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/live_runtime/cursor.py",
        "from backend.application.text_chunker import TextChunk, SentenceCursor\n",
    )
    with pytest.raises(RuntimeError, match="TextChunk-as-sentence coupling"):
        assert_no_script_specific_chunker(tmp_path)


def test_guard_passes_on_canonical_chunker_extra_file(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/text_chunker/extra.py",
        "sentence: TextChunk = TextChunk(text)\n",
    )
    assert_no_script_specific_chunker(tmp_path)
