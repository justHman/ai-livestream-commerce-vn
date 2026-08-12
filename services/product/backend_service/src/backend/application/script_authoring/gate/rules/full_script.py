"""Full Script Gate rules (task 3.10): cross-segment checks.

``check(segments, context)`` receives the exact ordered list of segment
texts (the selected segment versions) and attributes violations to
``segment_index`` where applicable:

- cross-segment repetition (same phrase in 2+ segments),
- contradictory claims (a claim asserted in one segment and negated in
  another),
- required fact/topic coverage (``context.required_topics``),
- CTA pacing (too many CTAs across the whole script),
- tone/persona consistency signals (shouting, exclamation density),
- transition policy (ORDER_AGNOSTIC scripts must not hard-code another
  product dependency),
- overall spoken duration (sum of segment estimates via Change A).
"""

from __future__ import annotations

import re

from ..results import RuleViolation, Severity

__all__ = [
    "check_cross_segment_repetition",
    "check_contradictory_claims",
    "check_required_coverage",
    "check_cta_pacing",
    "check_tone_consistency",
    "check_transition_policy",
    "check_total_duration",
    "RULE_REPETITION_CROSS",
    "RULE_CLAIM_CONTRADICTION",
    "RULE_COVERAGE_REQUIRED",
    "RULE_CTA_PACING",
    "RULE_TONE_CONSISTENCY",
    "RULE_TRANSITION_ORDER",
    "RULE_SPEECH_DURATION_TOTAL",
]

RULE_REPETITION_CROSS = "REPETITION_CROSS"
RULE_CLAIM_CONTRADICTION = "CLAIM_CONTRADICTION"
RULE_COVERAGE_REQUIRED = "COVERAGE_REQUIRED"
RULE_CTA_PACING = "CTA_PACING"
RULE_TONE_CONSISTENCY = "TONE_CONSISTENCY"
RULE_TRANSITION_ORDER = "TRANSITION_ORDER"
RULE_SPEECH_DURATION_TOTAL = "SPEECH_DURATION_TOTAL"

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)
_CTA_RE = re.compile(
    r"(?i)(mua ngay|đặt ngay|đặt hàng|vào giỏ hàng|chốt đơn|"
    r"nhấn link|bấm link|đừng bỏ lỡ|số lượng có hạn|order ngay)"
)
_SHOUT_RE = re.compile(
    r"[A-ZÀÁẢÃẠĂẰẮẲẴẶÂẦẤẨẪẬĐÈÉẺẼẸÊỀẾỂỄỆÌÍỈĨỊÒÓỎÕỌÔỒỐỔỖỘ"
    r"ƠỜỚỞỠỢÙÚỦŨỤƯỪỨỬỮỰỲÝỶỸỴ]{3,}"
)
_NEGATION_RE = re.compile(r"(?i)(không|chẳng|chả|đừng|vô|làm sao|không hề|chưa bao giờ)")

# Cross-segment repetition threshold: a 4-gram appearing in >= 2 segments.
_CROSS_4GRAM_MIN_SEGMENTS = 2

# Contradiction pairs: claim keyword and its negation keyword.
_CONTRADICTIONS = (
    ("an toàn", "độc hại"),
    ("chính hãng", "hàng giả"),
    ("mới 100%", "đã qua sử dụng"),
    ("bảo hành", "không bảo hành"),
)


