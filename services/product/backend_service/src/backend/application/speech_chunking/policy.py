"""Deterministic candidate scoring for adaptive Vietnamese chunking (task 3.6).

Pure functions over the boundary candidates produced by ``boundaries.py`` and
the duration estimates produced by ``duration.py``. Composite score
(``score_boundary``), lower is better, precedence in this exact order:

1. **linguistic quality** — ``CandidateKind`` rank (paragraph > sentence >
   clause > comma > cue > whitespace > hard-cap). A stronger kind never loses
   to a weaker one; the hard-cap candidate ranks weakest so a real natural
   boundary is preferred whenever one exists.
2. **estimated-duration proximity** — head-duration distance to
   ``TARGET_DURATION_MS`` (the primary soft size signal, design Decision 5):
   compact written forms (``199.000đ``, ``50%``) size chunks by spoken
   length, not raw characters.
3. **``target_chars`` tie-break/fallback** — head-length distance to
   ``target_chars`` breaks ties when duration is equal (design Decision 7).

Selection (``select_boundary``):

- A **strong** boundary (paragraph/sentence punctuation) is committed as soon
  as it is available and not protected. Committing the *earliest* such
  boundary is deterministic and invariant to input fragmentation: the first
  strong boundary is a function of the accumulated prefix alone, exactly like
  the fixed policy's first-qualifying-punctuation rule. Weak candidates
  (clause/comma/cue/whitespace) are never committed prematurely — they only
  decide forced hard-cap splits (see below).
- A candidate cutting inside a **protected span** is hard-excluded whenever a
  safe candidate exists (design Decision 4). Protected boundaries are held
  (never auto-committed) and split only at finalize or as a forced-cap last
  resort (the caller stamps ``protected_span_fallback``).
- The **hard cap** wins only when no natural boundary exists at or before
  ``max_chars``. When the buffer exceeds the cap and only weak natural
  boundaries exist, the best weak boundary by composite score is committed so
  a large single delta drains into multiple ``<= max_chars`` chunks; with no
  natural boundary at all, the exact-cap forced split is the only progress
  guarantee.

``runtime_hints`` adjust the soft duration target (task 5.1/5.2): startup
(no audio yet, speech late), steady (playback buffer healthy), and starvation
(playback buffer low, degraded TTS RTF/first-audio) move the target within
``[MIN_SOFT_TARGET_MS, MAX_SOFT_TARGET_MS]``. The target changes only which
weak boundaries score best and when a weak boundary may be committed early;
strong boundaries, the hard cap, and the min/max character invariants are
unchanged. Neutral (None / missing / NaN) hints keep ``TARGET_DURATION_MS``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .boundaries import BoundaryCandidate, CandidateKind
from .duration import SpeechDurationEstimator
from .types import ChunkDecisionReason, RuntimeHints

__all__ = [
    "AdaptiveAnalysisError",
    "SelectedBoundary",
    "select_boundary",
    "score_boundary",
    "chunk_decision_reason",
    "soft_target_duration_ms",
    "TARGET_DURATION_MS",
    "MIN_SOFT_TARGET_MS",
    "MAX_SOFT_TARGET_MS",
    "STEADY_TARGET_MS",
    "STARTUP_LATE_ELAPSED_MS",
    "STARTUP_EARLY_TARGET_MS",
    "STARVATION_WATERMARK_MS",
    "STARVATION_TARGET_MS",
    "HEALTHY_WATERMARK_MS",
    "RTF_DEGRADED_THRESHOLD",
    "FIRST_AUDIO_SLOW_MS",
]


# Soft duration target for adaptive_vi (baseline). Module-level and fixed
# (not a constructor knob) so the chunking stack exposes no config surface;
# runtime adaptation is driven by the hint law in ``soft_target_duration_ms``.
TARGET_DURATION_MS = 2200.0

# Soft-target law constants (all module-level, all documented). The law
# (``soft_target_duration_ms``) adjusts the target within the [MIN, MAX]
# clamp; startup/steady/starvation signals push monotonic directions:
# - startup: no audio observed yet and speech is late — the target shrinks
#   linearly from TARGET_DURATION_MS (at STARTUP_LATE_ELAPSED_MS) down to
#   STARTUP_EARLY_TARGET_MS (at 2*STARTUP_LATE_ELAPSED_MS), so the first
#   chunk commits sooner and the viewer hears something faster.
# - steady: playback buffer at/above HEALTHY_WATERMARK_MS — chunks may grow
#   up to STEADY_TARGET_MS (more natural pacing, fewer chunks).
# - starvation: playback buffer below STARVATION_WATERMARK_MS, or degraded
#   TTS (RTF at/above RTF_DEGRADED_THRESHOLD, or first-audio EWMA at/above
#   FIRST_AUDIO_SLOW_MS) — chunks shrink to STARVATION_TARGET_MS so the next
#   commit is sooner and the renderer refills faster.
MIN_SOFT_TARGET_MS = 1200.0
MAX_SOFT_TARGET_MS = 3200.0
STEADY_TARGET_MS = 2800.0
STARTUP_LATE_ELAPSED_MS = 2500.0
STARTUP_EARLY_TARGET_MS = 1500.0
STARVATION_WATERMARK_MS = 1500.0
STARVATION_TARGET_MS = 1400.0
HEALTHY_WATERMARK_MS = 5000.0
RTF_DEGRADED_THRESHOLD = 1.5
FIRST_AUDIO_SLOW_MS = 3000.0

# Composite-score weights. Kind dominates (a stronger kind never loses to a
# weaker one); within a kind, duration dominates character count so estimated
# spoken length is the primary soft size signal; character distance to
# target_chars is the tie-break/fallback.
_KIND_WEIGHT = 1_000_000.0
_DURATION_WEIGHT = 1000.0
_CHAR_WEIGHT = 1.0

# A finite duration target must survive score arithmetic; validated at import
# so a misconfigured constant fails loudly instead of silently poisoning every
# score comparison.
if not math.isfinite(TARGET_DURATION_MS) or TARGET_DURATION_MS <= 0:
    raise ValueError(f"TARGET_DURATION_MS must be finite and > 0, got {TARGET_DURATION_MS}")


class AdaptiveAnalysisError(Exception):
    """Adaptive analysis produced an unusable result (e.g. non-finite duration).

    The chunker catches this (and any exception from the adaptive path) and
    fails closed to deterministic fixed segmentation for the current
    utterance, stamping ``fixed_fallback``.
    """


@dataclass(frozen=True)
class SelectedBoundary:
    """One selected split plus whether the hard cap forced the decision."""

    candidate: BoundaryCandidate
    forced: bool


def chunk_decision_reason(kind: CandidateKind) -> ChunkDecisionReason:
    """Decision-reason for a non-cap candidate kind."""
    if kind == CandidateKind.PARAGRAPH:
        return ChunkDecisionReason.PARAGRAPH
    if kind == CandidateKind.SENTENCE:
        return ChunkDecisionReason.SENTENCE
    if kind == CandidateKind.CLAUSE:
        return ChunkDecisionReason.CLAUSE
    if kind == CandidateKind.COMMA:
        return ChunkDecisionReason.CLAUSE
    return ChunkDecisionReason.TARGET


def _require_duration(estimator: SpeechDurationEstimator, text: str) -> float:
    """Estimated duration of ``text``, raising on a non-finite result.

    NaN/inf/negative means the estimator is broken; the adaptive analysis
    must fail closed rather than rank candidates on garbage.
    """
    duration = estimator.estimate_ms(text)
    if not math.isfinite(duration) or duration < 0:
        raise AdaptiveAnalysisError(f"non-finite duration estimate {duration!r}")
    return duration


def _hint_neutral(value: Optional[float]) -> bool:
    """True when a single hint value carries no signal (None or non-finite)."""
    return value is None or not math.isfinite(value)


def soft_target_duration_ms(runtime_hints: Optional[RuntimeHints]) -> float:
    """Soft duration target (ms) derived from ``runtime_hints``.

    Deterministic control law, monotone in the right directions:
    - Base is ``TARGET_DURATION_MS``; the result is always clamped to
      ``[MIN_SOFT_TARGET_MS, MAX_SOFT_TARGET_MS]``.
    - Startup (no audio observed yet — playback buffer AND first-audio EWMA
      both absent): the target shrinks linearly from ``TARGET_DURATION_MS``
      (at ``STARTUP_LATE_ELAPSED_MS``) to ``STARTUP_EARLY_TARGET_MS`` (at
      ``2 * STARTUP_LATE_ELAPSED_MS``, clamped beyond) as the speech start
      falls later; before the late threshold, no startup adjustment.
    - Steady: playback buffer at/above ``HEALTHY_WATERMARK_MS`` may raise the
      target up to ``STEADY_TARGET_MS``.
    - Starvation (each independent, result takes the min): playback buffer
      below ``STARVATION_WATERMARK_MS``, TTS RTF EWMA at/above
      ``RTF_DEGRADED_THRESHOLD``, or first-audio EWMA at/above
      ``FIRST_AUDIO_SLOW_MS`` — each shrinks the target down to
      ``STARVATION_TARGET_MS``.
    - All pushes apply in order (downward via min, upward via max), then the
      clamp; when startup and starvation both fire, the most aggressive
      (smallest) target wins.

    Never raises: neutral/absent/non-finite hints simply keep the base
    target, so a garbage hint cannot trip the chunker's failure fallback.
    """
    if runtime_hints is None:
        return TARGET_DURATION_MS

    target = TARGET_DURATION_MS

    # Steady: healthy playback buffer allows longer chunks.
    if (
        not _hint_neutral(runtime_hints.playback_buffer_ms)
        and runtime_hints.playback_buffer_ms >= HEALTHY_WATERMARK_MS
    ):
        target = max(target, STEADY_TARGET_MS)

    # Starvation: each degraded signal independently shrinks the target.
    if (
        not _hint_neutral(runtime_hints.playback_buffer_ms)
        and runtime_hints.playback_buffer_ms < STARVATION_WATERMARK_MS
    ):
        target = min(target, STARVATION_TARGET_MS)
    if (
        not _hint_neutral(runtime_hints.tts_rtf_ewma)
        and runtime_hints.tts_rtf_ewma >= RTF_DEGRADED_THRESHOLD
    ):
        target = min(target, STARVATION_TARGET_MS)
    if (
        not _hint_neutral(runtime_hints.tts_first_audio_ewma_ms)
        and runtime_hints.tts_first_audio_ewma_ms >= FIRST_AUDIO_SLOW_MS
    ):
        target = min(target, STARVATION_TARGET_MS)

    # Startup: only while no audio has ever been observed.
    if runtime_hints.playback_buffer_ms is None and runtime_hints.tts_first_audio_ewma_ms is None:
        if (
            not _hint_neutral(runtime_hints.speech_start_elapsed_ms)
            and runtime_hints.speech_start_elapsed_ms >= STARTUP_LATE_ELAPSED_MS
        ):
            # Linear ramp from TARGET_DURATION_MS (at the late threshold) down
            # to STARTUP_EARLY_TARGET_MS at 2x the threshold, clamped beyond.
            progress = min(
                1.0,
                (runtime_hints.speech_start_elapsed_ms - STARTUP_LATE_ELAPSED_MS)
                / STARTUP_LATE_ELAPSED_MS,
            )
            target = min(
                target, TARGET_DURATION_MS - progress * (TARGET_DURATION_MS - STARTUP_EARLY_TARGET_MS)
            )

    return min(MAX_SOFT_TARGET_MS, max(MIN_SOFT_TARGET_MS, target))


def _duration_distance(estimate: float, target: float) -> float:
    """Deterministic proximity of an estimate to the soft target.

    Symmetric relative deviation so short and long overruns compare on the
    same scale; returns a cost (lower is better), always in [0, 1] for any
    finite positive inputs.
    """
    if estimate <= 0.0:
        return 1.0
    return abs(estimate - target) / max(estimate, target)


def score_boundary(
    text: str,
    candidate: BoundaryCandidate,
    estimator: SpeechDurationEstimator,
    target_chars: int,
    *,
    soft_target: Optional[float] = None,
) -> float:
    """Deterministic composite cost for ``candidate`` (lower is better).

    ``candidate.kind`` encodes the primary linguistic rank; duration
    proximity (to ``soft_target``, or ``TARGET_DURATION_MS`` when None) then
    character distance refine within the same kind. Raises
    ``AdaptiveAnalysisError`` when the estimator returns a non-finite value
    or when ``soft_target`` is set but not a finite positive number.
    """
    if soft_target is not None and (not math.isfinite(soft_target) or soft_target <= 0):
        raise AdaptiveAnalysisError(f"soft_target must be finite and > 0, got {soft_target!r}")
    head = text[: candidate.end]
    duration = _require_duration(estimator, head)
    duration_target = TARGET_DURATION_MS if soft_target is None else soft_target
    return (
        int(candidate.kind) * _KIND_WEIGHT
        + _duration_distance(duration, duration_target) * _DURATION_WEIGHT
        + abs(candidate.end - target_chars) * _CHAR_WEIGHT
    )


def _select_adaptive(
    text: str,
    candidates: list[BoundaryCandidate],
    *,
    estimator: SpeechDurationEstimator,
    target_chars: int,
    max_chars: int,
    min_chars: int,
    soft_target: float,
) -> Optional[SelectedBoundary]:
    """Adaptive selection — earliest strong, weak-commit, forced cap, None.

    Selection order (task 5.2/5.3/5.4):
    1. Earliest safe strong (paragraph/sentence, not protected,
       ``min_chars <= end <= max_chars``) — the soft target never delays or
       accelerates a strong commitment, so strong segmentation stays a pure
       function of the accumulated prefix (fragmentation-invariant).
    2. Weak-commit under downward pressure: only when ``soft_target`` is
       below ``TARGET_DURATION_MS`` and no strong committed, commit the
       EARLIEST weak candidate (clause/comma/cue/whitespace) that is not
       protected, lands in ``[min_chars, max_chars]``, and whose head
       duration fits the shrunk target. Earliest-qualifying is again a pure
       function of (prefix, hints), so fragmentation invariance holds. The
       duration check uses ``_require_duration``: a non-finite estimate
       raises ``AdaptiveAnalysisError`` (fail closed).
    3. Cap pressure (unchanged structure): no strong committed and the cap
       candidate exists — best weak by composite score against
       ``soft_target``, else the exact-cap split. ``forced=True``.
    4. Else ``None`` ("keep buffering").

    With neutral hints ``soft_target == TARGET_DURATION_MS``: rule 2 never
    fires and rule 3 scores identically to the pre-hints behavior.
    """
    cap_candidate = next((c for c in candidates if c.hard_cap), None)

    safe_strong = [
        c
        for c in candidates
        if c.kind in (CandidateKind.PARAGRAPH, CandidateKind.SENTENCE)
        and not c.protected
        and min_chars <= c.end <= max_chars
    ]
    if safe_strong:
        return SelectedBoundary(safe_strong[0], forced=False)

    weak_kinds = (
        CandidateKind.CLAUSE,
        CandidateKind.COMMA,
        CandidateKind.VIETNAMESE_CUE,
        CandidateKind.WHITESPACE,
    )
    if soft_target < TARGET_DURATION_MS and cap_candidate is None:
        for candidate in candidates:
            if candidate.hard_cap or candidate.kind not in weak_kinds or candidate.protected:
                continue
            if not (min_chars <= candidate.end <= max_chars):
                continue
            head = text[: candidate.end]
            if _require_duration(estimator, head) <= soft_target:
                return SelectedBoundary(candidate, forced=False)

    if cap_candidate is not None:
        # Buffer exceeds max_chars and no safe strong boundary exists: the
        # cap forces a decision. Prefer the best natural (weak) boundary by
        # composite score, or the exact-cap forced split when none exists.
        natural = [c for c in candidates if not c.hard_cap and min_chars <= c.end <= max_chars]
        safe = [c for c in natural if not c.protected]
        pool = safe if safe else natural
        if pool:
            best = min(
                pool,
                key=lambda c: score_boundary(text, c, estimator, target_chars, soft_target=soft_target),
            )
            return SelectedBoundary(best, forced=True)
        return SelectedBoundary(cap_candidate, forced=True)

    return None


def select_boundary(
    text: str,
    candidates: list[BoundaryCandidate],
    *,
    estimator: SpeechDurationEstimator,
    target_chars: int,
    max_chars: int,
    min_chars: int,
    runtime_hints: Optional[RuntimeHints] = None,
) -> Optional[SelectedBoundary]:
    """Select the best adaptive split for ``text`` among ``candidates``.

    Deterministic pure selection; ``text`` is the exact accumulated buffer and
    every candidate ``end`` is a slice offset into it. ``runtime_hints``
    derive the soft duration target once (``soft_target_duration_ms``) and
    that target feeds both candidate scoring and the weak-commit rule;
    neutral hints reproduce the pre-hints behavior exactly. Returns ``None``
    to keep buffering. May raise ``AdaptiveAnalysisError`` when the estimator
    is unusable (the chunker fails closed to fixed).
    """
    if not text or not candidates:
        return None
    soft_target = soft_target_duration_ms(runtime_hints)
    return _select_adaptive(
        text,
        candidates,
        estimator=estimator,
        target_chars=target_chars,
        max_chars=max_chars,
        min_chars=min_chars,
        soft_target=soft_target,
    )
