"""Unit tests for streaming audio windowing helpers (windows.py).

Covers split_waveform, merge_small_chunks, num_frames_for edge cases:
empty, tiny, exact, last-short, plus validation errors.
"""

from __future__ import annotations

import math

import pytest

from core.render.windows import (
    AudioWindow,
    TextChunk,
    VideoWindow,
    merge_small_chunks,
    num_frames_for,
    split_waveform,
)


# ---------- fixtures/helpers ----------

SAMPLE_RATE = 16000  # 16 kHz -> 16 samples/ms -> 32 bytes/ms (int16 mono)


def ms_to_bytes(ms: int, sample_rate: int = SAMPLE_RATE) -> int:
    """Convert milliseconds of int16 mono PCM to byte count."""
    samples = int(sample_rate * ms / 1000)
    return samples * 2


def make_pcm(ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Generate deterministic PCM bytes of the given duration."""
    n_bytes = ms_to_bytes(ms, sample_rate)
    # Deterministic but varied pattern (cycle a 4-byte motif).
    motif = b"\x01\x00\x02\x00"
    return (motif * (n_bytes // len(motif) + 1))[:n_bytes]


# ---------- split_waveform: validation ----------


def test_split_raises_on_non_positive_sample_rate():
    with pytest.raises(ValueError):
        split_waveform(b"\x00\x01", 0, 20, 50, 100)


def test_split_raises_on_inconsistent_min_target_max():
    # min > target
    with pytest.raises(ValueError):
        split_waveform(b"\x00\x01", SAMPLE_RATE, 60, 50, 100)
    # target > max
    with pytest.raises(ValueError):
        split_waveform(b"\x00\x01", SAMPLE_RATE, 20, 120, 100)
    # min > max
    with pytest.raises(ValueError):
        split_waveform(b"\x00\x01", SAMPLE_RATE, 120, 50, 100)


# ---------- split_waveform: empty ----------


def test_split_empty_pcm_returns_empty_list():
    assert split_waveform(b"", SAMPLE_RATE, 20, 50, 100) == []


# ---------- split_waveform: tiny (shorter than target) ----------


def test_split_tiny_pcm_returns_single_short_window():
    pcm = make_pcm(10)  # 10 ms < target 50 ms
    windows = split_waveform(pcm, SAMPLE_RATE, 20, 50, 100)
    assert len(windows) == 1
    w = windows[0]
    assert w.duration_ms == 10
    assert w.sample_rate == SAMPLE_RATE
    assert w.seq == 0
    assert w.pcm == pcm
    assert w.audio_path is None
    assert w.is_final is True
    assert w.id is not None and len(w.id) > 0


# ---------- split_waveform: exact multiple of target ----------


def test_split_exact_multiple_of_target():
    pcm = make_pcm(150)  # exactly 3 * 50 ms
    windows = split_waveform(pcm, SAMPLE_RATE, 20, 50, 100)
    assert len(windows) == 3
    for i, w in enumerate(windows):
        assert w.seq == i
        assert w.duration_ms == 50
        assert len(w.pcm) == ms_to_bytes(50)
    # Concatenated bytes equal input.
    assert b"".join(w.pcm for w in windows) == pcm
    # Only the last is_final.
    assert [w.is_final for w in windows] == [False, False, True]


# ---------- split_waveform: last window shorter than min_ms ----------


def test_split_last_window_shorter_than_min_is_kept():
    # 110 ms at target 50 -> 50 + 50 + 10 (10 < min 20, but kept, not dropped).
    pcm = make_pcm(110)
    windows = split_waveform(pcm, SAMPLE_RATE, 20, 50, 100)
    assert len(windows) == 3
    assert [w.duration_ms for w in windows] == [50, 50, 10]
    assert windows[-1].is_final is True
    assert windows[-1].seq == 2
    assert b"".join(w.pcm for w in windows) == pcm


# ---------- split_waveform: never exceeds max_ms ----------


def test_split_never_exceeds_max_ms():
    # target 50, max 60, total 130 -> windows should be <= 60 ms each.
    pcm = make_pcm(130)
    windows = split_waveform(pcm, SAMPLE_RATE, 20, 50, 60)
    assert len(windows) >= 2
    for w in windows:
        assert w.duration_ms <= 60
    # Bytes preserved.
    assert b"".join(w.pcm for w in windows) == pcm


# ---------- split_waveform: seq increments from 0, unique ids ----------


def test_split_seq_increments_and_ids_unique():
    pcm = make_pcm(200)
    windows = split_waveform(pcm, SAMPLE_RATE, 20, 50, 100)
    assert [w.seq for w in windows] == list(range(len(windows)))
    ids = {w.id for w in windows}
    assert len(ids) == len(windows)


# ---------- split_waveform: session/utterance metadata propagated ----------


def test_split_propagates_session_and_utterance_ids():
    pcm = make_pcm(100)
    windows = split_waveform(
        pcm,
        SAMPLE_RATE,
        20,
        50,
        100,
        session_id="sess-123",
        utterance_id="utt-456",
    )
    for w in windows:
        assert w.session_id == "sess-123"
        assert w.utterance_id == "utt-456"


# ---------- merge_small_chunks ----------


def test_merge_coalesces_sub_min_chunks_into_previous():
    # Three windows: 50, 5, 5 ms; min 20 -> 50 stays, 5+5 merge into a 10 ms tail
    # (tail may remain < min per spec).
    w1 = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=make_pcm(50), sample_rate=SAMPLE_RATE, duration_ms=50, is_final=False,
    )
    w2 = AudioWindow(
        id="b", session_id="s", utterance_id="u", seq=1,
        pcm=make_pcm(5), sample_rate=SAMPLE_RATE, duration_ms=5, is_final=False,
    )
    w3 = AudioWindow(
        id="c", session_id="s", utterance_id="u", seq=2,
        pcm=make_pcm(5), sample_rate=SAMPLE_RATE, duration_ms=5, is_final=True,
    )
    merged = merge_small_chunks([w1, w2, w3], min_ms=20)
    # w1 is >= min, stays. w2 < min -> merge into previous (w1)? Spec says coalesce
    # into the previous window. We interpret: a short chunk merges INTO the chunk
    # that precedes it. So w2 merges into w1 -> 55 ms. Then w3 < min -> merges into
    # the (now 55 ms) previous -> 60 ms. Final list = [60 ms].
    assert len(merged) == 1
    assert merged[0].duration_ms == 60
    assert merged[0].seq == 0
    assert merged[0].is_final is True
    assert merged[0].pcm == w1.pcm + w2.pcm + w3.pcm


def test_merge_keeps_chunks_at_or_above_min():
    w1 = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=make_pcm(50), sample_rate=SAMPLE_RATE, duration_ms=50, is_final=False,
    )
    w2 = AudioWindow(
        id="b", session_id="s", utterance_id="u", seq=1,
        pcm=make_pcm(50), sample_rate=SAMPLE_RATE, duration_ms=50, is_final=True,
    )
    merged = merge_small_chunks([w1, w2], min_ms=20)
    assert len(merged) == 2
    assert [m.duration_ms for m in merged] == [50, 50]
    assert [m.seq for m in merged] == [0, 1]


def test_merge_final_window_may_remain_short():
    # Single short window stays as-is.
    w = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=make_pcm(5), sample_rate=SAMPLE_RATE, duration_ms=5, is_final=True,
    )
    merged = merge_small_chunks([w], min_ms=20)
    assert len(merged) == 1
    assert merged[0].duration_ms == 5


def test_merge_concatenates_text_spans():
    w1 = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=make_pcm(50), sample_rate=SAMPLE_RATE, duration_ms=50,
        text_span="Hello ", is_final=False,
    )
    w2 = AudioWindow(
        id="b", session_id="s", utterance_id="u", seq=1,
        pcm=make_pcm(5), sample_rate=SAMPLE_RATE, duration_ms=5,
        text_span="world", is_final=True,
    )
    merged = merge_small_chunks([w1, w2], min_ms=20)
    # w2 (5 ms) < min 20 -> merges into w1.
    assert len(merged) == 1
    assert merged[0].text_span == "Hello world"


def test_merge_preserves_session_and_utterance_from_first_in_group():
    w1 = AudioWindow(
        id="a", session_id="s1", utterance_id="u1", seq=0,
        pcm=make_pcm(50), sample_rate=SAMPLE_RATE, duration_ms=50, is_final=False,
    )
    w2 = AudioWindow(
        id="b", session_id="s2", utterance_id="u2", seq=1,
        pcm=make_pcm(5), sample_rate=SAMPLE_RATE, duration_ms=5, is_final=True,
    )
    merged = merge_small_chunks([w1, w2], min_ms=20)
    assert len(merged) == 1
    assert merged[0].session_id == "s1"
    assert merged[0].utterance_id == "u1"


def test_merge_empty_list_returns_empty_list():
    assert merge_small_chunks([], min_ms=20) == []


# ---------- num_frames_for ----------


def test_num_frames_for_exact_second():
    w = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=make_pcm(1000), sample_rate=SAMPLE_RATE, duration_ms=1000, is_final=True,
    )
    assert num_frames_for(w, fps=25) == 25


def test_num_frames_for_non_integer_ceil():
    # 750 ms at 25 fps -> 18.75 -> ceil 19.
    w = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=make_pcm(750), sample_rate=SAMPLE_RATE, duration_ms=750, is_final=True,
    )
    assert num_frames_for(w, fps=25) == 19
    assert num_frames_for(w, fps=30) == math.ceil(750 / 1000 * 30) == 23


def test_num_frames_for_zero_duration_returns_zero():
    w = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=b"", sample_rate=SAMPLE_RATE, duration_ms=0, is_final=True,
    )
    assert num_frames_for(w, fps=25) == 0


def test_num_frames_for_tiny_duration_returns_at_least_one():
    # 1 ms at 25 fps -> 0.025 -> ceil 1, and spec says at least 1 if duration>0.
    w = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=make_pcm(1), sample_rate=SAMPLE_RATE, duration_ms=1, is_final=True,
    )
    assert num_frames_for(w, fps=25) == 1


def test_num_frames_for_raises_on_non_positive_fps():
    w = AudioWindow(
        id="a", session_id="s", utterance_id="u", seq=0,
        pcm=make_pcm(100), sample_rate=SAMPLE_RATE, duration_ms=100, is_final=True,
    )
    with pytest.raises(ValueError):
        num_frames_for(w, fps=0)
    with pytest.raises(ValueError):
        num_frames_for(w, fps=-1)


# ---------- dataclasses: id auto-generation ----------


def test_text_chunk_auto_generates_id():
    c = TextChunk(session_id="s", utterance_id="u", seq=0, text="hi", is_final=True)
    assert c.id is not None and len(c.id) > 0
    assert c.text == "hi"
    assert c.is_final is True


def test_audio_window_auto_generates_id():
    w = AudioWindow(
        session_id="s", utterance_id="u", seq=0,
        pcm=b"\x00\x01", sample_rate=SAMPLE_RATE, duration_ms=1, is_final=True,
    )
    assert w.id is not None and len(w.id) > 0


def test_video_window_auto_generates_id():
    v = VideoWindow(
        session_id="s", utterance_id="u", seq=0,
        frames=[], fps=25, duration_ms=1000,
        audio_window_id="aw-1", is_final=True,
    )
    assert v.id is not None and len(v.id) > 0
    assert v.audio_window_id == "aw-1"


def test_audio_window_accepts_audio_path_instead_of_pcm():
    w = AudioWindow(
        session_id="s", utterance_id="u", seq=0,
        audio_path="/tmp/a.wav", sample_rate=SAMPLE_RATE, duration_ms=500,
        is_final=True,
    )
    assert w.pcm is None
    assert w.audio_path == "/tmp/a.wav"
