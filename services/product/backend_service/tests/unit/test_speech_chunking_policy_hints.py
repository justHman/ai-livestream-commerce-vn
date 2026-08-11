"""Runtime-hint law tests (tasks 5.1-5.5) for adaptive Vietnamese chunking.

Covers ``soft_target_duration_ms`` (startup/steady/starvation control law)
and hint-driven selection: identical text and identical policy constants can
select different *valid* boundaries under different hints, while the hard
invariants (min/max chars, protected spans, hard cap) never change.
"""

from __future__ import annotations


from backend.application.text_chunker.boundaries import (
    BoundaryCandidate,
    CandidateKind,
    extract_candidates,
)
from backend.application.text_chunker.duration import SpeechDurationEstimator
from backend.application.text_chunker.policy import (
    FIRST_AUDIO_SLOW_MS,
    HEALTHY_WATERMARK_MS,
    MAX_SOFT_TARGET_MS,
    MIN_SOFT_TARGET_MS,
    RTF_DEGRADED_THRESHOLD,
    STARTUP_EARLY_TARGET_MS,
    STARTUP_LATE_ELAPSED_MS,
    STARVATION_TARGET_MS,
    STARVATION_WATERMARK_MS,
    STEADY_TARGET_MS,
    TARGET_DURATION_MS,
    AdaptiveViPolicyConfig,
    select_boundary,
    soft_target_duration_ms,
)
from backend.application.text_chunker.types import RuntimeHints

_ESTIMATOR = SpeechDurationEstimator()


def _candidates(text: str, max_chars: int = 80) -> list[BoundaryCandidate]:
    return extract_candidates(text, max_chars)


def _selected_end(
    text: str,
    *,
    min_chars: int = 12,
    target_chars: int = 40,
    max_chars: int = 80,
    runtime_hints: RuntimeHints | None = None,
) -> int | None:
    selected = select_boundary(
        text,
        _candidates(text, max_chars),
        estimator=_ESTIMATOR,
        config=AdaptiveViPolicyConfig(
            min_chars=min_chars, max_chars=max_chars, char_bias_chars=target_chars
        ),
        runtime_hints=runtime_hints,
    )
    return None if selected is None else selected.candidate.end


def _is_real_end(text: str, end: int, min_chars: int = 12, max_chars: int = 80) -> bool:
    """True when ``end`` is a real non-protected candidate end in bounds."""
    return any(
        c.end == end and not c.protected and min_chars <= end <= max_chars
        for c in _candidates(text, max_chars)
    )


# ---------- neutral hints ----------


def test_neutral_hints_keep_base_target() -> None:
    assert soft_target_duration_ms(None) == TARGET_DURATION_MS
    assert soft_target_duration_ms(RuntimeHints()) == TARGET_DURATION_MS


def test_none_and_nan_hint_fields_are_neutral() -> None:
    # None-valued and NaN-valued hint fields carry no signal: the base target
    # holds and no exception is raised.
    assert (
        soft_target_duration_ms(RuntimeHints(speech_start_elapsed_ms=float("nan")))
        == TARGET_DURATION_MS
    )
    assert (
        soft_target_duration_ms(
            RuntimeHints(
                speech_start_elapsed_ms=0.0,
                playback_buffer_ms=None,
                tts_rtf_ewma=float("nan"),
                tts_first_audio_ewma_ms=float("nan"),
            )
        )
        == TARGET_DURATION_MS
    )


# ---------- startup law ----------


def test_startup_late_elapsed_at_threshold_stays_below_min_floor() -> None:
    # At 5x the late threshold the target saturates at STARTUP_EARLY_TARGET_MS,
    # never below MIN_SOFT_TARGET_MS.
    target = soft_target_duration_ms(
        RuntimeHints(speech_start_elapsed_ms=5 * STARTUP_LATE_ELAPSED_MS)
    )
    assert target == STARTUP_EARLY_TARGET_MS
    assert STARTUP_EARLY_TARGET_MS >= MIN_SOFT_TARGET_MS


def test_startup_never_raises_target_and_saturates_at_early_target() -> None:
    # Increasing speech-start elapsed never raises the target; beyond the
    # late threshold the target stays below base, never below the minimum,
    # and saturates at STARTUP_EARLY_TARGET_MS for very late speech.
    elapsed_values = [
        STARTUP_LATE_ELAPSED_MS * 0.5,
        STARTUP_LATE_ELAPSED_MS,
        STARTUP_LATE_ELAPSED_MS * 1.1,
        STARTUP_LATE_ELAPSED_MS * 1.5,
        STARTUP_LATE_ELAPSED_MS * 2.0,
        STARTUP_LATE_ELAPSED_MS * 5.0,
    ]
    previous: float | None = None
    for elapsed in elapsed_values:
        target = soft_target_duration_ms(RuntimeHints(speech_start_elapsed_ms=elapsed))
        if previous is not None:
            assert target <= previous
        previous = target
    # Strictly past the late threshold (progress > 0) the target drops below
    # the base; at exactly the threshold the ramp has zero progress.
    late = soft_target_duration_ms(
        RuntimeHints(speech_start_elapsed_ms=STARTUP_LATE_ELAPSED_MS * 1.01)
    )
    assert late < TARGET_DURATION_MS
    assert late >= MIN_SOFT_TARGET_MS
    assert (
        soft_target_duration_ms(RuntimeHints(speech_start_elapsed_ms=2 * STARTUP_LATE_ELAPSED_MS))
        <= STARTUP_EARLY_TARGET_MS
    )
    assert (
        soft_target_duration_ms(RuntimeHints(speech_start_elapsed_ms=10 * STARTUP_LATE_ELAPSED_MS))
        == STARTUP_EARLY_TARGET_MS
    )


