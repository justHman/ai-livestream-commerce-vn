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

    The 350 ms flush timeout must be measured from the first non-empty
    fragment, not from chunker construction. INTENDED RED on baseline: the
    current chunker stamps ``_last_flush_time`` in ``__init__``, so after
    ``advance(5.0)`` the first ``feed()`` sees a 5 s-old buffer and flushes
    inside ``feed()`` (the assertion below fails with a spurious chunk).
    """
    clock, advance = _make_fake_clock()
    chunker = TextChunker(
        session_id="sess-1",
        utterance_id="utt-1",
        min_chars=12,
        flush_timeout_ms=350,
        clock=clock,
    )

    advance(5.0)  # long TTFT: no text yet, buffer stays empty
    assert chunker.feed("hello world ") == []  # 12 chars, no punctuation, no flush

    advance(0.2)  # only 200 ms of buffer age
    assert chunker.check_timeout() == []

    advance(0.2)  # 400 ms total age >= 350 ms
    chunks = chunker.check_timeout()
    assert len(chunks) == 1
    assert chunks[0].text == "hello world "
    assert chunks[0].is_final is False


def test_buffer_age_starts_on_first_non_empty_fragment():
    """The timeout counter starts when the buffer receives its first fragment.

    INTENDED RED on baseline: the current chunker counts age from
    construction, so the 3.2 s of elapsed fake time ages the buffer during
    the idle TTFT and the ``feed()`` that reaches min_chars flushes
    prematurely (unexpected chunk emitted) instead of waiting for 400 ms of
    post-first-fragment age.

    NOTE: the buffer must reach ``min_chars`` before the timeout window, or
    ``check_timeout()`` can never fire (TextChunker only applies the timeout
    to buffers >= min_chars) and the final assertion would stay red even
    after task 1.6 lands.
    """
    clock, advance = _make_fake_clock()
    chunker = TextChunker(
        session_id="sess-1",
        utterance_id="utt-1",
        min_chars=12,
        flush_timeout_ms=350,
        clock=clock,
    )

    advance(3.0)  # idle TTFT — must not count as buffer age
    assert chunker.feed("abcdefghij") == []  # 10 chars: below min_chars, no flush
    advance(0.2)
    # Reaches min_chars: 200 ms of age < 350 ms -> must NOT flush yet.
    assert chunker.feed("kl") == []  # INTENDED RED: construction-age flush on baseline

    advance(0.2)  # 400 ms of age since the first fragment
    chunks = chunker.check_timeout()
    assert len(chunks) == 1
    assert chunks[0].text == "abcdefghijkl"
    assert chunks[0].is_final is False


def test_empty_fragments_at_advanced_times_do_not_start_buffer_age():
    """Empty fragments must be ignored: they never start (or restart) the age clock.

    INTENDED RED on baseline: the chunker stamps ``_last_flush_time`` at
    construction, so by the time the first non-empty fragment arrives the
    buffer is already construction-aged — ``feed("kl")`` sees 200 ms +
    construction age >= 350 ms and flushes prematurely (unexpected chunk)
    instead of waiting for the post-first-fragment timeout.
    """
    clock, advance = _make_fake_clock()
    chunker = TextChunker(
        session_id="sess-1",
        utterance_id="utt-1",
        min_chars=12,
        flush_timeout_ms=350,
        clock=clock,
    )

    advance(2.0)
    assert chunker.feed("") == []  # empty fragment: ignored, no buffer age
    advance(2.0)
    assert chunker.feed("") == []  # still empty: still no buffer age

    advance(1.0)  # 5 s total idle; buffer still empty
    assert chunker.feed("abcdefghij") == []  # 10 chars: first non-empty starts the age clock
    advance(0.2)  # 200 ms of age < 350 ms
    assert chunker.feed("kl") == []  # INTENDED RED: construction-age flush on baseline

    advance(0.2)  # 400 ms of age since the first non-empty fragment
    chunks = chunker.check_timeout()
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
