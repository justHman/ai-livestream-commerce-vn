"""Content-free chunk-decision telemetry and bounded EWMA runtime hints.

Task 7.1/7.2/7.4: every chunk-decision fact recorded here is content-free —
sequence, decision reason, character length, estimated spoken duration,
fallback flags, policy id. Raw chunk text is never stored, logged, or leaked
through ``repr``, so telemetry can be consumed by observability pipelines
with the same redaction guarantees as the rest of the backend.

``BoundedEwma`` is a deterministic, bounded-memory EWMA over a fixed window
of the last ``window`` samples; ``value`` is recomputed over that window
every read, so the same input sequence always yields the same output. Missing
data degrades to ``None`` — the neutral hint ``RuntimeHints`` already treats
as "no information".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .types import RuntimeHints

__all__ = ["ChunkTelemetry", "BoundedEwma", "TelemetryCollector"]


@dataclass(frozen=True)
class ChunkTelemetry:
    """One content-free record of a chunk-decision.

    Deliberately no ``text`` field: lengths, timings, reason enums, and flag
    booleans only, so the record can never leak what was spoken.
    """

    seq: int
    decision_reason: str
    char_length: int
    estimated_duration_ms: Optional[float] = None
    hard_max_used: bool = False
    protected_span_fallback: bool = False
    policy: Optional[str] = None
    is_final: bool = False


class BoundedEwma:
    """Deterministic bounded-memory EWMA over the last ``window`` samples.

    NaN/inf updates are ignored (a broken measurement never poisons the
    hint); ``value`` is ``None`` while no finite sample exists. The EWMA is
    recomputed over the stored window on every read, so it is deterministic
    and order-of-call independent for the same sequence of samples.
    """

    def __init__(self, alpha: float, window: int = 8) -> None:
        if not (0.0 < alpha <= 1.0):
            raise ValueError(f"alpha must be in (0, 1], got {alpha!r}")
        if window < 1:
            raise ValueError(f"window must be >= 1, got {window!r}")
        self._alpha = alpha
        self._window = window
        self._values: list[float] = []

    @property
    def window_size(self) -> int:
        """Fixed sample window; exposed for testability."""
        return self._window

    def update(self, value: float) -> None:
        if not math.isfinite(value):
            return  # NaN-safe: ignore non-finite samples
        self._values.append(value)
        if len(self._values) > self._window:
            self._values.pop(0)

    @property
    def value(self) -> Optional[float]:
        if not self._values:
            return None
        if len(self._values) == 1:
            return self._values[0]
        # Init to the window mean, then roll the EWMA forward in order.
        avg = sum(self._values) / len(self._values)
        ema = avg
        for v in self._values:
            ema = self._alpha * v + (1 - self._alpha) * ema
        return ema


class TelemetryCollector:
    """Collects content-free chunk/TTS telemetry; never stores text."""

    def __init__(self, ewma_alpha: float = 0.3, ewma_window: int = 8) -> None:
        self._records: list[ChunkTelemetry] = []
        self._first_audio = BoundedEwma(ewma_alpha, ewma_window)
        self._rtf = BoundedEwma(ewma_alpha, ewma_window)
        self._playback = BoundedEwma(ewma_alpha, ewma_window)

    def record_chunk(self, telemetry: ChunkTelemetry) -> None:
        self._records.append(telemetry)

    def record_tts_timing(self, synthesis_ms: float, audio_duration_ms: float) -> None:
        """Record one synthesis call's timing.

        ``synthesis_ms`` is the wall time until the first audio exists (the
        non-streaming seam synthesizes once, then splits windows, so
        first-audio latency == synthesis latency). ``audio_duration_ms`` is
        the total generated audio duration. RTF = synthesis / duration, and
        is skipped (neutral) when the duration is zero or the synthesis time
        is non-finite.
        """
        if not math.isfinite(synthesis_ms):
            return
        if audio_duration_ms > 0:
            self._rtf.update(synthesis_ms / audio_duration_ms)
        self._first_audio.update(synthesis_ms)

    def record_playback_buffer(self, depth_ms: float) -> None:
        """Record one playback-buffer depth sample (ms of queued-but-undelivered video).

        Depth is the orchestration boundary's estimate of pending playback:
        queue depth in windows scaled by the most recent window's duration.
        Non-finite samples are ignored (NaN-safe via ``BoundedEwma``), and
        missing telemetry degrades to None — the neutral hint.
        """
        self._playback.update(depth_ms)

    def to_runtime_hints(self) -> RuntimeHints:
        """RuntimeHints-compatible neutral values (None when no data yet)."""
        return RuntimeHints(
            tts_first_audio_ewma_ms=self._first_audio.value,
            tts_rtf_ewma=self._rtf.value,
            playback_buffer_ms=self._playback.value,
        )

    @property
    def records(self) -> list[ChunkTelemetry]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)
