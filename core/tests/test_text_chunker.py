"""Unit tests for TextChunker (Task 3) — LLM→TTS phrase-boundary coalescing.

The chunker sits between the LLM stream (token-sized deltas) and the TTS
stream, coalescing small incoming tokens into phrase-sized TextChunks
suitable for TTS. It flushes on:
  1. punctuation boundary (. , ! ? ; : newline) when length >= min_chars,
  2. hard max_chars cap,
  3. flush_timeout_ms elapsed since last flush (pollable via check_timeout),
  4. forced flush() / finalize() (finalize marks is_final=True and may emit
     a sub-min_chars remainder).

A fake clock (closure over a mutable single-element list) is injected for
deterministic timeout behaviour. No real time, no network, no mocks of
TextChunk itself — assertions are on text content, seq, is_final, and count.
"""

from __future__ import annotations

from core.render.windows import TextChunk
from core.stream.chunker import TextChunker


# ---------- helpers / fakes ----------


def make_clock(start: float = 0.0) -> tuple:
    """Return (clock, advance) where clock() -> float and advance(dt) bumps it.

    Uses a mutable single-element list as the clock's state so the closure
    can be advanced deterministically without real time. Units: seconds
    (matches time.monotonic, which TextChunker defaults to).
    """
    t = [start]

    def clock() -> float:
        return t[0]

    def advance(dt_seconds: float) -> None:
        t[0] += dt_seconds

    return clock, advance


# ---------- punctuation flush ----------


def test_punctuation_flush_emits_full_phrase():
    """Feeding "Xin chào bạn." token by token: after the "." lands and the
    buffer is >= min_chars, a single chunk is emitted containing the full
    phrase. No sub-min_chars emission before the punctuation."""
    c = TextChunker(session_id="s1", utterance_id="u1", min_chars=12)
    out: list[TextChunk] = []
    for tok in ["Xin ", "chào ", "bạn."]:
        out.extend(c.feed(tok))

    assert len(out) == 1
    assert out[0].text == "Xin chào bạn."
    assert out[0].seq == 0
    assert out[0].is_final is False
    assert out[0].session_id == "s1"
    assert out[0].utterance_id == "u1"
    assert len(out[0].id) > 0


def test_punctuation_below_min_chars_does_not_flush():
    """A punctuation token arriving before min_chars is reached must NOT
    flush (the chunk would be too short for TTS). The buffer holds until
    more tokens arrive or finalize() is called."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12)
    # "Hi." is 3 chars — has punctuation but well under min_chars.
    out = c.feed("Hi.")
    assert out == []
    # Finalize must then emit it as a sub-min final chunk.
    final = c.finalize()
    assert len(final) == 1
    assert final[0].text == "Hi."
    assert final[0].is_final is True


# ---------- max-chars flush ----------


def test_max_chars_hard_cap_flush():
    """Feeding 200 'a' chars (no punctuation) with max_chars=80: feed()
    emits chunks at exactly the max_chars boundary. Remainder (< max) stays
    buffered until finalize()."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12, max_chars=80)
    out: list[TextChunk] = []
    for _ in range(200):
        out.extend(c.feed("a"))

    # 200 // 80 = 2 full flushes during feed (at 80 and 160); remainder 40.
    assert len(out) == 2
    assert all(ch.text == "a" * 80 for ch in out)
    assert [ch.seq for ch in out] == [0, 1]
    assert all(ch.is_final is False for ch in out)

    # Finalize the remainder.
    final = c.finalize()
    assert len(final) == 1
    assert final[0].text == "a" * 40
    assert final[0].is_final is True
    assert final[0].seq == 2


def test_max_chars_exactly_at_boundary_no_extra():
    """Feeding exactly max_chars chars then finalizing yields one feed-time
    chunk (exactly max) and finalize returns [] (buffer empty)."""
    c = TextChunker(session_id="s", utterance_id="u", max_chars=80)
    out: list[TextChunk] = []
    for _ in range(80):
        out.extend(c.feed("x"))
    assert len(out) == 1
    assert out[0].text == "x" * 80
    assert c.finalize() == []


# ---------- timeout flush ----------


