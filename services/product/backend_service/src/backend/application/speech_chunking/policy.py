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

``runtime_hints`` is accepted but deliberately unused in this cluster (task
5.x consumes it); the soft duration target stays a module constant.
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
    "TARGET_DURATION_MS",
]


# Soft duration target for adaptive_vi. Module-level and fixed (not a
# constructor knob) because cluster 3B must not add config/tuning surface:
# startup/steady/starvation adaptation lands in task 5.x. Neutral runtime
# hints keep this constant for now.
TARGET_DURATION_MS = 2200.0

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
) -> float:
    """Deterministic composite cost for ``candidate`` (lower is better).

    ``candidate.kind`` encodes the primary linguistic rank; duration
    proximity then character distance refine within the same kind. Raises
    ``AdaptiveAnalysisError`` when the estimator returns a non-finite value.
    """
    head = text[: candidate.end]
    duration = _require_duration(estimator, head)
    return (
        int(candidate.kind) * _KIND_WEIGHT
        + _duration_distance(duration, TARGET_DURATION_MS) * _DURATION_WEIGHT
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
) -> Optional[SelectedBoundary]:
    """Adaptive selection — earliest strong, else forced cap split, else None.

    ``None`` means "keep buffering" (no strong boundary and no cap forcing a
    decision). The earliest-strong rule keeps segmentation invariant to input
    fragmentation: it is a function of the accumulated prefix alone.
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

    if cap_candidate is not None:
        # Buffer exceeds max_chars and no safe strong boundary exists: the
        # cap forces a decision. Prefer the best natural (weak) boundary by
        # composite score, or the exact-cap forced split when none exists.
        natural = [c for c in candidates if not c.hard_cap and min_chars <= c.end <= max_chars]
        safe = [c for c in natural if not c.protected]
        pool = safe if safe else natural
        if pool:
            best = min(pool, key=lambda c: score_boundary(text, c, estimator, target_chars))
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
    every candidate ``end`` is a slice offset into it. Returns ``None`` to
    keep buffering. May raise ``AdaptiveAnalysisError`` when the estimator is
    unusable (the chunker fails closed to fixed).
    """
    del runtime_hints  # neutral hints in this cluster; task 5.x consumes them

    if not text or not candidates:
        return None
    return _select_adaptive(
        text,
        candidates,
        estimator=estimator,
        target_chars=target_chars,
        max_chars=max_chars,
        min_chars=min_chars,
    )
