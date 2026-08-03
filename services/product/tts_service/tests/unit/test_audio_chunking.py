"""Audio chunking and PCM conversion semantics."""

from __future__ import annotations

import numpy as np

from tts.engines.base import AudioChunk, TTSRequest


def test_pcm16_bytes_clips_and_converts() -> None:
    pcm = np.array([-1.0, 0.0, 1.0, 2.0], dtype=np.float32)
    chunk = AudioChunk(pcm=pcm, sample_rate=24_000)
    raw = chunk.to_pcm16_bytes()
    assert len(raw) == 4 * 2
    import struct

    values = struct.unpack("<4h", raw)
    assert values[0] == -32767
    assert values[1] == 0
    assert values[2] == 32767
    assert values[3] == 32767  # clipped


def test_audio_chunk_sample_rate() -> None:
    chunk = AudioChunk(pcm=np.zeros(4, np.float32), sample_rate=48_000)
    assert chunk.sample_rate == 48_000


def test_tts_request_defaults() -> None:
    req = TTSRequest(text="Xin chào")
    assert req.language == "vi"
    assert req.speed == 1.0