def test_timeout_flush_via_check_timeout():
    """With a fake clock, a buffer >= min_chars flushed past the timeout
    threshold is emitted by check_timeout(). A sub-min buffer does NOT fire
    timeout — it keeps waiting until more tokens arrive or finalize() is
    called (timeout respects the min-chars coalescing rule)."""
    clock, advance = make_clock(start=0.0)
    c = TextChunker(
        session_id="s",
        utterance_id="u",
        min_chars=12,
        flush_timeout_ms=350,
        clock=clock,
    )

    # Buffer a short token (< min_chars) — no flush (below min, no punct).
    assert c.feed("hello") == []  # "hello" = 5 chars < 12

    # Even past the timeout threshold, a sub-min buffer must NOT flush:
    # timeout respects min_chars.
    advance(0.400)  # 400ms >= 350ms
    assert c.check_timeout() == []

    # The sub-min buffer is still waiting; finalize must emit it as a
    # sub-min final chunk (timeout never did).
    final = c.finalize()
    assert len(final) == 1
    assert final[0].text == "hello"
    assert final[0].is_final is True

    # Now a separate chunker: feed >= min_chars with no punctuation so only
    # timeout can fire.
    clock2, advance2 = make_clock(start=0.0)
    c2 = TextChunker(
        session_id="s",
        utterance_id="u",
        min_chars=12,
        flush_timeout_ms=350,
        clock=clock2,
    )
    # Feed 13 chars with no punctuation/boundary — stays buffered.
    assert c2.feed("hello world ") == []  # 12 chars, no punct, no flush
    assert c2.feed("x") == []  # 13 chars, no punct, no flush

    # Before timeout: check_timeout returns nothing.
    advance2(0.100)  # 100ms < 350ms
    assert c2.check_timeout() == []

    # Cross the timeout threshold.
    advance2(0.300)  # total 400ms >= 350ms
    flushed = c2.check_timeout()
    assert len(flushed) == 1
    assert flushed[0].text == "hello world x"
    assert flushed[0].is_final is False
    assert flushed[0].seq == 0

    # After flush, buffer is empty; a further check_timeout returns [].
    assert c2.check_timeout() == []


def test_timeout_flush_triggered_inside_feed():
    """If the clock has advanced past the timeout by the time feed() is
    called with a new token, the timeout condition flushes the previously
    buffered text — but ONLY when the buffer is >= min_chars (timeout
    respects min_chars). A sub-min buffer does NOT flush on timeout; it
    keeps accumulating until it reaches min_chars, or until finalize()."""
    clock, advance = make_clock(start=0.0)
    c = TextChunker(
        session_id="s",
        utterance_id="u",
        min_chars=12,
        flush_timeout_ms=350,
        clock=clock,
    )

    # Buffer sub-min content ("firstsecon" = 10 chars < 12).
    c.feed("first")   # 5 chars
    c.feed("secon")   # +5 = 10 chars, still < 12
    advance(0.500)    # 500ms >= 350ms — but buffer < min, no timeout flush.

    # Feeding another token: still below min_chars (11), no flush (timeout
    # cannot fire; no punct; not at max).
    out = c.feed("x")  # +1 = 11 chars, still < 12
    assert out == []

    # Now feed one more token to reach min_chars (12). Timeout fires on the
    # whole buffer (including the new token) because clock is still past
    # threshold and buffer >= min_chars.
    out = c.feed("y")  # buffer becomes "firstseconxy" = 12 chars
    assert len(out) == 1
    assert out[0].text == "firstseconxy"
    assert out[0].is_final is False
    assert out[0].seq == 0

    # Sub-min remainder after flush: timeout will not emit it, finalize will.
    c.feed("tail")  # 4 chars buffered, < min
    advance(1.000)  # well past timeout
    assert c.check_timeout() == []  # sub-min: no timeout flush
    final = c.finalize()
    assert len(final) == 1
    assert final[0].text == "tail"
    assert final[0].is_final is True


# ---------- final flush ----------


