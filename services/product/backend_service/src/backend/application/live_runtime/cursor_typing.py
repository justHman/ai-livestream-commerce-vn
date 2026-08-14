"""Structural cursor contract for the sentence speaker (cluster C13).

The sentence player (``sentence_speaker.py``) depends only on this duck-typed
protocol — ``current_sentence()``, ``complete_current()``, ``finished`` — so
the real ``ScriptCursor`` (parallel task 13.1-13.3) satisfies it structurally
without the player importing it. Importing the real cursor here would create
an import-time dependency on a module that may not exist yet; tests import
the real cursor lazily at call time instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SentenceSpan:
    """Exact slice of the approved sentence text (identity + text).

    The text is a plain string copied from the immutable approved artifact —
    never derived from or coupled to ``TextChunk``.
    """

    index: int
    text: str


@runtime_checkable
class CursorLike(Protocol):
    """Minimal cursor surface the player drives."""

    @property
    def finished(self) -> bool: ...

    def current_sentence(self) -> SentenceSpan: ...

    def complete_current(self) -> None: ...
