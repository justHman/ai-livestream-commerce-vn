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
    # The message is also the repair instruction a segment repair receives, so
    # it must say HOW to fix the duration safely: the Change A estimate is
    # multiplier-based (currency/number/acronym tokens inflate the syllable
    # count), so removing/verbalizing the compact price token collapses the
    # estimate — the 15.4 repair used to do exactly that (1450 chars measured
    # ~81.9s vs a ~139.8s shorter sibling) and never landed in band.
    if estimate_seconds < context.target_min_seconds:
        violations.append(
            RuleViolation(
                rule_id=RULE_SPEECH_DURATION,
                severity=Severity.ERROR,
                message=(
                    f"Segment is too short: estimated {estimate_seconds:.1f}s "
                    f"spoken, target minimum is {context.target_min_seconds:g}s. "
                    "KEEP the compact price/number tokens (e.g. '2.990.000đ', "
                    "'12 tháng') — they inflate the spoken-duration estimate; "
                    "do NOT remove or verbalize them to save space. ADD new "
                    "sentences with a different allowed claim or expand an "
                    "existing point instead."
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
                    f"spoken, target maximum is {context.target_max_seconds:g}s. "
                    "TRIM redundant sentences and filler, but KEEP the compact "
                    "price/number tokens — removing them collapses the estimate "
                    "and overshoots the other direction."
                ),
            )
        )
    return violations