def test_finalize_emits_remainder_with_is_final():
    """finalize() flushes whatever is in the buffer as a single TextChunk
    with is_final=True, even when shorter than min_chars."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12)
    c.feed("short")  # 5 chars, no flush
    final = c.finalize()
    assert len(final) == 1
    assert final[0].text == "short"
    assert final[0].is_final is True
    assert final[0].seq == 0


def test_finalize_empty_buffer_returns_empty():
    """finalize() on a fresh chunker (empty buffer) returns []."""
    c = TextChunker(session_id="s", utterance_id="u")
    assert c.finalize() == []


def test_finalize_after_flush_empty_buffer_returns_empty():
    """If feed() already flushed everything (e.g. punct + min_chars), a
    following finalize() returns [] (nothing left)."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12)
    for tok in ["Xin ", "chào ", "bạn."]:
        c.feed(tok)
    # One punct flush happened; buffer is now empty.
    assert c.finalize() == []


# ---------- min-chars coalescing ----------


def test_min_chars_coalescing_feed_emits_nothing_then_finalize():
    """Feeding a short string (< min_chars, no punctuation) emits nothing
    from feed(); finalize() emits it as the final chunk."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12)
    out = c.feed("hi")
    assert out == []
    final = c.finalize()
    assert len(final) == 1
    assert final[0].text == "hi"
    assert final[0].is_final is True


def test_min_chars_accumulates_across_tokens_until_threshold():
    """Multiple sub-min tokens accumulate; feed() emits nothing until the
    combined length crosses min_chars AND ends with punctuation."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12)
    out: list[TextChunk] = []
    out.extend(c.feed("one "))  # 4
    out.extend(c.feed("two "))  # 8
    out.extend(c.feed("three."))  # 14, ends with "."
    assert len(out) == 1
    assert out[0].text == "one two three."
    assert out[0].seq == 0


# ---------- seq increments across multiple flushes ----------


def test_seq_increments_across_punct_and_max_and_final():
    """Three flushes: punct flush (seq 0), max flush (seq 1), finalize
    (seq 2). Verify seq numbers are 0,1,2 in order and is_final only on the
    last."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12, max_chars=80)
    out: list[TextChunk] = []

    # First: punct flush with >= min_chars.
    for tok in ["Hello ", "world."]:  # 12 chars, ends "."
        out.extend(c.feed(tok))

    # Second: max_chars flush (feed 80 'a's).
    for _ in range(80):
        out.extend(c.feed("a"))

    # Third: finalize a small remainder.
    c.feed("tail")
    out.extend(c.finalize())

    assert [ch.seq for ch in out] == [0, 1, 2]
    assert [ch.is_final for ch in out] == [False, False, True]
    assert out[0].text == "Hello world."
    assert out[1].text == "a" * 80
    assert out[2].text == "tail"


# ---------- forced flush() ----------


def test_flush_emits_buffered_without_is_final():
    """flush() forces a flush of the current buffer (sub-min allowed) but
    does NOT set is_final (only finalize does)."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12)
    c.feed("partial")
    out = c.flush()
    assert len(out) == 1
    assert out[0].text == "partial"
    assert out[0].is_final is False
    # Buffer cleared; second flush returns [].
    assert c.flush() == []


def test_flush_empty_buffer_returns_empty():
    """flush() on an empty buffer returns []."""
    c = TextChunker(session_id="s", utterance_id="u")
    assert c.flush() == []


# ---------- newline boundary ----------


def test_newline_treated_as_punctuation_boundary():
    """A newline is a phrase boundary: buffer ending with "\n" and >= min_chars
    flushes (matches the task brief: punctuation (. , ! ? ; :), newline)."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12)
    out: list[TextChunk] = []
    for tok in ["First line", "\n"]:
        out.extend(c.feed(tok))
    # "First line\n" is 11 chars < 12 — no flush yet.
    assert out == []
    # Add more so we cross min_chars with a newline ending.
    out.extend(c.feed("X\n"))  # now "First line\nX\n" = 13 chars, ends "\n"
    assert len(out) == 1
    assert out[0].text == "First line\nX\n"


# ---------- chunk identity / id uniqueness ----------


def test_emitted_chunk_ids_are_unique():
    """Across multiple flushes, every emitted TextChunk.id is unique and
    non-empty."""
    c = TextChunker(session_id="s", utterance_id="u", min_chars=12, max_chars=80)
    out: list[TextChunk] = []
    for _ in range(160):
        out.extend(c.feed("a"))
    out.extend(c.finalize())
    ids = [ch.id for ch in out]
    assert all(len(i) > 0 for i in ids)
    assert len(set(ids)) == len(ids)
