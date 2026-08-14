"""Sentence-level speech scheduling above the canonical Change A chunker (cluster C13).

Speaks each approved sentence through the existing canonical
``StreamOrchestrator.speak_verbatim`` path — one sentence at a time — and
advances the script cursor ONLY when the speech call returns normally.

Ownership boundaries this module preserves:

- 13.4: the canonical ``speak_verbatim`` path is the only way sentence text
  reaches TTS; no other speech seam is introduced here.
- 13.5/13.6: the ONLY sentence-completion signal is the speech call returning
  normally. The player never inspects ``TextChunk`` objects, ``is_final``
  flags, windows, or chunk boundaries — Change A's phrase segmentation
  happens entirely inside the orchestrator adapter (the boundary).
- 13.7: this module never constructs ``TextChunk``, never passes deadlines or
  runtime hints, and never stamps finality; chunk policy/deadline/hint/finality
  ownership stays with Change A. The module does not import the
  ``text_chunker`` package at all.
- 13.8: the cursor derives from the immutable approved artifact; the player
  only advances a cursor over exact sentence texts, never rewriting them.

The ``SentenceSpeechService`` protocol is the system boundary: tests inject
fakes to prove error/cancel semantics without a real orchestrator, while
``OrchestratorSpeechService`` is the thin production adapter over
``speak_verbatim``.
"""

from __future__ import annotations

import asyncio
from typing import Protocol, runtime_checkable

from backend.application.live_runtime.cursor_typing import CursorLike

__all__ = [
    "OrchestratorSpeechService",
    "ScriptSentencePlayer",
    "SentenceCompletionError",
    "SentenceSpeechService",
    "CursorLike",
]


class SentenceSpeechService(Protocol):
    """Boundary: canonical verbatim speech for one sentence.

    ``speak_sentence`` returns the exact spoken text on normal completion and
    raises on error or hard cancellation. ``StreamOrchestrator.speak_verbatim``
    satisfies the contract: it returns the input text on normal completion and
    propagates exceptions.
    """

    async def speak_sentence(self, session_id: str, text: str) -> str: ...


@runtime_checkable
class _CanonicalSpeech(Protocol):
    """Structural view of the canonical orchestrator boundary (task 12.x)."""

    async def speak_verbatim(self, session_id: str, text: str) -> str: ...


class OrchestratorSpeechService:
    """Thin ``SentenceSpeechService`` adapter over ``speak_verbatim``.

    Chunking (policy, deadlines, hints, finality) happens inside the
    orchestrator's verbatim path — Change A-owned. This adapter only forwards
    the exact sentence text and reports normal completion by returning it.
    """

    def __init__(self, speech_service: _CanonicalSpeech) -> None:
        self._speech_service = speech_service

    async def speak_sentence(self, session_id: str, text: str) -> str:
        return await self._speech_service.speak_verbatim(session_id, text)


class SentenceCompletionError(RuntimeError):
    """Sentence speech failed or was hard-cancelled before normal completion.

    Raised when the underlying speech call does not return normally, so the
    cursor is never advanced past a sentence that did not complete.
    """


class ScriptSentencePlayer:
    """Speak the current sentence and advance the cursor only on completion.

    ``play_current`` speaks ``cursor.current_sentence().text`` and, only on a
    normal return from the speech service, calls ``cursor.complete_current()``.
    On error/cancel the cursor keeps its position and the typed error
    propagates — the caller decides retry or bailout.
    """

    def __init__(self, cursor: CursorLike, speech: SentenceSpeechService) -> None:
        self._cursor = cursor
        self._speech = speech

    async def play_current(self, session_id: str) -> str:
        """Speak exactly the current sentence; advance only on completion."""
        sentence = self._cursor.current_sentence()
        try:
            spoken = await self._speech.speak_sentence(session_id, sentence.text)
        except asyncio.CancelledError:
            raise SentenceCompletionError(
                f"sentence speech cancelled before completion: {sentence.text!r}"
            ) from None
        except Exception as exc:
            raise SentenceCompletionError(
                f"sentence speech failed before completion: {sentence.text!r}"
            ) from exc
        self._cursor.complete_current()
        return spoken

    async def play_all(self, session_id: str) -> None:
        """Play every remaining sentence; stop on the first error."""
        while not self._cursor.finished:
            await self.play_current(session_id)
