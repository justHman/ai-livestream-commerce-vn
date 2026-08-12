"""Format/Unicode/whitespace/punctuation rules (task 3.3).

Pure deterministic checks over raw text: hidden control characters, mixed
punctuation/typographic confusion, whitespace hygiene, repeated punctuation,
and configured house-style rejection of the em-dash. All checks are pattern
based; there is no AI-detection heuristic anywhere in the gate.
"""

from __future__ import annotations

import re

from ..results import RuleViolation, Severity, TextSpan

__all__ = [
    "check_control_characters",
    "check_whitespace",
    "check_punctuation",
    "check_em_dash",
    "RULE_FORMAT_CONTROL",
    "RULE_FORMAT_WHITESPACE",
    "RULE_FORMAT_PUNCTUATION",
    "RULE_STYLE_EM_DASH",
]

# Hidden/format characters that never belong in script text: Unicode control
# chars (C0/C1), the soft hyphen (U+00AD), bidi overrides (U+202A-U+202E),
# zero-width space (U+200B), word joiner (U+2060), and zero-width no-break
# space (U+FEFF, a.k.a. BOM). All escapes are explicit so the source file
# holds no raw control bytes.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\xad​‪-‮⁠﻿]")

_CONTROL_NAMES = {
    "\xad": "soft hyphen",
    "​": "zero-width space",
    "‪": "left-to-right embedding",
    "‫": "right-to-left embedding",
    "‭": "left-to-right override",
    "‮": "right-to-left override",
    "⁠": "word joiner",
    "﻿": "zero-width no-break space",
}


def _span_of(match: re.Match[str]) -> TextSpan:
    return TextSpan(match.start(), match.end())


def check_control_characters(text: str, context) -> list[RuleViolation]:
    """Flag hidden control characters that a TTS engine would mispronounce.

    Any occurrence is an ERROR: invisible chars corrupt spoken output and
    should be stripped at the source, not normalized at TTS time.
    """
    violations: list[RuleViolation] = []
    for match in _CONTROL_RE.finditer(text):
        char = match.group()
        label = _CONTROL_NAMES.get(char, f"control character U+{ord(char):04X}")
        violations.append(
            RuleViolation(
                rule_id=RULE_FORMAT_CONTROL,
                severity=Severity.ERROR,
                message=f"Hidden {label} found in text; remove it.",
                text_span=_span_of(match),
            )
        )
    return violations


# Multiple spaces, space before punctuation, tab characters, trailing space.
_WHITESPACE_RE = re.compile(r"[ \t]{2,}|[ ]+[,.;:!?]|\t|[ ]+$", re.MULTILINE)


def check_whitespace(text: str, context) -> list[RuleViolation]:
    """Flag whitespace hygiene issues (double space, tab, space-before-punct).

    WARNING severity: these are readability/TTS-pause nuisances, not content
    failures.
    """
    violations: list[RuleViolation] = []
    for match in _WHITESPACE_RE.finditer(text):
        group = match.group()
        if group == "\t":
            message = "Tab character found; use a single space."
        elif group.startswith(" ") and group.endswith((",", ".", ";", ":", "!", "?")):
            message = f"Space before {group.strip()[-1]!r} punctuation."
        else:
            message = "Multiple consecutive spaces found; collapse to one."
        violations.append(
            RuleViolation(
                rule_id=RULE_FORMAT_WHITESPACE,
                severity=Severity.WARNING,
                message=message,
                text_span=_span_of(match),
            )
        )
    return violations


# Repeated punctuation runs: "!!", "??", "?!", "!?", "..", ",,", ";;",
# and adjacent mixed runs like "!?"/"?!". Three dots and a single ellipsis
# are legitimate, so only runs of 2+ interrobangs, 4+ dots, or 2+ commas/
# semicolons are flagged.
_REPEATED_PUNCT_RE = re.compile(r"[!?]{2,}|\.{4,}|[,;]{2,}|[!?][?!]")


def check_punctuation(text: str, context) -> list[RuleViolation]:
    """Flag malformed/repeated punctuation patterns (task 3.3).

    ERROR: repeated interrobang/exclamation runs and 4+ dots are sloppy
    shouting/typo forms that corrupt TTS intonation. A single ellipsis
    ("...") is allowed.
    """
    violations: list[RuleViolation] = []
    for match in _REPEATED_PUNCT_RE.finditer(text):
        group = match.group()
        message = {
            "??": "Repeated question mark.",
            "!!": "Repeated exclamation mark.",
            "?!": "Mixed question/exclamation mark.",
            "!?": "Mixed exclamation/question mark.",
        }.get(group, f"Repeated punctuation {group!r}.")
        violations.append(
            RuleViolation(
                rule_id=RULE_FORMAT_PUNCTUATION,
                severity=Severity.ERROR,
                message=message,
                text_span=_span_of(match),
            )
        )
    return violations


# Em dash (U+2014) and en dash (U+2013), written as escapes so the source
# holds no raw non-ASCII beyond Vietnamese text in messages.
_EM_DASH_RE = re.compile("[—–]")


def check_em_dash(text: str, context) -> list[RuleViolation]:
    """Configured house-style rejection of the em/en dash (task 3.3).

    ERROR only when the context does not allow em-dashes; the caller can
    configure ``allow_em_dash=True`` when the house style permits it.
    """
    if context.allow_em_dash:
        return []
    violations: list[RuleViolation] = []
    for match in _EM_DASH_RE.finditer(text):
        violations.append(
            RuleViolation(
                rule_id=RULE_STYLE_EM_DASH,
                severity=Severity.ERROR,
                message="Em/en dash found; house style forbids it (use commas or periods).",
                text_span=_span_of(match),
            )
        )
    return violations


# Stable rule IDs (see registry.py for the namespace).
RULE_FORMAT_CONTROL = "FORMAT_CONTROL"
RULE_FORMAT_WHITESPACE = "FORMAT_WHITESPACE"
RULE_FORMAT_PUNCTUATION = "FORMAT_PUNCTUATION"
RULE_STYLE_EM_DASH = "STYLE_EM_DASH"
