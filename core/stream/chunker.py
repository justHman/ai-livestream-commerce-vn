"""Text chunker that coalesces LLM token deltas into phrase-sized TextChunks.

The streaming pipeline is:

  LLM stream (token-sized deltas) -> TextChunker -> TTS stream

LLM deltas are usually 1-5 chars — far too small for TTS. The chunker
accumulates incoming token text into a buffer and flushes phrase-sized
TextChunks when one of the following conditions is met (checked in order):

  1. Punctuation boundary: the buffer ends with one of ". , ! ? ; :" or a
     newline AND the buffer length >= ``min_chars``. The whole buffer is
     flushed. This is the primary, natural phrase boundary.
  2. Max chars: the buffer length >= ``max_chars`` (hard cap). The whole
     buffer is flushed so the chunk never grows unboundedly between
     punctuation.
  3. Timeout: the buffer length >= ``min_chars`` AND
     ``now - last_flush_time >= flush_timeout_ms``. The whole buffer is
     flushed. This is checked inside ``feed()`` on every token, AND is
     also exposed as ``check_timeout()`` so a coordinator can poll it
     between tokens (e.g. when the LLM is momentarily quiet). A sub-min
     buffer does NOT fire timeout — it keeps waiting until more tokens
     arrive (reaching min_chars via punctuation/max) or ``finalize()``
     is called.

``feed()`` and ``check_timeout()`` therefore never emit a chunk shorter
than ``min_chars`` (the punctuation condition already requires >=
min_chars; the max condition produces >= max_chars >= min_chars; the
timeout condition also requires >= min_chars). Only ``flush()`` (forced)
and ``finalize()`` (end of utterance) may emit a sub-min remainder.

A clock callable is injectable so timeout behaviour is deterministic in
tests. The default is ``time.monotonic``. The clock returns seconds (a
float); ``flush_timeout_ms`` is converted to seconds internally.

Stdlib only (no numpy). Reuses ``TextChunk`` from ``core.render.windows``.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from core.render.windows import TextChunk


# Phrase boundary characters. A newline is included as a phrase boundary
# (matches the task brief: punctuation (. , ! ? ; :), newline). NOTE: do not
# build this set with str.split() — split() treats "\n" as whitespace and
# would silently drop it.
_PUNCT_BOUNDARY = frozenset({".", ",", "!", "?", ";", ":", "\n"})


class TextChunker:
    """Coalesce token-sized LLM deltas into phrase-sized TextChunks.

    The chunker is stateful: it holds a text buffer and a monotonically
    increasing ``seq`` counter. Each emitted TextChunk carries the
    ``session_id`` / ``utterance_id`` passed to the constructor, the next
    ``seq`` (starting at 0), and a fresh uuid4 ``id``.

    Args:
        session_id: Render session identifier (propagated to every chunk).
        utterance_id: Utterance identifier within the session.
        min_chars: Minimum chunk size for natural (non-forced) flushes.
            ``feed()`` and ``check_timeout()`` will not emit a chunk
            shorter than this (punctuation requires >= min_chars, max
            produces >= max_chars, timeout also requires >= min_chars).
            Only ``flush()`` / ``finalize()`` may emit sub-min chunks.
        target_chars: Target phrase size. Currently informational (the
            chunker does not proactively flush at target; it waits for a
            boundary or max). Kept for config compatibility with the
            surrounding pipeline and future tuning.
        max_chars: Hard cap on chunk size. When the buffer reaches this,
            the whole buffer is flushed regardless of punctuation.
        flush_timeout_ms: Max time (ms) the buffer may sit without a flush.
            Polled inside ``feed()`` and via ``check_timeout()``.
        clock: Optional callable returning a monotonic float in seconds.
            Defaults to ``time.monotonic``. Inject a fake in tests for
            deterministic timeout behaviour.

    Raises:
        ValueError: If min_chars > max_chars, or target is outside
            [min_chars, max_chars].
    """

    def __init__(
        self,
        session_id: str,
        utterance_id: str,
        min_chars: int = 12,
        target_chars: int = 40,
        max_chars: int = 80,
        flush_timeout_ms: int = 350,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if not (min_chars <= target_chars <= max_chars):
            raise ValueError(
                f"require min_chars <= target_chars <= max_chars, got "
                f"min={min_chars}, target={target_chars}, max={max_chars}"
            )
        if flush_timeout_ms < 0:
            raise ValueError(f"flush_timeout_ms must be >= 0, got {flush_timeout_ms}")

        self._session_id = session_id
        self._utterance_id = utterance_id
        self._min_chars = min_chars
        self._target_chars = target_chars
        self._max_chars = max_chars
        self._flush_timeout_s = flush_timeout_ms / 1000.0
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic

        self._buffer: list[str] = []
        self._buffer_len: int = 0
        self._seq: int = 0
        # Initialize last_flush_time to now so a long pause before the first
        # token does not cause an immediate timeout flush of an empty buffer
        # (the timeout condition also requires a non-empty buffer).
        self._last_flush_time: float = self._clock()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _buffer_text(self) -> str:
        """Return the current buffer as a single string (no mutation)."""
        return "".join(self._buffer)

    def _emit(self, text: str, is_final: bool) -> TextChunk:
        """Build a TextChunk with the current seq, then increment seq."""
        chunk = TextChunk(
            session_id=self._session_id,
            utterance_id=self._utterance_id,
            seq=self._seq,
            text=text,
            is_final=is_final,
        )
        self._seq += 1
        return chunk

    def _reset_buffer(self) -> None:
        """Clear the buffer and stamp last_flush_time to now."""
        self._buffer = []
        self._buffer_len = 0
        self._last_flush_time = self._clock()

    def _flush_buffer(
        self, is_final: bool, *, prefer_word_boundary: bool = False
    ) -> list[TextChunk]:
        """Flush buffered text without cutting a word at the hard-size boundary."""
        if self._buffer_len == 0:
            return []
        text = self._buffer_text()
        remainder = ""
        if prefer_word_boundary and len(text) > self._max_chars:
            boundary = text.rfind(" ", self._min_chars, self._max_chars + 1)
            if boundary >= self._min_chars:
                remainder = text[boundary + 1 :]
                text = text[: boundary + 1]
        chunk = self._emit(text, is_final=is_final and not remainder)
        self._reset_buffer()
        if remainder:
            self._buffer = [remainder]
            self._buffer_len = len(remainder)
        return [chunk]

    def _check_timeout(self) -> bool:
        """Return True if the timeout condition is currently satisfied.

        The timeout condition requires all of:
          - buffer is non-empty,
          - buffer length >= ``min_chars`` (timeout respects the same
            min-chars coalescing rule as the punctuation condition; a
            sub-min buffer keeps waiting until more tokens arrive or
            ``finalize()`` is called),
          - elapsed >= ``flush_timeout_ms`` since the last flush.
        """
        if self._buffer_len < self._min_chars:
            return False
        now = self._clock()
        return (now - self._last_flush_time) >= self._flush_timeout_s

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, token_text: str) -> list[TextChunk]:
        """Accumulate ``token_text`` into the buffer and return any chunks
        flushed by the punctuation / max / timeout conditions.

        ``feed()`` never emits a chunk shorter than ``min_chars`` (the
        conditions that fire here all produce >= min_chars: punctuation
        requires >= min_chars, max produces >= max_chars, timeout also
        requires >= min_chars). Sub-min remainders are emitted only via
        ``flush()`` or ``finalize()``.

        Args:
            token_text: A token delta from the LLM stream. May be empty
                (empty tokens are no-ops and return []).

        Returns:
            List of zero or one TextChunks flushed by this feed. (At most
            one condition fires per feed because each flush resets the
            buffer.)
        """
        if token_text == "":
            return []

        # Append the new token BEFORE checking flush conditions (per spec:
        # "After appending, check flush conditions"). This means a timeout
        # firing on feed() flushes the whole buffer including the new token.
        self._buffer.append(token_text)
        self._buffer_len += len(token_text)

        # Condition 1: punctuation boundary + min_chars. The whole buffer
        # is flushed when it ends with a boundary char and is >= min_chars.
        if self._buffer_len >= self._min_chars and token_text[-1] in _PUNCT_BOUNDARY:
            return self._flush_buffer(is_final=False)

        # Condition 2: max_chars hard cap. Flush regardless of punctuation.
        if self._buffer_len >= self._max_chars:
            return self._flush_buffer(is_final=False, prefer_word_boundary=True)

        # Condition 3: timeout. The buffer (including the just-appended
        # token) has sat without a flush for >= flush_timeout_ms.
        if self._check_timeout():
            return self._flush_buffer(is_final=False)

        # No flush yet: keep accumulating.
        return []

    def check_timeout(self) -> list[TextChunk]:
        """Poll-only flush on timeout. Does not append new text.

        A coordinator calls this between tokens (e.g. when the LLM is
        momentarily quiet) to ensure a buffered phrase is not held longer
        than ``flush_timeout_ms``. Returns the flushed chunk or [].

        Returns:
            List of zero or one TextChunks. The chunk is not final
            (``is_final=False``); only ``finalize()`` marks finality.
        """
        if self._check_timeout():
            return self._flush_buffer(is_final=False)
        return []

    def flush(self) -> list[TextChunk]:
        """Force-flush the current buffer as a single non-final TextChunk.

        Unlike ``feed()``, this may emit a sub-``min_chars`` chunk (it is a
        forced flush, not a natural boundary). Unlike ``finalize()``, the
        emitted chunk is NOT final — the utterance continues.

        Returns:
            List of zero or one TextChunks. [] if the buffer is empty.
        """
        return self._flush_buffer(is_final=False)

    def finalize(self) -> list[TextChunk]:
        """Flush the remaining buffer as the final TextChunk of the utterance.

        The final chunk carries ``is_final=True`` even when shorter than
        ``min_chars`` (the utterance is over; the remainder must still be
        spoken). After finalize, the buffer is reset and ``seq`` continues
        to increment (a subsequent utterance on the same chunker instance
        would resume seq numbering — callers should generally create a new
        chunker per utterance, but resetting the buffer keeps the instance
        reusable without surprising state).

        Returns:
            List of zero or one TextChunks. [] if the buffer was empty.
        """
        return self._flush_buffer(is_final=True)
