"""Audio helpers for LiveAvatar LITE mode.

LiveAvatar LITE expects raw PCM: 16-bit signed little-endian, 24 kHz,
mono, base64-encoded, sent in chunks over the session WebSocket. Wrong
sample rate = garbled or silent avatar with NO error returned, so all
audio MUST pass through here before going on the wire.
"""

from __future__ import annotations

import base64
import math
import struct

import numpy as np

TARGET_RATE = 24_000          # Hz — LiveAvatar LITE requirement
BYTES_PER_SEC = TARGET_RATE * 2  # 16-bit mono = 48,000 bytes/sec
FIRST_CHUNK = int(BYTES_PER_SEC * 0.6)  # 600 ms initial buffer
NEXT_CHUNK = BYTES_PER_SEC              # 1 s subsequent chunks
MAX_PACKET = 1_000_000                  # ~1 MB cap per agent.speak packet


def resample_to_24k(pcm_bytes: bytes, original_rate: int) -> bytes:
    """Resample 16-bit mono PCM to 24 kHz via linear interpolation.

    Parameters
    ----------
    pcm_bytes : bytes
        Raw 16-bit signed little-endian mono PCM at `original_rate`.
    original_rate : int
        Source sample rate (e.g. 16000 for EdgeTTS, 22050, 44100).

    Returns
    -------
    bytes
        16-bit mono PCM at 24 kHz.
    """
    if original_rate == TARGET_RATE:
        return pcm_bytes
    samples = np.frombuffer(pcm_bytes, dtype="<i2").astype(np.float64)
    if samples.size == 0:
        return b""
    new_len = int(round(len(samples) * TARGET_RATE / original_rate))
    idx = np.linspace(0, len(samples) - 1, new_len)
    resampled = np.interp(idx, np.arange(len(samples)), samples)
    return np.clip(resampled, -32768, 32767).astype("<i2").tobytes()


def float_to_pcm16(samples: np.ndarray) -> bytes:
    """Convert a float32/float64 waveform in [-1, 1] to 16-bit PCM bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def b64(pcm_bytes: bytes) -> str:
    """Base64-encode PCM for the WebSocket `audio` field."""
    return base64.b64encode(pcm_bytes).decode("ascii")


def chunk_pcm(pcm_bytes: bytes):
    """Yield WebSocket-sized PCM chunks (600 ms first, 1 s after).

    Each chunk is also capped under MAX_PACKET.
    """
    first = True
    pos = 0
    n = len(pcm_bytes)
    while pos < n:
        target = FIRST_CHUNK if first else NEXT_CHUNK
        target = min(target, MAX_PACKET)
        yield pcm_bytes[pos : pos + target]
        pos += target
        first = False


def test_tone(seconds: float = 1.0, freq: int = 440) -> bytes:
    """Generate a 24 kHz PCM sine tone for debugging the audio path.

    If this plays cleanly on the avatar but your TTS doesn't, the TTS
    sample-rate/format is wrong (resample to 24 kHz).
    """
    n = int(TARGET_RATE * seconds)
    return b"".join(
        struct.pack("<h", int(32767 * 0.5 * math.sin(2 * math.pi * freq * i / TARGET_RATE)))
        for i in range(n)
    )
