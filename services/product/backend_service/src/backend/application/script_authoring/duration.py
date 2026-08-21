"""Spoken-duration estimation for script authoring (tasks 4.2a/4.2b).

Change B MUST NOT implement a second Vietnamese speech-duration estimator.
This module is a thin delegation seam: ``spoken_duration_ms`` forwards to
the canonical Change A ``SpeechDurationEstimator`` exported from the
``backend.application.text_chunker`` package root. It never deep-imports
``text_chunker.duration`` and never duplicates the estimator's algorithm
(parity test 4.2b asserts equality for the same text).
"""

from __future__ import annotations

from backend.application.text_chunker import SpeechDurationEstimator

__all__ = ["spoken_duration_ms", "gate_duration_band"]

_ESTIMATOR = SpeechDurationEstimator()

# Defensible spoken-duration acceptance band: 50%-150% of the target
# (reviewer R9.4). The PR#53 band dropped the lower bound to 15%, letting a
# nominal 10-minute target pass at ~1.5 minutes; a 50% floor restores the
# operational meaning of the requested duration. The prompt and gate share
# this one source so they can never disagree.
_DURATION_MIN_FRACTION = 0.5
_DURATION_MAX_FRACTION = 1.5


def gate_duration_band(target_duration_s: float) -> tuple[float, float]:
    """Return the defensible spoken-duration acceptance band for a target.

    ``(0.5 * target, 1.5 * target)``. Pure and deterministic; the generation
    prompt states exactly this band so prompt and gate stay consistent.
    """
    return (_DURATION_MIN_FRACTION * target_duration_s, _DURATION_MAX_FRACTION * target_duration_s)


def spoken_duration_ms(text: str) -> float:
    """Estimated spoken duration of ``text`` in milliseconds.

    Deterministic and pure, delegated to Change A's canonical estimator:
    identical text -> identical estimate. Returns a finite nonnegative
    float for any input (0.0 for empty text).
    """
    return _ESTIMATOR.estimate_ms(text)
