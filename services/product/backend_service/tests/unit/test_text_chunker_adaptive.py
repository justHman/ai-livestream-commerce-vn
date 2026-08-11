"""Adaptive-policy integration tests (tasks 3.7-3.8).

``TextChunker(policy='adaptive_vi')`` exercises the deterministic Vietnamese
boundary scorer behind the selectable policy switch, while ``fixed`` stays
byte-for-byte the historical behavior. Covers exact-preservation and
fragmentation-invariance under both policies, hard-cap compliance, protected
Vietnamese forms, estimator-failure fallback, and huge single deltas.
"""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from backend.application.text_chunker import TextChunk, TextChunker


VIETNAMESE_TEXT = (
    "Xin chào mọi người. Hôm nay shop có áo khoác SKU-P004 giá 199.000đ, "
    "giảm 50%! Bạn muốn xem màu nào?"
)


def _feed_all(chunker: TextChunker, fragments: Iterable[str]) -> list[TextChunk]:
    """Feed every fragment, collecting all chunks emitted by feed()."""
    emitted: list[TextChunk] = []
    for fragment in fragments:
        emitted.extend(chunker.feed(fragment))
    return emitted


def _fixed_segment(fragments: Iterable[str], **kwargs: object) -> list[str]:
    """Feed fragments then finalize under the fixed policy; return texts."""
    chunker = TextChunker(session_id="s", utterance_id="u", **kwargs)
    chunks = _feed_all(chunker, fragments)
    chunks.extend(chunker.finalize())
    return [chunk.text for chunk in chunks]


def _word_fragments(text: str) -> list[str]:
    """Split on spaces, keeping the trailing space attached to each word."""
    parts = text.split(" ")
    return [part + (" " if index < len(parts) - 1 else "") for index, part in enumerate(parts)]


def _segment_adaptive(fragments: Iterable[str], **kwargs: object) -> list[str]:
    """Feed fragments then finalize under adaptive_vi; return chunk texts."""
    chunker = TextChunker(session_id="s", utterance_id="u", policy="adaptive_vi", **kwargs)
    chunks = _feed_all(chunker, fragments)
    chunks.extend(chunker.finalize())
    return [chunk.text for chunk in chunks]


# ---------- adaptive exact preservation ----------


def test_adaptive_preserves_exact_text_including_remainder() -> None:
    fragments = ["Xin ", "chào 👋. Giá ", "199.000đ", " còn hàng"]
    chunker = TextChunker(session_id="s", utterance_id="u", policy="adaptive_vi")
    emitted = _feed_all(chunker, fragments)
    remainder = "".join(chunk.text for chunk in chunker.finalize())
    assert "".join(chunk.text for chunk in emitted) + remainder == "".join(fragments)


def test_adaptive_keeps_compact_price_token_intact() -> None:
    # Adaptive must not split inside the protected "199.000đ" token (the
    # fixed policy's hard-max rule splits it because it cannot re-join the
    # pieces later).
    chunks = _segment_adaptive([VIETNAMESE_TEXT])
    assert "199.000đ" in "".join(chunks)
    assert any("199.000đ" in chunk for chunk in chunks)


# ---------- adaptive fragmentation invariance ----------


def test_adaptive_full_script_matches_intended_boundaries() -> None:
    expected = [
        "Xin chào mọi người.",
        " Hôm nay shop có áo khoác SKU-P004 giá 199.000đ, giảm 50%!",
        " Bạn muốn xem màu nào?",
    ]
    assert "".join(expected) == VIETNAMESE_TEXT
    assert _segment_adaptive([VIETNAMESE_TEXT]) == expected


@pytest.mark.parametrize(
    "fragments",
    [
        list(VIETNAMESE_TEXT),
        _word_fragments(VIETNAMESE_TEXT),
        [
            VIETNAMESE_TEXT[:19],
            VIETNAMESE_TEXT[19:43],
            VIETNAMESE_TEXT[43:70],
            VIETNAMESE_TEXT[70:91],
            VIETNAMESE_TEXT[91:],
        ],
    ],
    ids=["characters", "words", "provider-deltas"],
)
def test_adaptive_segmentation_invariant_to_fragmentation(fragments: list[str]) -> None:
    assert "".join(fragments) == VIETNAMESE_TEXT
    assert _segment_adaptive(fragments) == _segment_adaptive([VIETNAMESE_TEXT])


# ---------- adaptive hard cap ----------


def test_adaptive_huge_single_delta_drains_to_multiple_capped_chunks() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u", policy="adaptive_vi")
    emitted = chunker.feed("x" * 250)
    assert len(emitted) >= 3
    assert all(len(chunk.text) <= 80 for chunk in emitted)
    assert chunker._buffer_len <= 80
    final = chunker.finalize()
    assert "".join(chunk.text for chunk in emitted + final) == "x" * 250


