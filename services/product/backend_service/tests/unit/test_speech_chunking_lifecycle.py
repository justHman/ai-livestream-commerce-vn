"""Phase-1 regression tests for OpenSpec adaptive-speech-text-chunking.

Covers task 1.6 (buffer age starts at the first non-empty fragment, not at
chunker construction — long TTFT must not count as buffer age) and task 1.9
(canonical type contract: ``backend.application.text_chunker.TextChunk`` is
the one canonical class and ``render.windows`` does not define or re-export
it).
"""

from __future__ import annotations

import pytest

from backend.application.text_chunker import TextChunker


def _make_fake_clock():
    """Return (clock, advance) backed by a mutable list so closures share state."""
    now = [0.0]

    def clock() -> float:
        return now[0]

    def advance(dt: float) -> None:
        now[0] += dt

    return clock, advance


def test_long_ttft_before_first_delta_does_not_age_empty_buffer():
    """A long time-to-first-token must not age the (empty) buffer.

    Buffer age is measured from the first non-empty fragment, not from
    chunker construction. The chunker exposes buffer_started_at/buffer_age_ms
    as the deadline inputs; the orchestrator applies the deadline via an
    explicit latency flush.
    """
    clock, advance = _make_fake_clock()
    chunker = TextChunker(
        session_id="sess-1",
        utterance_id="utt-1",
        min_chars=12,
        clock=clock,
    )

    advance(5.0)  # long TTFT: no text yet, buffer stays empty
    assert chunker.feed("hello world ") == []  # 12 chars, no punctuation, no flush

    advance(0.2)  # only 200 ms of buffer age
    assert chunker.buffer_started_at == 5.0  # first fragment, not construction
    assert chunker.buffer_age_ms == pytest.approx(200.0)

    # The orchestrator applies the deadline explicitly: buffer age is only
    # an input to its decision, never a timer inside the chunker.
    chunks = chunker.flush(reason="latency_deadline")
    assert len(chunks) == 1
    assert chunks[0].text == "hello world "
    assert chunks[0].is_final is False


def test_buffer_age_starts_on_first_non_empty_fragment():
    """The deadline clock starts when the buffer receives its first fragment.

    The 3.2 s of idle TTFT before the first fragment must not count as
    buffer age: buffer_started_at is stamped on the first non-empty feed,
    and feed() alone never flushes on age (the orchestrator applies the
    deadline explicitly).
    """
    clock, advance = _make_fake_clock()
    chunker = TextChunker(
        session_id="sess-1",
        utterance_id="utt-1",
        min_chars=12,
        clock=clock,
    )

    advance(3.0)  # idle TTFT — must not count as buffer age
    assert chunker.feed("abcdefghij") == []  # 10 chars: below min_chars, no flush
    advance(0.2)
    # Reaches min_chars: 200 ms of age since the first fragment — feed()
    # must NOT flush on age.
    assert chunker.feed("kl") == []

    assert chunker.buffer_started_at == 3.0  # first fragment, not construction
    advance(0.2)  # 400 ms of age since the first fragment
    assert chunker.buffer_age_ms == pytest.approx(400.0)
    chunks = chunker.flush(reason="latency_deadline")
    assert len(chunks) == 1
    assert chunks[0].text == "abcdefghijkl"
    assert chunks[0].is_final is False


def test_empty_fragments_at_advanced_times_do_not_start_buffer_age():
    """Empty fragments must be ignored: they never start (or restart) the age clock."""
    clock, advance = _make_fake_clock()
    chunker = TextChunker(
        session_id="sess-1",
        utterance_id="utt-1",
        min_chars=12,
        clock=clock,
    )

    advance(2.0)
    assert chunker.feed("") == []  # empty fragment: ignored, no buffer age
    advance(2.0)
    assert chunker.feed("") == []  # still empty: still no buffer age
    assert chunker.buffer_started_at is None

    advance(1.0)  # 5 s total idle; buffer still empty
    assert chunker.feed("abcdefghij") == []  # 10 chars: first non-empty starts the age clock
    assert chunker.buffer_started_at == 5.0  # first non-empty fragment, not construction
    advance(0.2)  # 200 ms of age
    assert chunker.feed("kl") == []  # feed() never flushes on age

    advance(0.2)  # 400 ms of age since the first non-empty fragment
    chunks = chunker.flush(reason="latency_deadline")
    assert len(chunks) == 1
    assert chunks[0].text == "abcdefghijkl"
    assert chunks[0].is_final is False


def test_render_windows_does_not_export_textchunk():
    """``render.windows`` must not define or re-export ``TextChunk``.

    ``backend.application.text_chunker.TextChunk`` is the one canonical
    class; importing ``TextChunk`` from ``render.windows`` must raise
    ImportError.
    """
    from backend.application.text_chunker import TextChunk as CanonicalTextChunk

    with pytest.raises(ImportError):
        from backend.application.render.windows import TextChunk  # noqa: F401

    assert CanonicalTextChunk.__module__ == "backend.application.text_chunker.types"
