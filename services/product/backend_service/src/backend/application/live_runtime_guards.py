"""Architecture guards for the sentence scheduler above ``TextChunker`` (task 1.5).

The future sentence scheduler lives under ``backend/application/live_runtime/``
(cluster C13; the package does not exist yet). Per the change proposal, it
"sits above TextChunker and MUST NOT create a script-specific chunker, stamp
finality, or infer sentence completion from TextChunk identity". These guards
make that constraint machine-checkable before C13 lands:

- ``script-qna-speech-arbitration`` spec: "Sentence scheduling is above
  TextChunker"; lower-level TextChunk finality SHALL NOT be treated as proof
  that the approved sentence completed.
- ``approved-script-authoring-pipeline`` spec: the scheduler "SHALL NOT create
  a script-specific chunker"; "TextChunk finality remains Change A-owned".

Detection is intentionally pragmatic — a regex set over source lines, same
shape as ``change_a_contract._FORBIDDEN_IMPORT_PATTERNS`` — and flags only
COUPLING (a second chunker package, or TextChunk used as a sentence). It does
not forbid ``live_runtime`` from referencing the ``TextChunk`` type itself.
Module paths are written slash-style in this docstring on purpose: the guard
scans its own source, and dotted paths would trip the import pattern.
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.application.script_authoring.change_a_contract import iter_source_files

# Canonical Change A chunker package. Its own sentence-boundary heuristics are
# Change A-owned, so the whole package is exempt from the second-chunker scan.
_CANONICAL_CHUNKER = "text_chunker"

# A chunker-ish module is a file or package whose own name or containing
# directory carries a "chunk" token (e.g. ``sentence_chunker.py``,
# ``live_runtime/sentence_chunker/``). Anchored on the two innermost path
# segments so a chunker-named directory anywhere outside the canonical package
# is caught no matter how deeply the file sits inside it.
_CHUNK_NAME = re.compile(r"chunk", re.IGNORECASE)

# Import of a would-be second chunker namespace, e.g. importing from a
# ``sentence_chunker`` module under ``live_runtime``. The negative lookahead
# lets ``text_chunker`` imports through. (Unescaped dots in the module path
# could in principle match the ``\.`` of a compiled regex literal; no such
# literal exists in this tree, and the pattern needs a chunk token anyway.)
_CHUNKER_IMPORT = re.compile(
    r"backend\.application\.(?!text_chunker)[a-z_]+\.(?:[a-z_]*chunk[a-z_]*|chunking)"
)

# sentence=<TextChunk> coupling patterns (same-line or type-alias usage).
# "Sentence scheduling is above TextChunker": a scheduler may hold TextChunk,
# but must not treat the type as the sentence concept.
_SENTENCE_ANNOTATION = re.compile(r"sentence\s*[:=]\s*TextChunk")
_SENTENCE_ALIAS = re.compile(r"Sentence\s*=\s*TextChunk")
_SENTENCE_ISINSTANCE = re.compile(r"isinstance\(\s*sentence\s*,\s*TextChunk\s*\)")
_SENTENCE_KWARG = re.compile(r"sentence\s*=\s*chunk")
_SENTENCE_IMPORT_COUPLING = re.compile(
    r"from\s+backend\.application\.text_chunker(?:\.\w+)?\s+import\s+[^#\n]*\bTextChunk\b"
    r"[^\n]*(?:sentence|Sentence)"
)


def _is_second_chunker(path: Path) -> bool:
    """True when ``path`` names a chunker module outside the canonical package."""
    if _CANONICAL_CHUNKER in path.parts:
        return False
    return bool(_CHUNK_NAME.search(path.stem)) or bool(_CHUNK_NAME.search(path.parent.name))


def assert_no_script_specific_chunker(root: Path) -> None:
    """Fail when source under ``root`` defines a second chunker or couples TextChunk to sentence.

    Raises ``RuntimeError`` listing offenders when any ``*.py`` file under
    ``root`` (a) is a chunker-named module outside ``text_chunker/``, (b)
    imports a would-be second chunker namespace, or (c) uses ``TextChunk`` as
    a sentence (annotation, alias, kwarg, ``isinstance``, or import-line
    coupling). Identical guard shape to ``assert_no_legacy_chunker_imports``
    (task 1.10 audit), extended for the C13 sentence scheduler.
    """
    offenders: list[str] = []
    for path in iter_source_files(root):
        if _CANONICAL_CHUNKER in path.parts:
            continue  # canonical chunker + its sentence heuristics are Change A-owned
        relative = str(path.relative_to(root))
        if _is_second_chunker(path):
            offenders.append(f"chunker module outside {_CANONICAL_CHUNKER}: {relative}")
            continue
        text = path.read_text(encoding="utf-8")
        if _CHUNKER_IMPORT.search(text):
            offenders.append(f"second chunker import: {relative}")
        for pattern in (
            _SENTENCE_ANNOTATION,
            _SENTENCE_ALIAS,
            _SENTENCE_ISINSTANCE,
            _SENTENCE_KWARG,
            _SENTENCE_IMPORT_COUPLING,
        ):
            if pattern.search(text):
                # Label written hyphenated on purpose: the guard scans its own
                # source, and a literal equals-sign label would trip the
                # annotation pattern it enforces.
                offenders.append(f"TextChunk-as-sentence coupling: {relative}")
                break
    if offenders:
        raise RuntimeError(
            "Script-specific chunker or TextChunk-as-sentence coupling found: "
            + "; ".join(sorted(set(offenders)))
        )
