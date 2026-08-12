"""Profanity/offensive lexicon and teencode/obfuscation patterns (task 3.5).

The lexicon is a CURATED, VERSIONED runtime resource shipped with the
backend (``resources/profanity/curated_lexicon_v1.json``) — never a raw
downloaded dataset. Provenance and license metadata live in the resource
itself (task 3.6); before any external dataset-derived lexicon can be
activated, that resource's provenance section MUST be complete and its
false-positive tests MUST pass (see tests/unit/script_authoring/).

Matching is deterministic: normalized token lookup plus bounded
obfuscation patterns (teencode substitutions, separators inside a word,
run-together punctuation) with a brand/product allowlist checked FIRST so
authorized terms never trigger a false positive.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from ..results import RuleViolation, Severity, TextSpan

__all__ = [
    "ProfanityLexicon",
    "check_profanity",
    "load_curated_lexicon",
    "RULE_PROFANITY_OFFENSIVE",
]

RULE_PROFANITY_OFFENSIVE = "PROFANITY_OFFENSIVE"

# Default curated resource: service-level ``resources/profanity/`` directory
# (mirrors the repository's resource convention). Resolved relative to the
# package so it works from a source checkout.
_DEFAULT_RESOURCE = (
    Path(__file__).resolve().parents[6]
    / "resources"
    / "profanity"
    / "curated_lexicon_v1.json"
)

# Bounded obfuscation patterns: teencode digit/symbol-for-letter swaps
# (4=a, 0=o, 1=i, 3=e, 5=s, 7=t, 8=b, $=s, @=a) plus separators (dots,
# dashes, underscores) inserted inside a word.
_TEENCODE_MAP = str.maketrans(
    {
        "4": "a",
        "0": "o",
        "1": "i",
        "3": "e",
        "5": "s",
        "7": "t",
        "8": "b",
        "$": "s",
        "@": "a",
    }
)

_SEPARATOR_RE = re.compile(r"[._-]+")

# Any word-ish token is a candidate; separator-obfuscated variants join
# with dot/underscore/dash (never whitespace — spaces are word boundaries).
# The lexicon lookup normalizes it. Bounded length keeps matching linear.
_CANDIDATE_RE = re.compile(r"[\w$@]+(?:[._-][\w$@]+)*", re.UNICODE)


class ProfanityLexicon:
    """Versioned curated lexicon with provenance and deterministic lookup.

    ``is_offensive(token)`` normalizes Vietnamese diacritics away and strips
    obfuscation before checking the curated word set, so both "dmm" and
    "d.m.m" match the same curated entry. The allowlist is consulted before
    normalization so brand terms never trip a variant.
    """

    def __init__(
        self,
        words: list[str],
        *,
        version: str,
        source: str,
        license: str,
        curated_by: str,
    ) -> None:
        self.words: frozenset[str] = frozenset(w.lower() for w in words)
        self.version = version
        self.source = source
        self.license = license
        self.curated_by = curated_by

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> ProfanityLexicon:
        """Build from the curated resource dict (validates provenance, task 3.6)."""
        provenance = resource.get("provenance", {})
        missing = [
            key
            for key in ("version", "source", "license", "curated_by")
            if not provenance.get(key)
        ]
        if missing:
            raise ValueError(
                f"profanity lexicon provenance incomplete; missing {missing}"
            )
        return cls(
            resource.get("words", []),
            version=str(provenance["version"]),
            source=str(provenance["source"]),
            license=str(provenance["license"]),
            curated_by=str(provenance["curated_by"]),
        )

    def is_offensive(self, token: str) -> bool:
        return self._normalize(token) in self.words

    def _normalize(self, token: str) -> str:
        lowered = token.lower()
        # Strip teencode substitutions first, then separators, then
        # diacritics so "d.m.m", "dmm", "4mm" all collapse to the same key.
        transliterated = lowered.translate(_TEENCODE_MAP)
        stripped = _SEPARATOR_RE.sub("", transliterated)
        return self._strip_diacritics(stripped)

    @staticmethod
    def _strip_diacritics(text: str) -> str:
        # Deterministic Vietnamese diacritic folding for lexicon keys only.
        # Source text is never rewritten; this is purely a lookup key.
        replacements = {
            "à": "a", "á": "a", "ả": "a", "ã": "a", "ạ": "a",
            "ă": "a", "ằ": "a", "ắ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
            "â": "a", "ầ": "a", "ấ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
            "đ": "d",
            "è": "e", "é": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
            "ê": "e", "ề": "e", "ế": "e", "ể": "e", "ễ": "e", "ệ": "e",
            "ì": "i", "í": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
            "ò": "o", "ó": "o", "ỏ": "o", "õ": "o", "ọ": "o",
            "ô": "o", "ồ": "o", "ố": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
            "ơ": "o", "ờ": "o", "ớ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
            "ù": "u", "ú": "u", "ủ": "u", "ũ": "u", "ụ": "u",
            "ư": "u", "ừ": "u", "ứ": "u", "ử": "u", "ữ": "u", "ự": "u",
            "ỳ": "y", "ý": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
        }
        return "".join(replacements.get(c, c) for c in text)


def load_curated_lexicon(
    resource: Path | None = None,
) -> ProfanityLexicon:
    """Load the curated lexicon resource (default: packaged v1)."""
    path = resource if resource is not None else _DEFAULT_RESOURCE
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    return ProfanityLexicon.from_resource(data)


def check_profanity(
    text: str,
    context,
    lexicon: ProfanityLexicon | None = None,
) -> list[RuleViolation]:
    """Flag curated profanity/offensive terms, including teencode variants.

    ERROR severity: offensive language is a hard content failure. The
    brand/product allowlist is consulted before normalization so authorized
    terms never false-positive.
    """
    if lexicon is None:
        lexicon = load_curated_lexicon()
    violations: list[RuleViolation] = []
    for match in _CANDIDATE_RE.finditer(text):
        token = match.group()
        if any(
            allowed.lower() in token.lower() for allowed in context.brand_allowlist
        ):
            continue
        if lexicon.is_offensive(token):
            violations.append(
                RuleViolation(
                    rule_id=RULE_PROFANITY_OFFENSIVE,
                    severity=Severity.ERROR,
                    message="Offensive or profane language found; remove it.",
                    text_span=TextSpan(match.start(), match.end()),
                )
            )
    return violations
