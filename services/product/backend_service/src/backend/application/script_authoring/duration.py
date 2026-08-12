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

__all__ = ["spoken_duration_ms"]

_ESTIMATOR = SpeechDurationEstimator()


def spoken_duration_ms(text: str) -> float:
    """Estimated spoken duration of ``text`` in milliseconds.

    Deterministic and pure, delegated to Change A's canonical estimator:
    identical text -> identical estimate. Returns a finite nonnegative
    float for any input (0.0 for empty text).
    """
    return _ESTIMATOR.estimate_ms(text)
