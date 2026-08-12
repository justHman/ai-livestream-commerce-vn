"""Target spoken-duration rules (task 3.9) — via Change A's canonical
estimator, never a duplicate estimator (spec "Canonical speech-duration
estimation is reused", tasks 4.2a/4.2b).

``check_segment_duration`` estimates the segment's spoken duration through
``backend.application.text_chunker.SpeechDurationEstimator`` (the stable
Change A package export) and compares against ``context.target_min_seconds``
/ ``target_max_seconds``. Identical spoken text => identical estimate =>
identical gate outcome (task 3.12 determinism).
"""

from __future__ import annotations

from ..results import RuleViolation, Severity

__all__ = ["check_segment_duration", "RULE_SPEECH_DURATION"]

RULE_SPEECH_DURATION = "SPEECH_DURATION_SEGMENT"

# Change A stable contract: import from the package root, never deep-import
# text_chunker.duration (see change_a_contract.py).
from backend.application.text_chunker import SpeechDurationEstimator  # noqa: E402


def check_segment_duration(text: str, context) -> list[RuleViolation]:
    """Flag segments whose estimated spoken duration is out of target range.

    Uses the Change A estimator on the segment text; the segment text is
    assumed to be the final spoken form (compiled), matching what runtime
    will actually speak.
    """
    estimate_ms = SpeechDurationEstimator().estimate_ms(text)
    estimate_seconds = estimate_ms / 1000.0
    violations: list[RuleViolation] = []
    if estimate_seconds < context.target_min_seconds:
        violations.append(
            RuleViolation(
                rule_id=RULE_SPEECH_DURATION,
                severity=Severity.ERROR,
                message=(
                    f"Segment is too short: estimated {estimate_seconds:.1f}s "
                    f"spoken, target minimum is {context.target_min_seconds:g}s."
                ),
            )
        )
    elif estimate_seconds > context.target_max_seconds:
        violations.append(
            RuleViolation(
                rule_id=RULE_SPEECH_DURATION,
                severity=Severity.ERROR,
                message=(
                    f"Segment is too long: estimated {estimate_seconds:.1f}s "
                    f"spoken, target maximum is {context.target_max_seconds:g}s."
                ),
            )
        )
    return violations