def test_startup_adjustment_requires_no_audio_signals() -> None:
    # The startup ramp applies ONLY while both playback buffer and first-audio
    # EWMA are absent (the gate in soft_target_duration_ms). Once first-audio
    # is observed (even at a fast 100ms EWMA), the late-elapsed signal no
    # longer shrinks the target — the buffer fields express their own signal
    # (a present low buffer still starves via its own branch).
    late = soft_target_duration_ms(
        RuntimeHints(speech_start_elapsed_ms=2 * STARTUP_LATE_ELAPSED_MS)
    )
    assert late < TARGET_DURATION_MS
    with_first_audio = soft_target_duration_ms(
        RuntimeHints(
            speech_start_elapsed_ms=2 * STARTUP_LATE_ELAPSED_MS,
            tts_first_audio_ewma_ms=100.0,
        )
    )
    assert with_first_audio == TARGET_DURATION_MS
    with_healthy_buffer = soft_target_duration_ms(
        RuntimeHints(
            speech_start_elapsed_ms=2 * STARTUP_LATE_ELAPSED_MS,
            playback_buffer_ms=HEALTHY_WATERMARK_MS + 1.0,
        )
    )
    # A present healthy buffer suppresses the startup ramp (gate) and raises
    # the target to the steady target instead.
    assert with_healthy_buffer == STEADY_TARGET_MS


# ---------- steady law ----------


def test_steady_raises_target_to_steady_target() -> None:
    # A healthy playback buffer raises the target to exactly STEADY_TARGET_MS
    # (clamped by MAX_SOFT_TARGET_MS).
    target = soft_target_duration_ms(RuntimeHints(playback_buffer_ms=HEALTHY_WATERMARK_MS + 1.0))
    assert target == STEADY_TARGET_MS
    assert STEADY_TARGET_MS <= MAX_SOFT_TARGET_MS
    assert target == min(STEADY_TARGET_MS, MAX_SOFT_TARGET_MS)


# ---------- starvation law ----------


def test_starvation_signals_each_shrink_target() -> None:
    # Each starvation signal is independent: low playback buffer, degraded TTS
    # RTF, or slow first audio all shrink the target to STARVATION_TARGET_MS.
    low_buffer = soft_target_duration_ms(
        RuntimeHints(playback_buffer_ms=STARVATION_WATERMARK_MS - 1.0)
    )
    assert low_buffer == STARVATION_TARGET_MS
    degraded_rtf = soft_target_duration_ms(RuntimeHints(tts_rtf_ewma=RTF_DEGRADED_THRESHOLD))
    assert degraded_rtf == STARVATION_TARGET_MS
    slow_first_audio = soft_target_duration_ms(
        RuntimeHints(tts_first_audio_ewma_ms=FIRST_AUDIO_SLOW_MS)
    )
    assert slow_first_audio == STARVATION_TARGET_MS


def test_starvation_wins_over_steady() -> None:
    # Steady + starvation signals together: the smallest (starvation) target
    # wins.
    mixed = soft_target_duration_ms(
        RuntimeHints(
            playback_buffer_ms=HEALTHY_WATERMARK_MS + 1.0,
            tts_rtf_ewma=RTF_DEGRADED_THRESHOLD,
        )
    )
    assert mixed == STARVATION_TARGET_MS


# ---------- hint-driven selection ----------

# Identical text where neutral (2200ms) and startup (1500ms) targets score
# different comma candidates under hard-cap pressure; startup and starvation
# also beat steady here. Empirically verified: neutral/steady pick end 29,
# startup/starvation pick end 16 (target_chars=20).
_FLIP_TEXT = "hôm nay shop mở,bán áo khoác,mới đẹp giá tốt cho khách hàng thân quen mua ngay kẻo"
_FLIP_TARGET_CHARS = 20
_STARTUP_HINTS = RuntimeHints(speech_start_elapsed_ms=2 * STARTUP_LATE_ELAPSED_MS)
_STEADY_HINTS = RuntimeHints(playback_buffer_ms=HEALTHY_WATERMARK_MS + 1.0)
_STARVATION_HINTS = RuntimeHints(playback_buffer_ms=STARVATION_WATERMARK_MS - 1.0)


