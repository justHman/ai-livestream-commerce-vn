"""Regression tests for adaptive speech-text chunking (tasks 1.1-1.5).

Section 1.1 locks baseline behavior (must pass at commit 486b4f5); section
1.2 pins exact text preservation. Sections 1.3-1.5 encode the intended fixed
behavior and FAIL at baseline: they pin fragmentation invariance, internal-
punctuation draining, and hard-cap compliance the current chunker lacks.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Callable

import pytest

from backend.application.text_chunker import TextChunk, TextChunker


VIETNAMESE_TEXT = (
    "Xin chào mọi người. Hôm nay shop có áo khoác SKU-P004 giá 199.000đ, "
    "giảm 50%! Bạn muốn xem màu nào?"
)


# Oracle for 1.3/1.4: fixed segmentation of VIETNAMESE_TEXT, derived from
# character-at-a-time feeding. Every punctuation position is a candidate
# boundary and min_chars=12 gates each split; max_chars=80 never binds here
# (every oracle chunk is well under it).
FULL_SCRIPT_EXPECTED = [
    "Xin chào mọi người.",
    " Hôm nay shop có áo khoác SKU-P004 giá 199.",
    "000đ, giảm 50%!",
    " Bạn muốn xem màu nào?",
]


# ---------- helpers / fakes ----------


def make_clock(start: float = 0.0) -> tuple[Callable[[], float], Callable[[float], None]]:
    """Return (clock, advance) where clock() reads and advance(dt) bumps a
    shared timestamp, giving deterministic timeout behaviour without real
    time. Units: seconds (matches time.monotonic, the chunker default)."""
    now = [start]

    def clock() -> float:
        return now[0]

    def advance(delta_seconds: float) -> None:
        now[0] += delta_seconds

    return clock, advance


def _feed_all(chunker: TextChunker, fragments: Iterable[str]) -> list[TextChunk]:
    """Feed every fragment, collecting all chunks emitted by feed()."""
    emitted: list[TextChunk] = []
    for fragment in fragments:
        emitted.extend(chunker.feed(fragment))
    return emitted


def _segment(fragments: Iterable[str], **kwargs: object) -> list[str]:
    """Feed fragments then finalize; return the emitted chunk texts."""
    chunker = TextChunker(session_id="s", utterance_id="u", **kwargs)
    chunks = _feed_all(chunker, fragments)
    chunks.extend(chunker.finalize())
    return [chunk.text for chunk in chunks]


def _word_fragments(text: str) -> list[str]:
    """Split on spaces, keeping the trailing space attached to each word."""
    parts = text.split(" ")
    return [part + (" " if index < len(parts) - 1 else "") for index, part in enumerate(parts)]


def _provider_fragments(text: str) -> list[str]:
    """Simulate provider deltas arriving at fixed character offsets."""
    split_points = (19, 43, 70, 91)
    return [
        text[: split_points[0]],
        text[split_points[0] : split_points[1]],
        text[split_points[1] : split_points[2]],
        text[split_points[2] : split_points[3]],
        text[split_points[3] :],
    ]


# ---------- 1.1 fixed-baseline behavior lock ----------


def test_default_thresholds_lock() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u")
    assert (chunker.min_chars, chunker.target_chars, chunker.max_chars) == (12, 40, 80)


def test_default_timeout_does_not_flush_before_350ms() -> None:
    clock, advance = make_clock()
    chunker = TextChunker(session_id="s", utterance_id="u", clock=clock)
    chunker.feed("hello world x")  # 13 chars, no punctuation
    advance(0.349)
    assert chunker.check_timeout() == []


def test_default_timeout_flushes_at_350ms() -> None:
    clock, advance = make_clock()
    chunker = TextChunker(session_id="s", utterance_id="u", clock=clock)
    chunker.feed("hello world x")  # 13 chars, no punctuation
    advance(0.350)
    flushed = chunker.check_timeout()
    assert [chunk.text for chunk in flushed] == ["hello world x"]
    assert flushed[0].is_final is False


def test_punctuation_flush_emits_single_non_final_chunk() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u")
    chunker.feed("Xin ")
    chunker.feed("chào ")
    emitted = chunker.feed("bạn.")
    assert [chunk.text for chunk in emitted] == ["Xin chào bạn."]
    assert emitted[0].seq == 0
    assert emitted[0].is_final is False


def test_sub_min_feed_emits_nothing_then_finalize() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u")
    assert chunker.feed("hi") == []
    final = chunker.finalize()
    assert [chunk.text for chunk in final] == ["hi"]
    assert final[0].is_final is True


def test_timeout_respects_min_chars_on_sub_min_buffer() -> None:
    clock, advance = make_clock()
    chunker = TextChunker(session_id="s", utterance_id="u", clock=clock)
    chunker.feed("hello")  # 5 chars < min_chars
    advance(1.0)
    assert chunker.check_timeout() == []
    final = chunker.finalize()
    assert [chunk.text for chunk in final] == ["hello"]
    assert final[0].is_final is True


def test_finalize_empty_buffer_returns_empty() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u")
    assert chunker.finalize() == []


def test_finalize_emits_remainder_as_final_chunk() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u")
    chunker.feed("short")
    final = chunker.finalize()
    assert [chunk.text for chunk in final] == ["short"]
    assert final[0].is_final is True


def test_seq_increments_across_punct_max_and_finalize() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u", max_chars=80)
    emitted: list[TextChunk] = []
    emitted.extend(chunker.feed("Hello "))
    emitted.extend(chunker.feed("world."))  # punct flush
    emitted.extend(chunker.feed("a" * 80))  # max flush
    chunker.feed("tail")
    emitted.extend(chunker.finalize())
    assert [chunk.seq for chunk in emitted] == [0, 1, 2]
    assert [chunk.is_final for chunk in emitted] == [False, False, True]


def test_chunk_ids_unique_and_non_empty_across_flush_types() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u", max_chars=80)
    emitted: list[TextChunk] = []
    emitted.extend(chunker.feed("Hello "))
    emitted.extend(chunker.feed("world."))
    emitted.extend(chunker.feed("a" * 80))
    chunker.feed("tail")
    emitted.extend(chunker.finalize())
    ids = [chunk.id for chunk in emitted]
    assert len(set(ids)) == len(ids)
    assert all(len(chunk_id) > 0 for chunk_id in ids)


# ---------- 1.2 exact text preservation ----------


def test_exact_text_preservation_including_finalize_remainder() -> None:
    fragments = ["Xin ", "chào 👋. Giá ", "199.000đ", " còn hàng"]
    chunker = TextChunker(
        session_id="s", utterance_id="u", min_chars=4, target_chars=12, max_chars=24
    )
    emitted = _feed_all(chunker, fragments)
    remainder = "".join(chunk.text for chunk in chunker.finalize())
    assert "".join(chunk.text for chunk in emitted) + remainder == "".join(fragments)


# ---------- 1.3 fragmentation invariance (regression: fails at baseline) ----------


def test_full_script_segmentation_matches_intended_boundaries() -> None:
    # Fixture integrity: the oracle must reproduce VIETNAMESE_TEXT exactly.
    assert "".join(FULL_SCRIPT_EXPECTED) == VIETNAMESE_TEXT
    assert _segment([VIETNAMESE_TEXT], min_chars=12, target_chars=40, max_chars=80) == (
        FULL_SCRIPT_EXPECTED
    )


@pytest.mark.parametrize(
    "fragments",
    [
        _word_fragments(VIETNAMESE_TEXT),
        list(VIETNAMESE_TEXT),
        [
            "Xin chào mọi người. ",
            "Hôm nay shop có áo khoác SKU-P004 giá 199.000đ, ",
            "giảm 50%! ",
            "Bạn muốn xem màu nào?",
        ],
        _provider_fragments(VIETNAMESE_TEXT),
    ],
    ids=["words", "characters", "punctuation-coalesced", "provider-deltas"],
)
def test_segmentation_is_invariant_to_fragmentation(fragments: list[str]) -> None:
    assert "".join(fragments) == VIETNAMESE_TEXT
    assert _segment(fragments, min_chars=12, target_chars=40, max_chars=80) == (
        FULL_SCRIPT_EXPECTED
    )


# ---------- 1.4 internal-punctuation draining (regression: fails at baseline) ----------


def test_punctuation_inside_one_delta_drains_multiple_chunks() -> None:
    text = "Xin chào mọi người. Hôm nay shop có ưu đãi lớn! Bạn cần tư vấn gì?"
    chunks = _segment([text], min_chars=12, target_chars=40, max_chars=80)
    assert chunks == [
        "Xin chào mọi người.",
        " Hôm nay shop có ưu đãi lớn!",
        " Bạn cần tư vấn gì?",
    ]


def test_single_feed_call_can_return_multiple_chunks() -> None:
    text = "Xin chào mọi người. Hôm nay shop có ưu đãi lớn! Bạn cần tư vấn gì?"
    chunker = TextChunker(session_id="s", utterance_id="u")
    emitted = chunker.feed(text)
    assert len(emitted) >= 2


# ---------- 1.5 hard-cap compliance (regression: fails at baseline) ----------


@pytest.mark.parametrize(
    ("fragments", "max_chars"),
    [
        (["a" * 75, "b" * 30], 80),
        (["x" * 190], 80),
    ],
    ids=["buffer-plus-delta", "single-delta-over-two-caps"],
)
def test_automatic_chunks_respect_hard_max_without_text_loss(
    fragments: list[str], max_chars: int
) -> None:
    chunker = TextChunker(
        session_id="s",
        utterance_id="u",
        min_chars=12,
        target_chars=40,
        max_chars=max_chars,
    )
    emitted = _feed_all(chunker, fragments)

    # No automatic chunk may exceed the hard cap...
    assert all(len(chunk.text) <= max_chars for chunk in emitted)
    # ...and no oversized remainder may be left for finalize() to hide.
    # Private-buffer inspection is a stopgap: task 2.2 adds a public
    # buffered-text state to assert against instead.
    assert chunker._buffer_len <= max_chars

    remainder = "".join(chunk.text for chunk in chunker.finalize())
    assert "".join(chunk.text for chunk in emitted) + remainder == "".join(fragments)


def test_single_delta_over_two_caps_emits_at_least_two_automatic_chunks() -> None:
    chunker = TextChunker(
        session_id="s",
        utterance_id="u",
        min_chars=12,
        target_chars=40,
        max_chars=80,
    )
    emitted = chunker.feed("x" * 190)

    # 190 > 2 * 80: one 80-char cut cannot leave a <= max remainder, so the
    # drain must emit at least two automatic chunks.
    assert len(emitted) >= 2
    assert all(len(chunk.text) <= 80 for chunk in emitted)
    assert chunker._buffer_len <= 80
