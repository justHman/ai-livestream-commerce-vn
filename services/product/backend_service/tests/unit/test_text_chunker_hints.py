"""TextChunker runtime-hint integration tests (tasks 5.5-5.6).

Same text under startup/steady/starvation hints can select different valid
first-chunk boundaries (monotonic direction), while the hard invariants never
change: exact preservation, max_chars, sequence order, protected-span safety,
strong-boundary commitment, and finality. Neutral/None/NaN hints are
indistinguishable; the fixed policy ignores hints entirely.
"""

from __future__ import annotations

from collections.abc import Iterable


from backend.application.text_chunker.types import RuntimeHints
from backend.application.text_chunker import TextChunk, TextChunker

STARTUP_HINTS = RuntimeHints(speech_start_elapsed_ms=5000.0)
STEADY_HINTS = RuntimeHints(playback_buffer_ms=6000.0)
STARVATION_HINTS = RuntimeHints(playback_buffer_ms=100.0)
STARTUP_THRESHOLD_HINTS = RuntimeHints(speech_start_elapsed_ms=2500.0)

# Long multi-clause script without sentence-final punctuation, plus compact
# protected forms. Empirically verified: word-fragmented delivery under
# startup/starvation hints commits early ~13-char weak-boundary chunks (target
# 1500/1400ms), while neutral/steady commit ~70-char hard-max chunks.
HINT_SCRIPT = (
    "hôm nay shop mở bán áo khoác mới đẹp giá tốt cho khách hàng thân quen,"
    " mua ngay kẻo hết hàng sale lớn xinh xắn, sản phẩm có mã SKU-P004 giá 199.000đ,"
    " giảm 50% cho khách hàng thân thiết hôm nay, nhanh tay đặt hàng trước khi hết"
)
PROTECTED_TOKENS = ("199.000đ", "50%", "SKU-P004")


def _word_fragments(text: str) -> list[str]:
    parts = text.split(" ")
    return [part + (" " if index < len(parts) - 1 else "") for index, part in enumerate(parts)]


def _run(
    fragments: Iterable[str],
    hints: RuntimeHints | None = None,
    *,
    policy: str = "adaptive_vi",
    finalize_hints: RuntimeHints | None = None,
) -> list[TextChunk]:
    """Feed fragments with ``hints``, finalize with ``finalize_hints`` (defaults
    to the feed hints), returning every emitted chunk."""
    chunker = TextChunker(session_id="s", utterance_id="u", policy=policy)
    chunks: list[TextChunk] = []
    for fragment in fragments:
        chunks.extend(chunker.feed(fragment, runtime_hints=hints))
    chunks.extend(
        chunker.finalize(runtime_hints=finalize_hints if finalize_hints is not None else hints)
    )
    return chunks


def _chunker() -> TextChunker:
    return TextChunker(session_id="s", utterance_id="u", policy="adaptive_vi")


# ---------- startup vs steady vs starvation on identical text ----------


def _hint_scripts(fragments: list[str]) -> dict[str, list[TextChunk]]:
    return {
        "neutral": _run(fragments, None),
        "startup": _run(fragments, STARTUP_HINTS),
        "steady": _run(fragments, STEADY_HINTS),
        "starvation": _run(fragments, STARVATION_HINTS),
    }


def test_same_text_different_hints_emit_different_first_chunk() -> None:
    fragments = _word_fragments(HINT_SCRIPT)
    runs = _hint_scripts(fragments)
    # Exact preservation under every hint state.
    for chunks in runs.values():
        assert "".join(chunk.text for chunk in chunks) == HINT_SCRIPT
        assert [chunk.seq for chunk in chunks] == list(range(len(chunks)))
    # Startup commits sooner than steady: first-chunk end monotonic.
    startup_first = runs["startup"][0].text
    steady_first = runs["steady"][0].text
    starvation_first = runs["starvation"][0].text
    assert len(startup_first) <= len(steady_first)
    assert len(starvation_first) <= len(steady_first)
    # At least one pair of hint states differs in the first chunk.
    assert len({startup_first, steady_first, starvation_first, runs["neutral"][0].text}) >= 2
    # is_final shape: exactly one final chunk, at the end, for every state.
    for chunks in runs.values():
        assert [chunk.is_final for chunk in chunks] == [False] * (len(chunks) - 1) + [True]
        # Every automatic non-final chunk respects the hard cap.
        assert all(len(chunk.text) <= 80 for chunk in chunks[:-1])
    # All boundaries valid: every emitted chunk ends on whitespace/weak
    # punctuation (never mid-word), and no protected token is cut.
    for chunks in runs.values():
        for chunk in chunks[:-1]:
            assert chunk.text[-1].isspace() or chunk.text[-1] in ",;:"
        for token in PROTECTED_TOKENS:
            assert any(token in chunk.text for chunk in chunks)


def test_startup_and_starvation_emit_identical_boundaries() -> None:
    # At 5000ms elapsed the startup target saturates at 1500ms, close to the
    # starvation target 1400ms: on this script both commit the same early
    # weak boundaries.
    fragments = _word_fragments(HINT_SCRIPT)
    startup_chunks = _run(fragments, STARTUP_HINTS)
    starvation_chunks = _run(fragments, STARVATION_HINTS)
    assert [chunk.text for chunk in startup_chunks] == [chunk.text for chunk in starvation_chunks]


# ---------- neutral == pre-hints behavior ----------


def test_neutral_hints_identical_to_no_hints() -> None:
    fragments = _word_fragments(HINT_SCRIPT)
    no_hints = _run(fragments, None)
    empty_hints = _run(fragments, RuntimeHints())
    assert [(chunk.text, chunk.is_final, chunk.decision_reason) for chunk in no_hints] == [
        (chunk.text, chunk.is_final, chunk.decision_reason) for chunk in empty_hints
    ]