def test_neutral_and_late_startup_select_different_valid_ends() -> None:
    neutral_end = _selected_end(_FLIP_TEXT, target_chars=_FLIP_TARGET_CHARS, runtime_hints=None)
    startup_end = _selected_end(
        _FLIP_TEXT, target_chars=_FLIP_TARGET_CHARS, runtime_hints=_STARTUP_HINTS
    )
    assert neutral_end is not None and startup_end is not None
    assert neutral_end != startup_end
    # Both selections are valid: real candidate ends within [min_chars, max_chars].
    assert _is_real_end(_FLIP_TEXT, neutral_end)
    assert _is_real_end(_FLIP_TEXT, startup_end)


def test_startup_target_below_steady_selects_earlier_or_equal_end() -> None:
    startup_target = soft_target_duration_ms(_STARTUP_HINTS)
    steady_target = soft_target_duration_ms(_STEADY_HINTS)
    assert startup_target <= steady_target
    startup_end = _selected_end(
        _FLIP_TEXT, target_chars=_FLIP_TARGET_CHARS, runtime_hints=_STARTUP_HINTS
    )
    steady_end = _selected_end(
        _FLIP_TEXT, target_chars=_FLIP_TARGET_CHARS, runtime_hints=_STEADY_HINTS
    )
    assert startup_end is not None and steady_end is not None
    assert startup_end <= steady_end


def test_starvation_selects_earlier_or_equal_end_than_steady() -> None:
    starvation_end = _selected_end(
        _FLIP_TEXT, target_chars=_FLIP_TARGET_CHARS, runtime_hints=_STARVATION_HINTS
    )
    steady_end = _selected_end(
        _FLIP_TEXT, target_chars=_FLIP_TARGET_CHARS, runtime_hints=_STEADY_HINTS
    )
    assert starvation_end is not None and steady_end is not None
    assert starvation_end <= steady_end


def test_hard_cap_enforced_under_active_hints() -> None:
    # With startup hints active a split over the cap still respects
    # end <= max_chars. The earliest-qualifying weak boundary now wins
    # (forced=False); the exact-cap forced split survives only when no weak
    # candidate at all qualifies.
    selected = select_boundary(
        _FLIP_TEXT,
        _candidates(_FLIP_TEXT, 80),
        estimator=_ESTIMATOR,
        config=AdaptiveViPolicyConfig(
            min_chars=12, max_chars=80, char_bias_chars=_FLIP_TARGET_CHARS
        ),
        runtime_hints=_STARTUP_HINTS,
    )
    assert selected is not None
    assert selected.candidate.end <= 80
    assert selected.forced is False


def test_hard_cap_forced_split_when_no_weak_candidate_with_hints() -> None:
    # A single long unbroken token over the cap has no weak boundary: the
    # exact-cap forced split fires with forced=True even under startup hints.
    text = "X" * 120
    selected = select_boundary(
        text,
        _candidates(text, 80),
        estimator=_ESTIMATOR,
        config=AdaptiveViPolicyConfig(min_chars=12, max_chars=80, char_bias_chars=40),
        runtime_hints=_STARTUP_HINTS,
    )
    assert selected is not None
    assert selected.candidate.end == 80
    assert selected.forced is True
    assert selected.candidate.kind == CandidateKind.HARD_CAP


def test_protected_candidate_never_selected_while_safe_exists_with_hints() -> None:
    # A candidate cutting inside a protected span is never selected while a
    # safe candidate exists — under any hint state.
    text = 'Cô ấy nói "Đi ngay!" rồi cười. Sau đó im lặng.'
    candidates = _candidates(text)
    protected_ends = [c.end for c in candidates if c.protected]
    assert protected_ends  # guard: a protected candidate exists
    for hints in (None, _STARTUP_HINTS, _STEADY_HINTS, _STARVATION_HINTS):
        selected = select_boundary(
            text,
            candidates,
            estimator=_ESTIMATOR,
            config=AdaptiveViPolicyConfig(min_chars=12, max_chars=80, char_bias_chars=40),
            runtime_hints=hints,
        )
        assert selected is not None
        assert not selected.candidate.protected


def test_weak_commit_with_hints_holds_over_cap() -> None:
    # Under downward pressure the earliest-qualifying weak boundary fires
    # even when the buffer already exceeds the cap: the same text selected
    # under the same hints yields the same end whether or not a hard-cap
    # candidate exists (fragmentation invariance for one-feed vs fragmented
    # delivery). The committed boundary is a real weak candidate end in
    # [min_chars, max_chars] — never the exact-cap forced split.
    selected = select_boundary(
        _FLIP_TEXT,
        _candidates(_FLIP_TEXT, 80),
        estimator=_ESTIMATOR,
        config=AdaptiveViPolicyConfig(
            min_chars=12, max_chars=80, char_bias_chars=_FLIP_TARGET_CHARS
        ),
        runtime_hints=_STARTUP_HINTS,
    )
    assert selected is not None
    assert selected.forced is False
    assert 12 <= selected.candidate.end <= 80
    assert selected.candidate.kind not in (
        CandidateKind.HARD_CAP,
        CandidateKind.PARAGRAPH,
        CandidateKind.SENTENCE,
    )
