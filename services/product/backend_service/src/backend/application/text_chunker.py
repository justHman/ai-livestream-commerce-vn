"""Canonical streaming text chunker (OpenSpec 1.21).

Coalesces LLM token deltas into phrase-sized chunks. The canonical chunk
dataclass lives here; the legacy ``core.render.windows.TextChunk`` remains
the transport type used by the existing orchestrator pipeline until Task
1.26 migrates launch paths.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from uuid import uuid4

__all__ = ["TextChunk", "TextChunker"]


@dataclass(frozen=True)
class TextChunk:
    """A streamed text fragment for one utterance."""

    session_id: str
    utterance_id: str
    seq: int
    text: str
    is_final: bool = False
    id: str = field(default_factory=lambda: uuid4().hex)


# Phrase boundary characters (punctuation + newline).
_PUNCT_BOUNDARY = frozenset({".", ",", "!", "?", ";", ":", "\n"})


class TextChunker:
    """Coalesce token-sized LLM deltas into phrase-sized TextChunks.

    Stateful: holds a text buffer and a monotonically increasing ``seq``.
    ``feed()`` flushes on punctuation (>= min_chars), max_chars hard cap, or
    timeout (>= min_chars). ``check_timeout()`` polls the timeout between
    tokens. ``flush()`` force-flushes non-final; ``finalize()`` emits the
    final remainder (may be shorter than min_chars).
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
        self.session_id = session_id
        self.utterance_id = utterance_id
        self.min_chars = min_chars
        self.target_chars = target_chars
        self.max_chars = max_chars
        self._flush_timeout_s = flush_timeout_ms / 1000.0
        self._clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._buffer: list[str] = []
        self._buffer_len = 0
        self._seq = 0
        self._last_flush_time: float = self._clock()

    # -- internal helpers -------------------------------------------------

    def _emit(self, text: str, is_final: bool) -> TextChunk:
        chunk = TextChunk(
            session_id=self.session_id,
            utterance_id=self.utterance_id,
            seq=self._seq,
            text=text,
            is_final=is_final,
        )
        self._seq += 1
        return chunk

    def _reset_buffer(self) -> None:
        self._buffer = []
        self._buffer_len = 0
        self._last_flush_time = self._clock()

    def _flush_buffer(self, is_final: bool) -> list[TextChunk]:
        if self._buffer_len == 0:
            return []
        chunk = self._emit("".join(self._buffer), is_final=is_final)
        self._reset_buffer()
        return [chunk]

    def _check_timeout(self) -> bool:
        if self._buffer_len < self.min_chars:
            return False
        return (self._clock() - self._last_flush_time) >= self._flush_timeout_s

    # -- public API -------------------------------------------------------

    def feed(self, token_text: str) -> list[TextChunk]:
        """Accumulate a token delta and flush any completed phrase."""
        if token_text == "":
            return []
        self._buffer.append(token_text)
        self._buffer_len += len(token_text)
        if self._buffer_len >= self.min_chars and token_text[-1] in _PUNCT_BOUNDARY:
            return self._flush_buffer(is_final=False)
        if self._buffer_len >= self.max_chars:
            return self._flush_buffer(is_final=False)
        if self._check_timeout():
            return self._flush_buffer(is_final=False)
        return []

    def check_timeout(self) -> list[TextChunk]:
        """Poll-only flush on timeout (no new text)."""
        if self._check_timeout():
            return self._flush_buffer(is_final=False)
        return []

    def flush(self) -> list[TextChunk]:
        """Force-flush the buffer as a non-final chunk (may be sub-min)."""
        return self._flush_buffer(is_final=False)

    def finalize(self) -> list[TextChunk]:
        """Flush the remaining buffer as the final chunk of the utterance."""
        return self._flush_buffer(is_final=True)