def test_neutral_hints_match_pre_hints_segmentation_of_full_script() -> None:
    # A full-script feed+finalize with neutral hints uses the same scorer as
    # the pre-hints adaptive pipeline (single feed, no controller): the
    # segmentation equals the reference boundary segmentation (identical to
    # the pre-hints run in test_text_chunker_adaptive, which also emits three
    # hard_max chunks plus the finalize remainder).
    chunks = _run([HINT_SCRIPT], None)
    assert [chunk.text for chunk in chunks] == [
        "hôm nay shop mở bán áo khoác mới đẹp giá tốt cho khách hàng thân quen,",
        " mua ngay kẻo hết hàng sale lớn xinh xắn,",
        " sản phẩm có mã SKU-P004 giá 199.000đ,",
        " giảm 50% cho khách hàng thân thiết hôm nay, nhanh tay đặt hàng trước khi hết",
    ]
    assert "".join(chunk.text for chunk in chunks) == HINT_SCRIPT


# ---------- hard invariants under hints ----------


def test_strong_boundary_commitment_unchanged_by_hints() -> None:
    # An early sentence boundary commits the same first chunk under neutral,
    # startup, and steady hints: strong boundaries are a pure function of the
    # accumulated prefix and hints never accelerate/delay them.
    text = (
        "Xin chào mọi người. hôm nay shop mở bán áo khoác mới đẹp giá tốt cho khách hàng"
        " thân quen mua ngay kẻo hết hàng sale lớn xinh xắn"
    )
    fragments = [text[index : index + 20] for index in range(0, len(text), 20)]
    firsts = []
    for hints in (None, STARTUP_HINTS, STEADY_HINTS):
        chunks = _run(fragments, hints)
        firsts.append((chunks[0].text, chunks[0].decision_reason))
    assert firsts[0] == ("Xin chào mọi người.", "sentence")
    assert firsts[1] == firsts[0]
    assert firsts[2] == firsts[0]


def test_finalize_with_hints_emits_single_final_chunk() -> None:
    # A short script below min_chars never drains during feed (weak-commit
    # needs end >= min_chars), so finalize emits the whole buffer as exactly
    # one final chunk regardless of hints.
    text = "hôm nay mở bán"
    for hints in (None, STARTUP_HINTS, STEADY_HINTS, STARVATION_HINTS):
        chunker = _chunker()
        chunker.feed(text, runtime_hints=hints)
        final_chunks = chunker.finalize(runtime_hints=hints)
        assert len(final_chunks) == 1
        assert final_chunks[0].text == text
        assert final_chunks[0].is_final is True
        assert final_chunks[0].decision_reason == "finalize"


def test_protected_tokens_intact_under_active_hints() -> None:
    # Fragmented delivery with startup hints must never cut a protected span
    # (only the exact-cap forced split may split one; none occurs here).
    chunks = _run(_word_fragments(HINT_SCRIPT), STARTUP_HINTS)
    for token in PROTECTED_TOKENS:
        assert any(token in chunk.text for chunk in chunks)


# ---------- NaN/None fail-neutral ----------


def test_nan_hints_identical_to_neutral() -> None:
    fragments = _word_fragments(HINT_SCRIPT)
    neutral = _run(fragments, None)
    nan_startup = _run(fragments, RuntimeHints(speech_start_elapsed_ms=float("nan")))
    nan_degraded = _run(
        fragments,
        RuntimeHints(
            playback_buffer_ms=None,
            tts_rtf_ewma=float("nan"),
            tts_first_audio_ewma_ms=float("nan"),
        ),
    )
    for chunks in (nan_startup, nan_degraded):
        assert [(chunk.text, chunk.is_final, chunk.decision_reason) for chunk in chunks] == [
            (chunk.text, chunk.is_final, chunk.decision_reason) for chunk in neutral
        ]


# ---------- fragmentation invariance with hints ----------


def test_fragmentation_invariance_holds_with_startup_hints() -> None:
    # Same text + same startup hints across any fragmentation — one feed(),
    # word-sized fragments, character-sized fragments — produce equivalent
    # emitted chunk texts (no flush calls involved, so the realtime-deadline
    # exception does not apply). The over-cap single feed commits the same
    # earliest-qualifying weak boundary the fragmented runs commit, because
    # the weak-commit rule also fires under cap pressure.
    words = _run(_word_fragments(HINT_SCRIPT), STARTUP_HINTS)
    chars = _run(list(HINT_SCRIPT), STARTUP_HINTS)
    one = _run([HINT_SCRIPT], STARTUP_HINTS)
    assert [chunk.text for chunk in words] == [chunk.text for chunk in chars]
    assert [chunk.text for chunk in words] == [chunk.text for chunk in one]


def test_startup_threshold_elapsed_behaves_neutral() -> None:
    # At exactly the startup late threshold the ramp has zero progress, so
    # the hint state is indistinguishable from neutral hints.
    fragments = _word_fragments(HINT_SCRIPT)
    neutral = _run(fragments, None)
    threshold = _run(fragments, STARTUP_THRESHOLD_HINTS)
    assert [chunk.text for chunk in threshold] == [chunk.text for chunk in neutral]


# ---------- fixed policy ignores hints ----------


def test_fixed_policy_ignores_hints() -> None:
    fragments = _word_fragments(HINT_SCRIPT)
    neutral = _run(fragments, None, policy="fixed")
    starvation = _run(fragments, STARVATION_HINTS, policy="fixed")
    assert [(chunk.text, chunk.decision_reason) for chunk in starvation] == [
        (chunk.text, chunk.decision_reason) for chunk in neutral
    ]
