"""Profanity/offensive lexicon and teencode/obfuscation patterns (task 3.5).

The lexicon is a CURATED, VERSIONED runtime resource shipped with the
backend (``resources/profanity/curated_lexicon_v2.json``) — never a raw
downloaded dataset. Provenance and license metadata live in the resource
itself (task 3.6); before any external dataset-derived lexicon can be
activated, that resource's provenance section MUST be complete and its
false-positive tests MUST pass (see tests/unit/script_authoring/).

Matching is deterministic and diacritic-aware. Query tokens that carry
Vietnamese diacritics are matched EXACTLY against diacritic entries
(``lồn`` matches ``lồn`` but never the common word ``lon``-folded
``các``/``đi``); bare-ASCII tokens are matched against ASCII entries after
bounded teencode substitution (``c4c`` -> ``cac``, ``sh1t`` -> ``shit``)
and separator stripping (``d.m.m`` -> ``dmm``). The curated word set is
curated so that no ASCII entry collides with a common standalone
Vietnamese word; the brand/product allowlist is consulted FIRST so
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
    Path(__file__).resolve().parents[6] / "resources" / "profanity" / "curated_lexicon_v2.json"
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

    Diacritic-aware two-set matching (task 3.6 activation guard):

    - A query token that carries Vietnamese diacritics is matched EXACTLY
      against the diacritic entry set, so ``lồn`` matches the curated
      ``lồn`` entry while the common words ``lon``, ``các``, ``đi`` never
      match anything. Diacritics are never folded away on either side.
    - A bare-ASCII token is teencode-translated (``c4c`` -> ``cac``,
      ``sh1t`` -> ``shit``), separators stripped (``d.m.m`` -> ``dmm``),
      then matched against the ASCII entry set. The curated word set is
      curated so no ASCII entry collides with a common standalone
      Vietnamese word.

    The allowlist is consulted before normalization so brand terms never
    trip a variant.
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
        # Two lookup sets: diacritic entries (exact) and ASCII entries
        # (teencode/separator-normalized). A word is "ASCII" iff it has no
        # Vietnamese diacritic — the two sets are disjoint by construction.
        self._ascii_entries: frozenset[str] = frozenset(
            self._normalize_ascii(w) for w in self.words if not _has_diacritics(w)
        )
        self._diacritic_entries: frozenset[str] = frozenset(
            w for w in self.words if _has_diacritics(w)
        )

    @classmethod
    def from_resource(cls, resource: dict[str, Any]) -> ProfanityLexicon:
        """Build from the curated resource dict (validates provenance, task 3.6)."""
        provenance = resource.get("provenance", {})
        missing = [
            key for key in ("version", "source", "license", "curated_by") if not provenance.get(key)
        ]
        if missing:
            raise ValueError(f"profanity lexicon provenance incomplete; missing {missing}")
        activation = provenance.get("activation_status")
        if activation != "active":
            raise ValueError(
                "profanity lexicon is not activated for runtime use "
                f"(activation_status={activation!r}); complete provenance and "
                "false-positive review before activation"
            )
        return cls(
            resource.get("words", []),
            version=str(provenance["version"]),
            source=str(provenance["source"]),
            license=str(provenance["license"]),
            curated_by=str(provenance["curated_by"]),
        )

    def is_offensive(self, token: str) -> bool:
        lowered = token.lower()
        if _has_diacritics(lowered):
            # Diacritic token: exact match against diacritic entries only.
            # Never fold; folding would collide with common words
            # ("lồn" -> "lon" is a train station).
            return lowered in self._diacritic_entries
        return self._normalize_ascii(lowered) in self._ascii_entries

    @staticmethod
    def _normalize_ascii(token: str) -> str:
        # Bounded teencode + separator normalization for ASCII tokens only.
        # Source text is never rewritten; this is purely a lookup key.
        return _SEPARATOR_RE.sub("", token.translate(_TEENCODE_MAP))


# Vietnamese diacritic chars that distinguish diacritic (exact-match) entries
# from ASCII (normalized-match) entries. "đ" (U+0111) is included: it is a
# separate Vietnamese letter, so "đm" is a diacritic entry matched exactly.
_DIACRITIC_CHARS = frozenset("àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđ")


def _has_diacritics(text: str) -> bool:
    return any(char in _DIACRITIC_CHARS for char in text)


def load_curated_lexicon(
    resource: Path | None = None,
) -> ProfanityLexicon:
    """Load the curated lexicon resource (default: packaged v2)."""
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
        if any(allowed.lower() in token.lower() for allowed in context.brand_allowlist):
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
