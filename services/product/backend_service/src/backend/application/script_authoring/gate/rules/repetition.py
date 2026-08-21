"""Local repetition and CTA-frequency rule primitives (task 3.9).

Segment-scope rules: repeated phrases within one segment (word n-gram
overlap) and CTA frequency (call-to-action density) inside one segment.
Both are deterministic and bounded so evaluation stays linear.
"""

from __future__ import annotations

import re

from ..results import RuleViolation, Severity, TextSpan

__all__ = [
    "check_local_repetition",
    "check_cta_frequency",
    "RULE_REPETITION_LOCAL",
    "RULE_REPETITION_CTA",
]

RULE_REPETITION_LOCAL = "REPETITION_LOCAL"
RULE_REPETITION_CTA = "REPETITION_CTA"

# CTA phrases: imperative "mua ngay", "đặt ngay", "vào giỏ", "đặt hàng",
# "nhấn link", "chốt đơn", "đừng bỏ lỡ", "số lượng có hạn".
_CTA_RE = re.compile(
    r"(?i)(mua ngay|đặt ngay|đặt hàng|vào giỏ hàng|chốt đơn|"
    r"nhấn link|bấm link|đừng bỏ lỡ|số lượng có hạn|order ngay)"
)

# Word tokens for n-gram extraction (Vietnamese syllables).
_WORD_RE = re.compile(r"[\w]+", re.UNICODE)

# Repetition thresholds: 3 occurrences of a 4-gram, or 4 occurrences of a
# 3-gram, within one segment.
# Raised 4-gram 2 -> 3 and 3-gram 3 -> 4 (15.4 real-LLM E2E): a long segment
# (~360s for a K=5/1800s script) must restate the mandatory claim phrase
# verbatim, and a real LLM naturally says it twice (intro + factual
# sentence). That single restatement makes every overlapping 4-gram of the
# claim appear 2x and a 2x threshold fired 2-3 violations per restated
# claim. Common Vietnamese 3-grams ("của thiết bị", "một thiết bị") also
# recur 3x in natural prose. A phrase repeated 4+ times in one segment is
# the real within-segment defect (mirrors the cross-segment 4-gram
# threshold, also >= 3 segments).
_MAX_4GRAM_REPEAT = 2
_MAX_3GRAM_REPEAT = 3


def _ngrams(words: list[str], n: int) -> list[tuple[str, ...]]:
    return [tuple(words[i : i + n]) for i in range(len(words) - n + 1)]


def check_local_repetition(text: str, context) -> list[RuleViolation]:
    """Flag repeated 3-gram/4-gram phrases within one segment.

    ERROR when a phrase is repeated more than the threshold: spoken text
    repeats are the most audible defect in livestream scripts.
    """
    violations: list[RuleViolation] = []
    words = _WORD_RE.findall(text.lower())
    for n, threshold in ((4, _MAX_4GRAM_REPEAT), (3, _MAX_3GRAM_REPEAT)):
        counts: dict[tuple[str, ...], int] = {}
        for gram in _ngrams(words, n):
            counts[gram] = counts.get(gram, 0) + 1
        for gram, count in counts.items():
            if count > threshold:
                phrase = " ".join(gram)
                violations.append(
                    RuleViolation(
                        rule_id=RULE_REPETITION_LOCAL,
                        severity=Severity.ERROR,
                        message=(
                            f"Phrase {phrase!r} is repeated {count} times "
                            f"in this segment; vary the wording."
                        ),
                    )
                )
    return violations


def check_cta_frequency(text: str, context) -> list[RuleViolation]:
    """Flag too many CTAs in one segment.

    ERROR when CTA count exceeds ``context.max_cta_per_segment``.
    """
    violations: list[RuleViolation] = []
    matches = list(_CTA_RE.finditer(text))
    if len(matches) > context.max_cta_per_segment:
        first = matches[0]
        violations.append(
            RuleViolation(
                rule_id=RULE_REPETITION_CTA,
                severity=Severity.ERROR,
                message=(
                    f"{len(matches)} CTAs in one segment; the limit is "
                    f"{context.max_cta_per_segment}."
                ),
                text_span=TextSpan(first.start(), first.end()),
            )
        )
    return violations
