"""Bounded script cursor owning advancement over the C11 ``ScriptState`` (task 13.3).

The cursor holds the immutable ``SentenceMap`` (derived once at bind from
the exact approved text, task 13.1) plus one ``ScriptState`` position, and
is the ONLY thing that advances it: ``complete_current`` verifies the exact
current sentence text, records the completion with the exact next sentence,
and marks the script finished at the final span. The map + state pair is
bounded — it never grows with the transcript and never mutates the approved
artifact.

Identity fields (script set id, approved version id, product id, current
sentence index, last completed sentence index, exact next sentence) come
straight from ``ScriptState``/``ScriptPosition`` (task 11.1); the arbiter
reads them through ``position()`` and ``checkpoint_next_before_qa()``
(no advancement).
"""

from __future__ import annotations

from typing import Optional

from backend.application.live_runtime.sentence_map import SentenceMap, SentenceSpan
from backend.application.live_runtime.script_state import ScriptPosition, ScriptState

__all__ = ["ScriptCursor", "ScriptCursorState"]


class ScriptCursor:
    """Owns sentence advancement over the C11 ``ScriptState``.

    Satisfies the ``CursorLike`` protocol surface the sentence player
    drives (``current_sentence``/``complete_current``/``finished``).
    """

    def __init__(
        self,
        script: object,
        sentence_map: Optional[SentenceMap] = None,
        *,
        script_set_id: Optional[str] = None,
        approved_version_id: Optional[str] = None,
        product_id: Optional[str] = None,
        state: Optional[ScriptState] = None,
    ) -> None:
        # Both call shapes are supported: ScriptCursor(map) and the
        # committed speaker tests' ScriptCursor(script, map).
        if sentence_map is None and isinstance(script, SentenceMap):
            sentence_map = script
            script = None
        if sentence_map is not None:
            self._map = sentence_map
            self._script_set_id = script_set_id or self._map.script_set_id
            self._approved_version_id = approved_version_id or self._map.approved_version_id
            self._product_id = product_id or self._map.product_id
        else:
            self._map = SentenceMap(spoken_text=script.spoken_text)  # type: ignore[attr-defined]
            self._script_set_id = script_set_id or getattr(script, "script_set_id", "")
            self._approved_version_id = approved_version_id or getattr(
                script, "approved_version_id", ""
            )
            self._product_id = product_id or getattr(script, "product_id", "")
        self._state = state if state is not None else ScriptState()
        if state is None:
            self._state.bind(
                script_set_id=self._script_set_id,
                approved_version_id=self._approved_version_id,
                product_id=self._product_id,
                first_sentence=self._map.spans[0].text if self._map.spans else "",
            )

    @property
    def finished(self) -> bool:
        """True after the final span completed (or the map is empty)."""
        position = self._state.position
        if position is None:
            return len(self._map) == 0
        return position.sentence_index > self._map.last_index

    @property
    def sentence_index(self) -> int:
        """Current span index (position.next_sentence matched to the map)."""
        position = self._state.position
        if position is None:
            return 0
        return position.sentence_index

    @property
    def last_completed_sentence_index(self) -> Optional[int]:
        """Index of the most recently completed span, or None."""
        position = self._state.position
        return position.last_completed_sentence_index if position is not None else None

    def current_sentence(self) -> SentenceSpan:
        """The exact current span (position.next_sentence matched to the map)."""
        position = self._state.position
        if position is None:
            raise RuntimeError("cursor is not bound")
        index = position.sentence_index
        if index > self._map.last_index:
            raise RuntimeError("script already finished")
        span = self._map.spans[index]
        if span.text != position.next_sentence:
            raise RuntimeError(
                f"position.next_sentence does not match map span {index}: "
                f"{position.next_sentence!r} != {span.text!r}"
            )
        return span

    def complete_current(self) -> None:
        """Verify the exact current sentence, advance by one, finish at the end."""
        current = self.current_sentence()
        next_span = self._map.next_after(current.index)
        self._state.record_sentence_completed(
            completed_sentence=current.text,
            next_sentence=next_span.text if next_span is not None else "",
        )

    def next_sentence(self) -> Optional[str]:
        """Exact text of the sentence after the current one, or None at the end."""
        position = self._state.position
        if position is None:
            return self._map.spans[0].text if self._map.spans else None
        span = self._map.next_after(position.sentence_index)
        return span.text if span is not None else None

    def position(self) -> ScriptPosition:
        """The C11 authoritative position (identity fields for task 13.3)."""
        position = self._state.position
        if position is None:
            raise RuntimeError("cursor is not bound")
        return position

    def checkpoint_next_before_qa(self) -> Optional[str]:
        """Exact next sentence text WITHOUT advancing (arbiter pre-Q&A checkpoint).

        Read-only view of the position that will move next; the arbiter uses
        it to checkpoint before Q&A interleave (task 14.9).
        """
        position = self._state.position
        if position is None:
            return self._map.spans[0].text if self._map.spans else None
        return position.next_sentence

    def render_context(self) -> dict[str, object]:
        """Passthrough of the bounded C11 position render context."""
        return self._state.render_context()


# Alias matching the QA-fixture duck-typed surface; the canonical name is
# ``ScriptCursor`` (structural ``CursorLike`` compatibility).
ScriptCursorState = ScriptCursor