def _word_ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    words = _WORD_RE.findall(text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def check_cross_segment_repetition(segments: list[str], context) -> list[RuleViolation]:
    """Flag phrases repeated across segments.

    ERROR: a 4-gram present in >= 2 different segments is a script-level
    repetition defect (the fixed-K plan must distribute content).
    """
    violations: list[RuleViolation] = []
    gram_segments: dict[tuple[str, ...], list[int]] = {}
    for index, segment in enumerate(segments):
        for gram in _word_ngrams(segment, 4):
            gram_segments.setdefault(gram, []).append(index)
    for gram, indexes in gram_segments.items():
        unique = sorted(set(indexes))
        if len(unique) >= _CROSS_4GRAM_MIN_SEGMENTS:
            phrase = " ".join(gram)
            violations.append(
                RuleViolation(
                    rule_id=RULE_REPETITION_CROSS,
                    severity=Severity.ERROR,
                    message=(
                        f"Phrase {phrase!r} repeats across segments "
                        f"{', '.join(str(i + 1) for i in unique)}; distribute content."
                    ),
                    segment_index=unique[0],
                )
            )
    return violations


def check_contradictory_claims(segments: list[str], context) -> list[RuleViolation]:
    """Flag a claim asserted in one segment and negated in another.

    ERROR: contradictory claims are the worst factual defect in a script.
    """
    violations: list[RuleViolation] = []
    for claim, negation in _CONTRADICTIONS:
        claim_indexes = [i for i, seg in enumerate(segments) if claim in seg.lower()]
        negated_indexes = [i for i, seg in enumerate(segments) if negation in seg.lower()]
        if claim_indexes and negated_indexes:
            violations.append(
                RuleViolation(
                    rule_id=RULE_CLAIM_CONTRADICTION,
                    severity=Severity.ERROR,
                    message=(
                        f"Claim {claim!r} is asserted in segment "
                        f"{claim_indexes[0] + 1} and contradicted in "
                        f"segment {negated_indexes[0] + 1}."
                    ),
                    segment_index=negated_indexes[0],
                )
            )
    return violations


def check_required_coverage(segments: list[str], context) -> list[RuleViolation]:
    """Require configured topics/facts to be covered somewhere in the script.

    ERROR: a required topic absent from every segment is a coverage gap.
    """
    violations: list[RuleViolation] = []
    combined = " ".join(segments).lower()
    for topic in context.required_topics:
        if topic.lower() not in combined:
            violations.append(
                RuleViolation(
                    rule_id=RULE_COVERAGE_REQUIRED,
                    severity=Severity.ERROR,
                    message=f"Required topic {topic!r} is not covered in the script.",
                )
            )
    return violations


def check_cta_pacing(segments: list[str], context) -> list[RuleViolation]:
    """Flag too many CTAs across the whole script.

    ERROR when the total CTA count exceeds ``context.max_cta_per_segment``
    per segment on average (script-level pacing guard).
    """
    violations: list[RuleViolation] = []
    total = sum(len(_CTA_RE.findall(seg)) for seg in segments)
    allowed = max(1, context.max_cta_per_segment * len(segments))
    if total > allowed:
        violations.append(
            RuleViolation(
                rule_id=RULE_CTA_PACING,
                severity=Severity.ERROR,
                message=(
                    f"{total} CTAs across the script; the pacing limit is "
                    f"{allowed} ({context.max_cta_per_segment} per segment)."
                ),
            )
        )
    return violations


def check_tone_consistency(segments: list[str], context) -> list[RuleViolation]:
    """Flag tone/persona consistency signals.

    WARNING: ALL-CAPS shouting or excessive exclamation runs are tone
    signals that drift from a calm selling persona.
    """
    violations: list[RuleViolation] = []
    for index, segment in enumerate(segments):
        shouts = _SHOUT_RE.findall(segment)
        if len(shouts) >= 3:
            violations.append(
                RuleViolation(
                    rule_id=RULE_TONE_CONSISTENCY,
                    severity=Severity.WARNING,
                    message=(
                        f"Segment {index + 1} uses {len(shouts)} ALL-CAPS "
                        f"tokens; keep a calm selling tone."
                    ),
                    segment_index=index,
                )
            )
        if segment.count("!") >= 3:
            violations.append(
                RuleViolation(
                    rule_id=RULE_TONE_CONSISTENCY,
                    severity=Severity.WARNING,
                    message=(
                        f"Segment {index + 1} uses {segment.count('!')} "
                        f"exclamation marks; keep a calm selling tone."
                    ),
                    segment_index=index,
                )
            )
    return violations


def check_transition_policy(segments: list[str], context) -> list[RuleViolation]:
    """Enforce the transition policy.

    ORDER_AGNOSTIC: the script must not hard-code a previous/next product
    dependency that prevents runtime reordering (ERROR when another
    product's name appears). ORDER_AWARE: adjacent product mention is fine.
    """
    violations: list[RuleViolation] = []
    if context.transition_policy == "ORDER_AGNOSTIC":
        combined = " ".join(segments).lower()
        for name in context.other_product_names:
            if name.lower() in combined:
                violations.append(
                    RuleViolation(
                        rule_id=RULE_TRANSITION_ORDER,
                        severity=Severity.ERROR,
                        message=(
                            f"ORDER_AGNOSTIC script mentions another product "
                            f"{name!r}; runtime may reorder products."
                        ),
                    )
                )
    return violations


def check_total_duration(segments: list[str], context) -> list[RuleViolation]:
    """Check the compiled script's total spoken duration.

    Uses the Change A estimator per segment and sums the estimates (task
    4.2a/4.2b parity: same estimator as segment scope, no duplicate
    algorithm).
    """
    from backend.application.text_chunker import SpeechDurationEstimator

    estimator = SpeechDurationEstimator()
    total_seconds = sum(estimator.estimate_ms(seg) for seg in segments) / 1000.0
    violations: list[RuleViolation] = []
    if total_seconds < context.total_min_seconds:
        violations.append(
            RuleViolation(
                rule_id=RULE_SPEECH_DURATION_TOTAL,
                severity=Severity.ERROR,
                message=(
                    f"Script is too short: estimated {total_seconds:.1f}s "
                    f"total, target minimum is {context.total_min_seconds:g}s."
                ),
            )
        )
    elif total_seconds > context.total_max_seconds:
        violations.append(
            RuleViolation(
                rule_id=RULE_SPEECH_DURATION_TOTAL,
                severity=Severity.ERROR,
                message=(
                    f"Script is too long: estimated {total_seconds:.1f}s "
                    f"total, target maximum is {context.total_max_seconds:g}s."
                ),
            )
        )
    return violations
