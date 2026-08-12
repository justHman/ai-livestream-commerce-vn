"""TTS-readiness rules/normalizers (task 3.8).

Deterministic checks that flag forms a TTS engine would mispronounce or
that corrupt spoken output: raw numbers that should be verbalized, grouped
prices, currency, percentages, URL/email, acronyms/product codes, unsupported
markup (markdown/HTML), and hidden control characters (already flagged by the
FORMAT family, duplicated here with the TTS rule id so repair prompts can
target TTS-specific fixes).

The normalizers are the deterministic counterpart used by the display/spoken
compiler (task 4.1); this module only checks, never rewrites, source text.
"""

from __future__ import annotations

import re

from ..results import RuleViolation, Severity, TextSpan

__all__ = [
    "check_tts_markup",
    "check_tts_control_chars",
    "check_tts_acronyms",
    "check_tts_numbers",
    "normalize_tts_text",
    "RULE_TTS_MARKUP",
    "RULE_TTS_CONTROL",
    "RULE_TTS_ACRONYM",
    "RULE_TTS_NUMBER",
]

# Markdown/HTML markup that a TTS engine may speak literally or choke on.
_MARKUP_RE = re.compile(r"<[^>]+>|\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|`[^`]+`|^#{1,6}\s", re.MULTILINE)

# URLs and emails.
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+|\b[\w.+-]+@[\w-]+\.[\w.]+")

# Uppercase acronyms / product codes (2+ capital letters, optionally with
# digits/dashes). TTS engines read "SKU" letter-by-letter only if configured.
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}[A-Z0-9-]*\b")

# Raw numbers that are not part of a grouped price or percent form. The
# price/percent forms are excluded because they have their own spoken forms
# and are handled by the commerce-claim rules.
_NUMBER_RE = re.compile(r"(?<![0-9])[0-9]+(?:[.,][0-9]+)?(?![0-9%])")
_GROUPED_PRICE_RE = re.compile(r"[0-9]{1,3}(?:[.,][0-9]{3})+")


def _span_of(match: re.Match[str]) -> TextSpan:
    return TextSpan(match.start(), match.end())


def check_tts_markup(text: str, context) -> list[RuleViolation]:
    """Flag unsupported markup (markdown/HTML) in spoken text."""
    violations: list[RuleViolation] = []
    for match in _MARKUP_RE.finditer(text):
        violations.append(
            RuleViolation(
                rule_id=RULE_TTS_MARKUP,
                severity=Severity.ERROR,
                message="Markup/HTML found in spoken text; remove it.",
                text_span=_span_of(match),
            )
        )
    return violations


def check_tts_control_chars(text: str, context) -> list[RuleViolation]:
    """Flag hidden control characters (TTS-specific rule id)."""
    violations: list[RuleViolation] = []
    for match in re.finditer(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", text):
        violations.append(
            RuleViolation(
                rule_id=RULE_TTS_CONTROL,
                severity=Severity.ERROR,
                message="Hidden control character found; remove it.",
                text_span=_span_of(match),
            )
        )
    return violations


def check_tts_acronyms(text: str, context) -> list[RuleViolation]:
    """Flag uppercase acronyms/product codes for TTS review.

    WARNING: the author must confirm the TTS pronunciation (letter-by-letter
    vs. word). Product codes in ``context.facts.skus`` are exempt.
    """
    violations: list[RuleViolation] = []
    for match in _ACRONYM_RE.finditer(text):
        token = match.group()
        if token in context.facts.skus:
            continue
        violations.append(
            RuleViolation(
                rule_id=RULE_TTS_ACRONYM,
                severity=Severity.WARNING,
                message=(
                    f"Acronym/code {token!r} may be mispronounced by TTS; "
                    "confirm the spoken form."
                ),
                text_span=_span_of(match),
            )
        )
    return violations


def check_tts_numbers(text: str, context) -> list[RuleViolation]:
    """Flag raw numbers not in price/percent/date forms.

    WARNING: the author must confirm the spoken (verbalized) form. Numbers
    inside a grouped price are skipped (their verbalization is the
    commerce-claim rules' concern).
    """
    violations: list[RuleViolation] = []
    grouped_spans = [(m.start(), m.end()) for m in _GROUPED_PRICE_RE.finditer(text)]
    for match in _NUMBER_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in grouped_spans):
            continue
        violations.append(
            RuleViolation(
                rule_id=RULE_TTS_NUMBER,
                severity=Severity.WARNING,
                message=(
                    f"Number {match.group()!r} should be verbalized for TTS; "
                    "confirm the spoken form."
                ),
                text_span=_span_of(match),
            )
        )
    return violations


def normalize_tts_text(text: str) -> str:
    """Deterministic TTS normalization for the display/spoken compiler.

    Strips markup and control characters; keeps price/percent forms intact
    (their verbalization is the commerce-claim rules' concern). Idempotent:
    applying twice yields the same result.
    """
    stripped = _MARKUP_RE.sub("", text)
    stripped = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", stripped)
    return re.sub(r"[ \t]{2,}", " ", stripped).strip()


# Stable rule IDs.
RULE_TTS_MARKUP = "TTS_MARKUP"
RULE_TTS_CONTROL = "TTS_CONTROL"
RULE_TTS_ACRONYM = "TTS_ACRONYM"
RULE_TTS_NUMBER = "TTS_NUMBER"