def test_adaptive_auto_chunks_respect_hard_max_without_text_loss() -> None:
    fragments = ["a" * 75, "b" * 30]
    chunker = TextChunker(session_id="s", utterance_id="u", policy="adaptive_vi")
    emitted = _feed_all(chunker, fragments)
    assert all(len(chunk.text) <= 80 for chunk in emitted)
    assert chunker._buffer_len <= 80
    remainder = "".join(chunk.text for chunk in chunker.finalize())
    assert "".join(chunk.text for chunk in emitted) + remainder == "".join(fragments)


def test_adaptive_whitespace_over_cap_splits_on_word_boundaries() -> None:
    chunker = TextChunker(session_id="s", utterance_id="u", policy="adaptive_vi")
    text = "xin chào " * 15
    emitted = chunker.feed(text)
    assert all(len(chunk.text) <= 80 for chunk in emitted)
    assert all(chunk.text[-1] == " " for chunk in emitted)
    assert "".join(chunk.text for chunk in emitted) + chunker.buffered_text == text


# ---------- protected Vietnamese forms under both policies ----------


PROTECTED_FORMS_TEXT = (
    "Mã SKU-P004 giá 199.000đ, giảm 50% hôm nay. Xem tại https://example.com nhé!"
)


def _all_chunks(chunker: TextChunker, text: str) -> list[TextChunk]:
    chunks = chunker.feed(text)
    chunks.extend(chunker.finalize())
    return chunks


@pytest.mark.parametrize("policy", ["fixed", "adaptive_vi"])
def test_protected_forms_text_preserved_exactly_under_policy(policy: str) -> None:
    # Exact reconstruction is a hard invariant under BOTH policies; the fixed
    # policy may split inside a protected token (punctuation-driven, no
    # protection), so intactness is asserted only for adaptive below.
    chunker = TextChunker(session_id="s", utterance_id="u", policy=policy)
    chunks = _all_chunks(chunker, PROTECTED_FORMS_TEXT)
    assert "".join(chunk.text for chunk in chunks) == PROTECTED_FORMS_TEXT


def test_adaptive_keeps_protected_forms_intact() -> None:
    # The adaptive scorer hard-excludes protected candidates, so compact
    # price/percent/URL/SKU tokens survive as whole spans.
    chunker = TextChunker(session_id="s", utterance_id="u", policy="adaptive_vi")
    chunks = _all_chunks(chunker, PROTECTED_FORMS_TEXT)
    assert "".join(chunk.text for chunk in chunks) == PROTECTED_FORMS_TEXT
    for token in ["SKU-P004", "199.000đ", "50%", "https://example.com"]:
        assert any(token in chunk.text for chunk in chunks)


# ---------- estimator-failure fallback ----------


class _BrokenEstimator:
    def estimate_ms(self, text: str) -> float:  # noqa: ARG002
        return float("nan")


class _ThrowingEstimator:
    def estimate_ms(self, text: str) -> float:  # noqa: ARG002
        raise RuntimeError("estimator exploded")


@pytest.mark.parametrize("estimator", [_BrokenEstimator(), _ThrowingEstimator()])
def test_adaptive_falls_back_to_fixed_without_text_loss(estimator: object) -> None:
    text = "xin chào " * 20
    chunker = TextChunker(
        session_id="s", utterance_id="u", policy="adaptive_vi", estimator=estimator
    )
    chunks = _all_chunks(chunker, text)
    assert "".join(chunk.text for chunk in chunks) == text
    assert [chunk.seq for chunk in chunks] == list(range(len(chunks)))
    # Hard cap holds on every automatic non-final chunk (final may be sub-min).
    assert all(len(chunk.text) <= 80 for chunk in chunks[:-1])
    assert chunker.fallback_active is True
    # After the failure the chunker stamps fixed_fallback; text is never
    # dropped or reordered.
    fallback_chunks = [chunk for chunk in chunks if chunk.decision_reason == "fixed_fallback"]
    assert fallback_chunks


def test_adaptive_fallback_does_not_duplicate_or_reorder() -> None:
    text = "xin chào mọi người. Hôm nay tôi đi chợ sớm mua đồ."
    chunker = TextChunker(
        session_id="s", utterance_id="u", policy="adaptive_vi", estimator=_ThrowingEstimator()
    )
    chunks = _all_chunks(chunker, text)
    assert "".join(chunk.text for chunk in chunks) == text
    assert [chunk.seq for chunk in chunks] == list(range(len(chunks)))


# ---------- fixed stays identical ----------


def test_fixed_policy_unchanged_under_adaptive_chunker() -> None:
    assert _fixed_segment([VIETNAMESE_TEXT], min_chars=12, target_chars=40, max_chars=80) == [
        "Xin chào mọi người.",
        " Hôm nay shop có áo khoác SKU-P004 giá 199.",
        "000đ, giảm 50%!",
        " Bạn muốn xem màu nào?",
    ]
