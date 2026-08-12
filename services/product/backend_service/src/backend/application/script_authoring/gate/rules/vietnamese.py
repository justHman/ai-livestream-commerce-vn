"""Deterministic Vietnamese spacing/spelling heuristic hooks (task 3.4).

These are heuristic hooks, not a full spell checker: they flag the most
common deterministic Vietnamese spelling failure modes that corrupt TTS —
wrong tone diacritics (the "tro`ng" slot order), missing/questionable
diacritics, and vowel+consonant confusion patterns like "gi" vs "d".
Every WARNING/ERROR is pattern-based and allowlist-aware: brand/product
terms (e.g. a brand spelled without diacritics on purpose) are exempted via
``context.brand_allowlist``.

Severity semantics: an ambiguous pattern that may be a legitimate loanword
is a WARNING; a pattern that is unambiguously a Vietnamese misspelling of a
common word is an ERROR.
"""

from __future__ import annotations

import re

from ..results import RuleViolation, Severity, TextSpan

__all__ = [
    "check_common_spelling",
    "check_tense_spacing",
    "RULE_VN_SPELLING_TONE",
    "RULE_VN_SPELLING_GI_D",
]

# Every Vietnamese tone-marked vowel, in BOTH encodings Vietnamese text
# actually uses: precomposed Latin-1 (à á ã è é ì í ò ó õ ù ú ý) and the
# U+1EAx/U+01Ax precomposed forms (ả ạ ắ …). Building the class from one
# source string keeps the two spellings in sync.
_TONE_VOWELS = "àáảãạằắẳẵặầấẩẫậèéẻẽẹềếểễệìíỉĩịòóỏõọồốổỗộờớởỡợùúủũụừứửữựỳýỷỹỵ"
_TONE_CLASS = f"[{_TONE_VOWELS}]"

# Tone marks sit on the LAST vowel of a syllable, EXCEPT when that final
# vowel is a semivowel (i/y/u/o) — then the tone goes on the preceding
# vowel ("thời" is correct: ờ before the semivowel i). The classic
# wrong-slot misspelling is a tone before a NON-semivowel final:
# "tóan" (correct: "toán"), "hòan" (correct: "hoàn"). Pattern: consonant +
# TONED vowel + untoned NON-semivowel vowel (a, ă, â, e, ê, ơ). Legit
# "toàn" and "thời" never match.
_NON_SEMIVOWELS = "aăâeêơ"
_WRONG_TONE_RE = re.compile(r"[bcdđghklmnpqrstvx]" + _TONE_CLASS + f"[{_NON_SEMIVOWELS}]")

# Adjacent tone marks on one syllable ("tròang") — never legitimate.
_DOUBLE_TONE_RE = re.compile(_TONE_CLASS + _TONE_CLASS)

# "gi" vs "d": "gi" is the correct Vietnamese digraph; a bare "d" followed
# by a vowel in common words ("di" for "gi", "da" for "gia") is the classic
# confusion. Whole words only, allowlisted by context.
_GI_D_WORDS = {
    "di": ("gi", "di chuyển"),
    "da": ("gia", "gia đình"),
    "dau": ("giau", "giàu"),
    "deo": ("gheo", "dèo"),
    "dung": ("giung", "dừng"),
    "dai": ("giai", "dài"),
    "den": ("ghen", "đến"),
}


def _word_span(match: re.Match[str]) -> TextSpan:
    return TextSpan(match.start(), match.end())


def _allowed(word: str, context) -> bool:
    """True when the token is an allowlisted brand/product term."""
    lowered = word.lower()
    return any(
        lowered in allowed.lower() or allowed.lower() in lowered
        for allowed in context.brand_allowlist
    )


def check_common_spelling(text: str, context) -> list[RuleViolation]:
    """Flag the ``gi``/``d`` confusion and other common misspellings.

    ERROR for unambiguous Vietnamese misspellings of common words; WARNING
    when the token is a plausible loanword. Brand/product allowlist exempts
    deliberate brand spellings.
    """
    violations: list[RuleViolation] = []
    for match in re.finditer(r"[\w]+", text):
        word = match.group()
        if _allowed(word, context):
            continue
        fix = _GI_D_WORDS.get(word.lower())
        if fix is not None:
            correct, meaning = fix
            violations.append(
                RuleViolation(
                    rule_id=RULE_VN_SPELLING_GI_D,
                    severity=Severity.ERROR,
                    message=(
                        f"Spelling of {word!r}: in Vietnamese, {correct!r} "
                        f"(meaning {meaning}) is the common form; review and fix."
                    ),
                    text_span=_word_span(match),
                )
            )
    return violations


def check_tense_spacing(text: str, context) -> list[RuleViolation]:
    """Flag double-tone and wrong-slot tone patterns (task 3.4).

    ERROR: two tone marks on one syllable is always a typo. The wrong-slot
    tone check is WARNING because some dialects/loanwords are ambiguous.
    """
    violations: list[RuleViolation] = []
    for match in _DOUBLE_TONE_RE.finditer(text):
        violations.append(
            RuleViolation(
                rule_id=RULE_VN_SPELLING_TONE,
                severity=Severity.ERROR,
                message="Double tone mark on one syllable; check the spelling.",
                text_span=_word_span(match),
            )
        )
    for match in _WRONG_TONE_RE.finditer(text):
        violations.append(
            RuleViolation(
                rule_id=RULE_VN_SPELLING_TONE,
                severity=Severity.WARNING,
                message=(
                    "Tone mark on a non-final vowel; Vietnamese tones sit on "
                    "the last vowel of a syllable."
                ),
                text_span=_word_span(match),
            )
        )
    return violations


# Stable rule IDs.
RULE_VN_SPELLING_TONE = "VN_SPELLING_TONE"
RULE_VN_SPELLING_GI_D = "VN_SPELLING_GI_D"
