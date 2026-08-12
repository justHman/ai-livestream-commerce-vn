"""Contract: Change T implementation stays within its scope (task 16.7).

Change T deliberately excludes `/ws/platform`, viewer Q&A, Director
interruption, and semantic priority policy — those remain future product
specs. This test scans executable code (comments/docstrings stripped via the
tokenizer) to keep those concepts out of the implementation. Historical
mentions may exist in docs/notes; the code may not.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "tts"
FORBIDDEN = (
    re.compile(r"/ws/platform"),
    re.compile(r"viewer[ _-]?q&a|viewer[ _-]?message", re.IGNORECASE),
    re.compile(r"director[ _-]?interruption|script[ _-]?interruption", re.IGNORECASE),
    re.compile(r"semantic[ _-]?priority"),
)


def _executable_code() -> list[str]:
    chunks: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        try:
            source = tokenize.open(path).read()
        except (OSError, SyntaxError):
            continue
        code: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            code.append(tok.string)
        chunks.append("\n".join(code))
    return chunks


def test_no_out_of_scope_concepts_in_implementation() -> None:
    offenders: list[tuple[str, str]] = []
    for code in _executable_code():
        for pattern in FORBIDDEN:
            if pattern.search(code):
                offenders.append((SRC.as_posix(), pattern.pattern))
    assert offenders == [], f"out-of-scope concepts present in src/tts: {offenders}"
