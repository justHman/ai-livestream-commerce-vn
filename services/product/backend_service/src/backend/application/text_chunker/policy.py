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
3. **char-bias tie-break** — head-length distance to a fixed neutral
   ``char_bias_chars`` reference (``AdaptiveViPolicyConfig``) breaks ties
   when duration is equal. This is an internal scoring constant, NOT a
   sizing target: the fixed policy's ``target_chars`` never leaks into
   adaptive scoring.

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
from typing import TYPE_CHECKING, Optional, Protocol

from .types import ChunkPolicy, ChunkDecisionReason, FixedChunkPolicyConfig, RuntimeHints, TextChunk

from .boundaries import BoundaryCandidate, CandidateKind, extract_candidates
from .duration import SpeechDurationEstimator

if TYPE_CHECKING:  # pragma: no cover — type-only circular import
    from .chunker import TextChunker

__all__ = [
    "AdaptiveAnalysisError",
    "SelectedBoundary",
    "AdaptiveViPolicyConfig",
    "ChunkingPolicy",
    "FixedPolicyStrategy",
    "AdaptiveViPolicyStrategy",
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
# spoken length is the primary soft size signal; character distance to the
# adaptive char-bias reference is the tie-break/fallback.
_KIND_WEIGHT = 1_000_000.0
_DURATION_WEIGHT = 1000.0
_CHAR_WEIGHT = 1.0


@dataclass(frozen=True)
class AdaptiveViPolicyConfig:
    """Adaptive_vi policy configuration: speech-duration targets, linguistic
    scoring weights, and hard safety constraints.

    The duration-target fields feed ``soft_target_duration_ms`` directly
    (task 8.9): every control-law constant is read from this config when one
    is passed, so the tuned targets are not dead knobs. Defaults equal the
    cand-05 calibrated constants (target 2200ms, startup early 1200ms,
    starvation 1200ms); the module-level constants remain the no-config
    backward-compatible fallback.

    This config deliberately carries NO ``target_chars``: the fixed policy's
    character sizing semantics never leak into adaptive scoring. The only
    character-size inputs are the hard safety constraints (``min_chars`` and
    ``max_chars``) plus a fixed neutral ``char_bias_chars`` used to break
    duration ties (the legacy fixed target_chars value, kept as an internal
    bias reference so identical inputs score identically to the pre-split
    implementation).
    """

    min_chars: int = 12
    max_chars: int = 80
    char_bias_chars: int = 40
    target_duration_ms: float = TARGET_DURATION_MS
    min_soft_target_ms: float = MIN_SOFT_TARGET_MS
    max_soft_target_ms: float = MAX_SOFT_TARGET_MS
    steady_target_ms: float = STEADY_TARGET_MS
    startup_late_elapsed_ms: float = STARTUP_LATE_ELAPSED_MS
    startup_early_target_ms: float = 1200.0  # cand-05 calibrated (was 1500)
    starvation_watermark_ms: float = STARVATION_WATERMARK_MS
    starvation_target_ms: float = 1200.0  # cand-05 calibrated (was 1400)
    healthy_watermark_ms: float = HEALTHY_WATERMARK_MS
    rtf_degraded_threshold: float = RTF_DEGRADED_THRESHOLD
    first_audio_slow_ms: float = FIRST_AUDIO_SLOW_MS

    def __post_init__(self) -> None:
        if self.min_chars <= 0:
            raise ValueError(f"min_chars must be > 0, got {self.min_chars}")
        if self.max_chars <= 0:
            raise ValueError(f"max_chars must be > 0, got {self.max_chars}")
        if self.char_bias_chars <= 0:
            raise ValueError(f"char_bias_chars must be > 0, got {self.char_bias_chars}")
        if not math.isfinite(self.target_duration_ms) or self.target_duration_ms <= 0:
            raise ValueError(
                f"target_duration_ms must be finite and > 0, got {self.target_duration_ms}"
            )


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


# Phrase boundary characters for the fixed policy's drain loop
# (accepted fixed punctuation: . , ! ? ; : newline).
PUNCTUATION_BOUNDARIES = frozenset({".", ",", "!", "?", ";", ":", "\n"})

# Adaptive analysis horizon: only the first ``max_chars + ADAPTIVE_HORIZON``
# characters of the buffer are scanned per drain iteration. Every committed
# boundary is at or before ``max_chars``, and protected-span detection for
# candidates at/before the cap only needs spans covering that prefix, so the
# bounded horizon preserves protection flags while keeping a single huge
# delta linear in input size instead of O(n^2) rescanning.
# ponytail: fixed horizon; if a single protected token longer than
# ``max_chars + 256`` must be protected across a forced split, widen it.
ADAPTIVE_HORIZON = 256


class ChunkingPolicy(Protocol):
    """Segmentation strategy protocol (design Decision: policy strategy).

    One TextChunker hosts the shared buffer/emit/telemetry state; the
    injected strategy owns ONLY the segmentation decision (which boundaries
    to commit during drain, and how to finalize). There is no monolithic
    mode-switch in the chunker — the strategy is chosen at construction.
    """

    policy_id: ChunkPolicy

    def drain(
        self, chunker: "TextChunker", runtime_hints: Optional[RuntimeHints]
    ) -> list[TextChunk]: ...

    def finalize(
        self, chunker: "TextChunker", runtime_hints: Optional[RuntimeHints]
    ) -> list[TextChunk]: ...


class FixedPolicyStrategy:
    """Fixed character-threshold segmentation strategy.

    The deterministic baseline and explicit runtime rollback: first
    qualifying punctuation with a ``[min, max]`` prefix, then last
    whitespace at/before the cap, then the exact cap. At finalize,
    ``target_chars`` fixes the whitespace fallback boundary for an
    over-target pending buffer. Behavior is byte-for-byte the historical
    fixed policy.
    """

    policy_id = ChunkPolicy.FIXED

    def __init__(self, config: FixedChunkPolicyConfig) -> None:
        self.config = config

    def drain(
        self, chunker: "TextChunker", runtime_hints: Optional[RuntimeHints]
    ) -> list[TextChunk]:
        """Drain completed punctuation phrases and hard-cap splits."""
        del runtime_hints  # fixed policy ignores runtime hints
        chunks: list[TextChunk] = []
        while chunker.buffer_len > 0:
            start = chunker.buffered_text
            boundary = self._next_boundary(start, chunker.buffer_len)
            if boundary is None:
                break
            end, base_reason = boundary
            head = start[:end]
            tail = start[end:]
            reason = ChunkDecisionReason.FIXED_FALLBACK if chunker.fallback_active else base_reason
            chunks.append(chunker._emit(head, is_final=False, reason=reason))
            chunker._commit_tail(tail)
        return chunks

    def _next_boundary(
        self, text: str, buffer_len: int
    ) -> Optional[tuple[int, ChunkDecisionReason]]:
        """First qualifying split in ``text``: punctuation or the hard cap.

        A punctuation boundary qualifies only when its prefix lands in
        ``[min_chars, max_chars]``; without one, the hard cap is reached at
        exactly ``max_chars`` (progress is guaranteed: 1 <= end <= max_chars).
        Returns None while the buffer stays pending.
        """
        min_chars = self.config.min_chars
        max_chars = self.config.max_chars
        if buffer_len >= min_chars:
            for index, char in enumerate(text):
                if char in PUNCTUATION_BOUNDARIES and min_chars <= index + 1 <= max_chars:
                    return index + 1, ChunkDecisionReason.PUNCTUATION
        if buffer_len >= max_chars:
            # Safe fixed-core fallback: no qualifying punctuation was found,
            # so prefer the LAST whitespace at or before the cap — the head
            # then ends on a word boundary and keeps that whitespace, so
            # exact slicing/order stays trivial. Only when the split position
            # is >= min_chars; otherwise cut exactly at the cap. HARD_MAX is
            # stamped either way: the cap forced the decision.
            for split_at in range(max_chars, 0, -1):
                if text[split_at - 1].isspace() and split_at >= min_chars:
                    return split_at, ChunkDecisionReason.HARD_MAX
            return max_chars, ChunkDecisionReason.HARD_MAX
        return None

    def finalize(
        self, chunker: "TextChunker", runtime_hints: Optional[RuntimeHints]
    ) -> list[TextChunk]:
        """Flush the remaining buffer as the final chunk(s) of the utterance.

        The pending buffer is necessarily < max_chars (the drain loop splits
        at len >= max_chars), so ``target_chars`` gets its real role here:
        when the buffer exceeds ``target_chars``, split ONCE at the
        whitespace nearest ``target_chars`` within ``[min_chars, len-1]``
        (ties prefer the lower index). The head is emitted as a non-final
        FIXED_FALLBACK chunk — the target is only a fallback, not a deadline
        — and the remainder follows as the exact final FINALIZE chunk. With
        no qualifying whitespace, or when ``target_chars`` >= len, the whole
        buffer is one final chunk.
        """
        del runtime_hints
        buffer_len = chunker.buffer_len
        if buffer_len == 0:
            return []
        text = chunker.buffered_text
        min_chars = self.config.min_chars
        target_chars = self.config.target_chars
        if buffer_len > target_chars:
            # Target fallback only at finalize of a pending buffer below the
            # cap: no punctuation drained and no hard-max split applies, so
            # the closest whitespace around target_chars fixes the boundary.
            best: Optional[int] = None
            for index in range(min_chars, buffer_len):
                if not text[index - 1].isspace():
                    continue
                if best is None or abs(index - target_chars) < abs(best - target_chars):
                    best = index
            if best is not None:
                head = chunker._emit(
                    text[:best],
                    is_final=False,
                    reason=ChunkDecisionReason.FIXED_FALLBACK,
                )
                chunker._commit_tail(text[best:])
                return [head] + chunker._flush_buffer(
                    is_final=True, reason=ChunkDecisionReason.FINALIZE
                )
        return chunker._flush_buffer(is_final=True, reason=ChunkDecisionReason.FINALIZE)


class AdaptiveViPolicyStrategy:
    """Deterministic Vietnamese boundary-selection strategy.

    Commits the earliest strong (paragraph/sentence), non-protected boundary
    at/after ``min_chars`` during the drain loop; weak boundaries commit only
    under downward hint pressure or forced-cap scoring. Runtime hints adjust
    the soft duration target; the hard invariants (strong commitment, cap,
    min/max chars, protection) never change. Any analysis failure fails
    closed to the fixed strategy for the rest of the utterance, stamping
    ``fixed_fallback`` — text is never dropped or reordered.
    """

    policy_id = ChunkPolicy.ADAPTIVE_VI

    def __init__(self, config: AdaptiveViPolicyConfig) -> None:
        self.config = config

    def drain(
        self, chunker: "TextChunker", runtime_hints: Optional[RuntimeHints]
    ) -> list[TextChunk]:
        """Drain under the adaptive policy.

        Per iteration: run the deterministic scorer over the accumulated
        buffer and commit the selected boundary. A selected boundary is
        either the earliest strong (paragraph/sentence), non-protected
        boundary at/after ``min_chars``, or a forced hard-cap split when the
        buffer exceeds ``max_chars`` and no safe strong boundary exists.
        The hard-cap split prefers the best natural (weak) boundary by
        composite score — linguistic quality, then estimated-duration
        proximity, then the adaptive char-bias reference — and falls back to
        the exact-cap cut only when no natural boundary exists.

        Any analysis failure (exception or non-finite estimate) fails closed
        to fixed segmentation: the chunker switches to the fixed strategy
        for the rest of the utterance, stamps ``fixed_fallback``, and keeps
        every already-emitted chunk — text is never dropped or reordered.
        """
        chunks: list[TextChunk] = []
        config = self.config
        max_chars = config.max_chars
        while chunker.buffer_len > 0:
            text = chunker.buffered_text
            # Bounded analysis horizon: candidates at/before ``max_chars`` only
            # need protected spans covering that prefix, and the earliest strong
            # boundary / forced-cap split are pure functions of the prefix — so
            # scanning past ``max_chars + ADAPTIVE_HORIZON`` adds nothing but
            # O(n^2) rescanning for a huge single delta.
            prefix = text[: max_chars + ADAPTIVE_HORIZON]
            try:
                candidates = extract_candidates(prefix, max_chars)
                selected = select_boundary(
                    text,
                    candidates,
                    estimator=chunker._estimator,
                    config=config,
                    runtime_hints=runtime_hints,
                )
            except Exception as exc:  # noqa: BLE001 — fail closed, never crash
                chunker._enter_fixed_fallback(f"adaptive_analysis_error: {type(exc).__name__}")
                return chunks + chunker._fixed_strategy.drain(chunker, runtime_hints)
            if selected is None:
                break
            end = selected.candidate.end
            # Hold a strong boundary at the buffer edge that is a "." directly
            # after a digit run: the run may continue into a decimal/grouped
            # number once more text arrives, which would make the dot protected
            # (interior). Committing it early would split a future "199.000đ" —
            # a fragmentation-dependent boundary. The same buffer content
            # always makes the same hold decision, so segmentation stays
            # invariant to how the input was fed.
            if (
                not selected.forced
                and end == len(text)
                and text[end - 1] == "."
                and end >= 2
                and text[end - 2].isdigit()
            ):
                break
            head = text[:end]
            tail = text[end:]
            forced = selected.forced
            reason = (
                ChunkDecisionReason.HARD_MAX
                if forced
                else chunk_decision_reason(selected.candidate.kind)
            )
            # A forced cap split whose candidate cuts inside a protected span
            # is a protected-span fallback (nothing safe existed); the flag is
            # telemetry-only, the chunk itself is unchanged.
            protected_fallback = forced and selected.candidate.protected
            chunks.append(
                chunker._emit(
                    head,
                    is_final=False,
                    reason=reason,
                    protected_fallback=protected_fallback,
                )
            )
            chunker._commit_tail(tail)
        return chunks

    def finalize(
        self, chunker: "TextChunker", runtime_hints: Optional[RuntimeHints]
    ) -> list[TextChunk]:
        """Adaptive finalize: the last coherent phrase is one final chunk.

        The drain loop keeps the pending buffer <= ``max_chars`` (forced-cap
        splits), so this path normally emits the whole buffer as the final
        chunk. A defensive drain first preserves the hard cap if a caller
        constructed state with an oversized buffer.
        """
        if chunker.buffer_len == 0:
            return []
        if chunker.buffer_len > self.config.max_chars:
            chunks = self.drain(chunker, runtime_hints)
            if chunker.buffer_len == 0:
                return chunks
        return chunker._flush_buffer(is_final=True, reason=ChunkDecisionReason.FINALIZE)


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


def soft_target_duration_ms(
    runtime_hints: Optional[RuntimeHints],
    config: Optional[AdaptiveViPolicyConfig] = None,
) -> float:
    """Soft duration target (ms) derived from ``runtime_hints``.

    Deterministic control law, monotone in the right directions. When
    ``config`` is given, every law constant is read from the config fields
    (``target_duration_ms``, ``min_soft_target_ms``, ``max_soft_target_ms``,
    ``steady_target_ms``, ``startup_late_elapsed_ms``,
    ``startup_early_target_ms``, ``starvation_watermark_ms``,
    ``starvation_target_ms``, ``healthy_watermark_ms``,
    ``rtf_degraded_threshold``, ``first_audio_slow_ms``); without a config the
    module-level constants are used (backward-compatible default).
    - Base is the target; the result is always clamped to
      ``[MIN_SOFT_TARGET_MS, MAX_SOFT_TARGET_MS]``.
    - Startup (no audio observed yet — playback buffer AND first-audio EWMA
      both absent): the target shrinks linearly from the base target (at
      ``STARTUP_LATE_ELAPSED_MS``) to ``STARTUP_EARLY_TARGET_MS`` (at
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
    base_target = TARGET_DURATION_MS if config is None else config.target_duration_ms
    min_target = MIN_SOFT_TARGET_MS if config is None else config.min_soft_target_ms
    max_target = MAX_SOFT_TARGET_MS if config is None else config.max_soft_target_ms
    steady_target = STEADY_TARGET_MS if config is None else config.steady_target_ms
    late_elapsed = STARTUP_LATE_ELAPSED_MS if config is None else config.startup_late_elapsed_ms
    early_target = STARTUP_EARLY_TARGET_MS if config is None else config.startup_early_target_ms
    starvation_watermark = (
        STARVATION_WATERMARK_MS if config is None else config.starvation_watermark_ms
    )
    starvation_target = STARVATION_TARGET_MS if config is None else config.starvation_target_ms
    healthy_watermark = HEALTHY_WATERMARK_MS if config is None else config.healthy_watermark_ms
    rtf_degraded = RTF_DEGRADED_THRESHOLD if config is None else config.rtf_degraded_threshold
    first_audio_slow = FIRST_AUDIO_SLOW_MS if config is None else config.first_audio_slow_ms

    if runtime_hints is None:
        return base_target

    target = base_target

    # Steady: healthy playback buffer allows longer chunks.
    if (
        not _hint_neutral(runtime_hints.playback_buffer_ms)
        and runtime_hints.playback_buffer_ms >= healthy_watermark
    ):
        target = max(target, steady_target)

    # Starvation: each degraded signal independently shrinks the target.
    if (
        not _hint_neutral(runtime_hints.playback_buffer_ms)
        and runtime_hints.playback_buffer_ms < starvation_watermark
    ):
        target = min(target, starvation_target)
    if (
        not _hint_neutral(runtime_hints.tts_rtf_ewma)
        and runtime_hints.tts_rtf_ewma >= rtf_degraded
    ):
        target = min(target, starvation_target)
    if (
        not _hint_neutral(runtime_hints.tts_first_audio_ewma_ms)
        and runtime_hints.tts_first_audio_ewma_ms >= first_audio_slow
    ):
        target = min(target, starvation_target)

    # Startup: only while no audio has ever been observed.
    if runtime_hints.playback_buffer_ms is None and runtime_hints.tts_first_audio_ewma_ms is None:
        if (
            not _hint_neutral(runtime_hints.speech_start_elapsed_ms)
            and runtime_hints.speech_start_elapsed_ms >= late_elapsed
        ):
            # Linear ramp from the base target (at the late threshold) down
            # to STARTUP_EARLY_TARGET_MS at 2x the threshold, clamped beyond.
            progress = min(
                1.0,
                (runtime_hints.speech_start_elapsed_ms - late_elapsed) / late_elapsed,
            )
            target = min(
                target,
                base_target - progress * (base_target - early_target),
            )

    return min(max_target, max(min_target, target))


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
    config: AdaptiveViPolicyConfig,
    *,
    soft_target: Optional[float] = None,
) -> float:
    """Deterministic composite cost for ``candidate`` (lower is better).

    ``candidate.kind`` encodes the primary linguistic rank; duration
    proximity (to ``soft_target``, or ``config.target_duration_ms`` when
    None) then character distance to ``config.char_bias_chars`` refine
    within the same kind. Raises ``AdaptiveAnalysisError`` when the
    estimator returns a non-finite value or when ``soft_target`` is set but
    not a finite positive number.
    """
    if soft_target is not None and (not math.isfinite(soft_target) or soft_target <= 0):
        raise AdaptiveAnalysisError(f"soft_target must be finite and > 0, got {soft_target!r}")
    head = text[: candidate.end]
    duration = _require_duration(estimator, head)
    duration_target = config.target_duration_ms if soft_target is None else soft_target
    return (
        int(candidate.kind) * _KIND_WEIGHT
        + _duration_distance(duration, duration_target) * _DURATION_WEIGHT
        + abs(candidate.end - config.char_bias_chars) * _CHAR_WEIGHT
    )


def _select_adaptive(
    text: str,
    candidates: list[BoundaryCandidate],
    *,
    estimator: SpeechDurationEstimator,
    config: AdaptiveViPolicyConfig,
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
       function of (prefix, hints), so fragmentation invariance holds —
       including when the buffer already exceeds the cap (a one-feed
       delivery and a fragmented delivery of the same text under the same
       hints then commit the same boundary; see the cap rule below). The
       duration check uses ``_require_duration``: a non-finite estimate
       raises ``AdaptiveAnalysisError`` (fail closed).
    3. Cap pressure: no strong committed and the buffer exceeds the cap —
       when the hints also shrink the target, the same earliest-qualifying
       weak rule fires first (the earliest boundary that fits the shrunk
       target is a pure function of (prefix, hints), exactly like the
       no-cap case, so single-feed and fragmented deliveries of the same
       text under the same hints commit the same boundary — fragmentation
       invariance); otherwise the best weak by composite score against
       ``soft_target``, else the exact-cap split. ``forced=True``.
    4. Else ``None`` ("keep buffering").

    With neutral hints ``soft_target == TARGET_DURATION_MS``: rule 2 never
    fires and rule 3 scores identically to the pre-hints behavior.
    """
    min_chars = config.min_chars
    max_chars = config.max_chars
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
    if soft_target < TARGET_DURATION_MS:
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
                key=lambda c: score_boundary(text, c, estimator, config, soft_target=soft_target),
            )
            return SelectedBoundary(best, forced=True)
        return SelectedBoundary(cap_candidate, forced=True)

    return None


def select_boundary(
    text: str,
    candidates: list[BoundaryCandidate],
    *,
    estimator: SpeechDurationEstimator,
    config: AdaptiveViPolicyConfig,
    runtime_hints: Optional[RuntimeHints] = None,
) -> Optional[SelectedBoundary]:
    """Select the best adaptive split for ``text`` among ``candidates``.

    Deterministic pure selection; ``text`` is the exact accumulated buffer and
    every candidate ``end`` is a slice offset into it. ``config`` supplies
    the hard character constraints (``min_chars``/``max_chars``), the char
    tie-break reference, and the duration-target constants; ``runtime_hints``
    derive the soft duration target once (``soft_target_duration_ms``) and
    that target feeds both candidate scoring and the weak-commit rule;
    neutral hints reproduce the pre-hints behavior exactly. Returns ``None``
    to keep buffering. May raise ``AdaptiveAnalysisError`` when the estimator
    is unusable (the chunker fails closed to fixed).
    """
    if not text or not candidates:
        return None
    soft_target = soft_target_duration_ms(runtime_hints, config)
    return _select_adaptive(
        text,
        candidates,
        estimator=estimator,
        config=config,
        soft_target=soft_target,
    )
