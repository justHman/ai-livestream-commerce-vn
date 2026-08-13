"""Authoritative live script position and bounded storage (task 11.1).

``ScriptPosition`` is the single source of truth for where the live session
stands in the approved script; ``ScriptState`` holds one current position
and never grows. Cursor advancement itself is owned by the speech-arbiter
cluster (``script_cursor.py``, cluster C13); this module only models the
state and its bounded storage semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ScriptPosition:
    """Exact approved-script identity and sentence-level position.

    ``next_sentence`` is the exact approved sentence text that will be
    spoken next — NOT derived from TextChunks (an approved sentence is
    spoken as multiple phrase-sized TextChunks by Change A; the sentence
    map lives in cluster C13).
    """

    script_set_id: str
    approved_version_id: str
    product_id: str
    sentence_index: int
    last_completed_sentence_index: Optional[int]
    next_sentence: str


@dataclass(frozen=True)
class _PendingCompletion:
    """A recorded sentence completion awaiting cursor advancement (C13)."""

    next_sentence: str


class ScriptState:
    """Mutable holder of the one current script position.

    ``bind()`` starts a script at sentence 0; ``record_sentence_completed()``
    records completion and re-attaches the checkpointed next sentence without
    advancing the cursor (advancement is cluster C13's job). Always holds at
    most one position.
    """

    def __init__(self) -> None:
        self._position: Optional[ScriptPosition] = None
        self._pending: Optional[_PendingCompletion] = None

    @property
    def position(self) -> Optional[ScriptPosition]:
        return self._position

    def bind(
        self,
        *,
        script_set_id: str,
        approved_version_id: str,
        product_id: str,
        first_sentence: str,
    ) -> None:
        """Start the approved script at sentence 0."""
        self._position = ScriptPosition(
            script_set_id=script_set_id,
            approved_version_id=approved_version_id,
            product_id=product_id,
            sentence_index=0,
            last_completed_sentence_index=None,
            next_sentence=first_sentence,
        )
        self._pending = None

    def record_sentence_completed(self, *, completed_sentence: str, next_sentence: str) -> None:
        """Record that ``completed_sentence`` finished and checkpoint ``next_sentence``.

        The cursor is NOT advanced: the checkpointed next sentence stays as
        ``position.next_sentence`` until the arbiter (cluster C13) advances.
        """
        if self._position is None:
            raise RuntimeError("cannot record sentence completion before bind()")
        if completed_sentence != self._position.next_sentence:
            raise RuntimeError("completed sentence does not match the current next sentence")
        self._position = ScriptPosition(
            script_set_id=self._position.script_set_id,
            approved_version_id=self._position.approved_version_id,
            product_id=self._position.product_id,
            sentence_index=self._position.sentence_index + 1,
            last_completed_sentence_index=self._position.sentence_index,
            next_sentence=next_sentence,
        )
        self._pending = _PendingCompletion(next_sentence=next_sentence)

    @property
    def pending_completion(self) -> Optional[_PendingCompletion]:
        return self._pending

    def render_context(self) -> dict[str, object]:
        """Bounded dict of the current position for model context.

        Contains only fixed-size identity fields — never transcript content.
        """
        if self._position is None:
            return {"script_bound": False}
        return {
            "script_bound": True,
            "script_set_id": self._position.script_set_id,
            "approved_version_id": self._position.approved_version_id,
            "product_id": self._position.product_id,
            "sentence_index": self._position.sentence_index,
            "last_completed_sentence_index": self._position.last_completed_sentence_index,
            "next_sentence": self._position.next_sentence,
        }
