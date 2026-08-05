"""Streaming data abstractions + audio windowing helpers.

This module defines the three dataclasses that flow between stages of the
streaming pipeline (LLM stream -> text chunker -> TTS stream -> avatar render):

  TextChunk   : LLM/text stage output
  AudioWindow : TTS stage output (PCM bytes or file path)
  VideoWindow : avatar render stage output

And three pure helper functions for audio windowing:

  split_waveform(pcm, sample_rate, min_ms, target_ms, max_ms) -> list[AudioWindow]
  merge_small_chunks(chunks, min_ms) -> list[AudioWindow]
  num_frames_for(window, fps) -> int

Stdlib only (dataclasses, math, uuid, typing). No numpy dependency.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextChunk:
    """A chunk of text emitted by the LLM/text stage of the stream.

    Attributes:
        id: Unique identifier (uuid4 hex, auto-generated if not supplied).
        session_id: Render session identifier.
        utterance_id: Utterance identifier within the session.
        seq: Sequence number within the utterance (0-based).
        text: The text content of this chunk.
        is_final: True if this is the final chunk of the utterance.
    """

    session_id: str
    utterance_id: str
    seq: int
    text: str
    is_final: bool
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class AudioWindow:
    """A window of audio emitted by the TTS stage.

    Exactly one of ``pcm`` or ``audio_path`` should be set: in-memory PCM bytes
    for the streaming path, or a filesystem path for the deferred/offline path.

    Attributes:
        session_id: Render session identifier.
        utterance_id: Utterance identifier within the session.
        seq: Sequence number within the utterance (0-based).
        sample_rate: Audio sample rate in Hz (e.g. 16000).
        duration_ms: Duration of this window in milliseconds.
        pcm: Raw PCM bytes (int16 mono, 2 bytes/sample) or None.
        audio_path: Filesystem path to an audio file or None.
        text_span: Optional text that produced this audio window.
        is_final: True if this is the final window of the utterance.
        id: Unique identifier (uuid4 hex, auto-generated if not supplied).
    """

    session_id: str
    utterance_id: str
    seq: int
    sample_rate: int
    duration_ms: int
    is_final: bool
    pcm: Optional[bytes] = None
    audio_path: Optional[str] = None
    text_span: Optional[str] = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass(frozen=True)
class VideoWindow:
    """A window of rendered video frames emitted by the avatar render stage.

    Attributes:
        session_id: Render session identifier.
        utterance_id: Utterance identifier within the session.
        seq: Sequence number within the utterance (0-based).
        frames: List of rendered frames (format TBD by render backend).
        fps: Frame rate of this window.
        duration_ms: Duration of this window in milliseconds.
        audio_window_id: id of the AudioWindow this video was rendered from.
        is_final: True if this is the final window of the utterance.
        id: Unique identifier (uuid4 hex, auto-generated if not supplied).
    """

    session_id: str
    utterance_id: str
    seq: int
    frames: list
    fps: int
    duration_ms: int
    audio_window_id: str
    is_final: bool
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# PCM int16 mono: 2 bytes per sample.
_BYTES_PER_SAMPLE = 2


def _ms_to_bytes(ms: int, sample_rate: int) -> int:
    """Convert milliseconds of int16 mono PCM to byte count."""
    samples = int(sample_rate * ms / 1000)
    return samples * _BYTES_PER_SAMPLE


def _bytes_to_ms(n_bytes: int, sample_rate: int) -> int:
    """Convert byte count of int16 mono PCM to milliseconds (integer floor)."""
    samples = n_bytes // _BYTES_PER_SAMPLE
    return int(samples * 1000 / sample_rate)


def split_waveform(
    pcm: bytes,
    sample_rate: int,
    min_ms: int,
    target_ms: int,
    max_ms: int,
    *,
    session_id: str = "",
    utterance_id: str = "",
) -> list[AudioWindow]:
    """Split raw PCM bytes (int16 mono) into AudioWindows near ``target_ms``.

    Each window is at most ``max_ms`` long; the LAST window of the stream may
    be shorter than ``min_ms`` (it is not dropped). Windows are sized to
    ``target_ms`` and the final window carries whatever remainder is left.

    Args:
        pcm: Raw PCM bytes (int16 mono, 2 bytes/sample).
        sample_rate: Audio sample rate in Hz. Must be > 0.
        min_ms: Minimum window duration. Must satisfy min_ms <= target_ms <= max_ms.
        target_ms: Target window duration.
        max_ms: Maximum window duration. Must be >= target_ms.
        session_id: Render session identifier propagated to each window.
        utterance_id: Utterance identifier propagated to each window.

    Returns:
        List of AudioWindow. Empty list if ``pcm`` is empty.

    Raises:
        ValueError: If sample_rate <= 0 or min/target/max are inconsistent
            (required: min_ms <= target_ms <= max_ms).
    """
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
    if not (min_ms <= target_ms <= max_ms):
        raise ValueError(
            f"require min_ms <= target_ms <= max_ms, got "
            f"min={min_ms}, target={target_ms}, max={max_ms}"
        )

    if len(pcm) == 0:
        return []

    target_bytes = _ms_to_bytes(target_ms, sample_rate)
    max_bytes = _ms_to_bytes(max_ms, sample_rate)

    # Make sure target_bytes/max_bytes are at least one sample (2 bytes) so we
    # always make progress even for very small min/target values.
    if target_bytes < _BYTES_PER_SAMPLE:
        target_bytes = _BYTES_PER_SAMPLE
    if max_bytes < _BYTES_PER_SAMPLE:
        max_bytes = _BYTES_PER_SAMPLE

    windows: list[AudioWindow] = []
    offset = 0
    seq = 0
    total = len(pcm)

    while offset < total:
        # Prefer a target-sized chunk, but never exceed max.
        end = min(offset + target_bytes, offset + max_bytes, total)
        chunk = pcm[offset:end]
        duration_ms = _bytes_to_ms(len(chunk), sample_rate)

        windows.append(
            AudioWindow(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=seq,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                pcm=chunk,
                is_final=False,  # set True on the last window below
            )
        )
        offset = end
        seq += 1

    # Mark the final window.
    if windows:
        last = windows[-1]
        windows[-1] = AudioWindow(
            session_id=last.session_id,
            utterance_id=last.utterance_id,
            seq=last.seq,
            sample_rate=last.sample_rate,
            duration_ms=last.duration_ms,
            pcm=last.pcm,
            audio_path=last.audio_path,
            text_span=last.text_span,
            is_final=True,
            id=last.id,
        )

    return windows


def merge_small_chunks(chunks: list[AudioWindow], min_ms: int) -> list[AudioWindow]:
    """Coalesce adjacent AudioWindows whose duration < min_ms into the previous window.

    A short window (duration_ms < min_ms) is merged INTO the preceding window:
    durations sum and PCM bytes concatenate. The first window of a group
    provides session_id/utterance_id; text_spans concatenate. Merging continues
    greedily, so a short window following an already-merged window also merges in.

    The final window of the returned list may remain < min_ms (per spec).

    Args:
        chunks: Adjacent AudioWindows to coalesce.
        min_ms: Minimum duration threshold; windows shorter than this merge
            into the previous window.

    Returns:
        New list of AudioWindow with seq reassigned sequentially from 0.
    """
    if not chunks:
        return []

    result: list[AudioWindow] = []
    for ch in chunks:
        if result and ch.duration_ms < min_ms:
            prev = result[-1]
            merged_pcm = b""
            if prev.pcm is not None and ch.pcm is not None:
                merged_pcm = prev.pcm + ch.pcm
            elif prev.pcm is not None:
                merged_pcm = prev.pcm
            elif ch.pcm is not None:
                merged_pcm = ch.pcm

            merged_text = None
            if prev.text_span and ch.text_span:
                merged_text = prev.text_span + ch.text_span
            elif prev.text_span:
                merged_text = prev.text_span
            elif ch.text_span:
                merged_text = ch.text_span

            result[-1] = AudioWindow(
                session_id=prev.session_id,
                utterance_id=prev.utterance_id,
                seq=prev.seq,  # will be reassigned below
                sample_rate=prev.sample_rate,
                duration_ms=prev.duration_ms + ch.duration_ms,
                pcm=merged_pcm if merged_pcm else None,
                audio_path=prev.audio_path or ch.audio_path,
                text_span=merged_text,
                is_final=ch.is_final,  # adopt finality of the latest chunk merged in
                id=prev.id,
            )
        else:
            # Append as-is; seq will be reassigned below.
            result.append(ch)

    # Reassign seq sequentially.
    return [
        AudioWindow(
            session_id=w.session_id,
            utterance_id=w.utterance_id,
            seq=i,
            sample_rate=w.sample_rate,
            duration_ms=w.duration_ms,
            pcm=w.pcm,
            audio_path=w.audio_path,
            text_span=w.text_span,
            is_final=w.is_final,
            id=w.id,
        )
        for i, w in enumerate(result)
    ]


def num_frames_for(window: AudioWindow, fps: int) -> int:
    """Return the number of video frames needed to render ``window`` at ``fps``.

    ``ceil(duration_ms / 1000 * fps)``. Returns 0 only if duration_ms == 0;
    returns at least 1 for any positive duration.

    Args:
        window: The AudioWindow whose video frame count is needed.
        fps: Target frame rate. Must be > 0.

    Returns:
        Frame count (>= 0).

    Raises:
        ValueError: If fps <= 0.
    """
    if fps <= 0:
        raise ValueError(f"fps must be > 0, got {fps}")
    if window.duration_ms <= 0:
        return 0
    return max(1, math.ceil(window.duration_ms / 1000 * fps))
