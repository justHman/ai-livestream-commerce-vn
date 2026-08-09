"""Deterministic Vietnamese speech-duration estimation (task 3.4).

``SpeechDurationEstimator`` estimates how long a text is likely to take when
spoken by VieNeu, using Vietnamese syllable-like units, punctuation pauses,
and compact written forms whose spoken form is much longer than their raw
character count (numbers, currency, percentages, acronyms, English-like and
product tokens).

The estimator is a segmenter's *feature*, not an oracle: it never rewrites,
normalizes, or mutates the input text — it only returns a finite nonnegative
millisecond estimate. Coefficients are explicit, frozen, validated sensible,
and finite; calibration against measured VieNeu audio belongs to the
benchmark (design Decision 5), never inside this module.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

__all__ = ["SpeechDurationEstimator", "DurationCoefficients"]


@dataclass(frozen=True)
class DurationCoefficients:
    """Explicit, finite, sensible per-unit spoken-duration coefficients (ms).

    All values are frozen and validated (``>= 0``, finite) at construction,
    so a misconfigured estimator fails loudly instead of producing garbage
    durations. ``syllable_ms`` is the Vietnamese baseline; compact forms are
    multipliers on top of it (e.g. ``199.000đ`` reads as
    "một trăm chín mươi chín nghìn đồng": ~6 syllables + number stress).
    """

    syllable_ms: float = 160.0
    number_multiplier: float = 1.4
    currency_multiplier: float = 1.6
    percent_multiplier: float = 1.4
    acronym_multiplier: float = 1.5
    ascii_multiplier: float = 1.2
    punctuation_pause_ms: float = 240.0
    comma_pause_ms: float = 140.0
    phrase_break_pause_ms: float = 120.0

    # Sensible upper limits so arbitrary finite coefficients can never
    # overflow during exponentiation in ``estimate_ms`` (calibrated defaults
    # are far below these; they exist only to keep the finite guarantee).
    MAX_MS = 60_000.0
    MAX_MULTIPLIER = 100.0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(
                    f"coefficient {name} must be a finite nonnegative number, got {value!r}"
                )
            limit = self.MAX_MULTIPLIER if name.endswith("_multiplier") else self.MAX_MS
            if value > limit:
                raise ValueError(
                    f"coefficient {name} must be <= {limit:g}, got {value!r} "
                    "(prevents overflow in the finite-duration guarantee)"
                )


class SpeechDurationEstimator:
    """Deterministic speech-duration estimator for Vietnamese text.

    Stateless and pure: ``estimate_ms`` never mutates the input and always
    returns a finite nonnegative float, even for adversarial inputs (deeply
    nested or unbalanced quotes, 10k-char digit runs).
    """

    # Compact written forms whose spoken form differs from plain characters.
    # Token-boundary aware: currency symbols, digit-adjacent suffixes
    # (``99.000đ``, ``50k``), and standalone currency words/codes (``VND``,
    # ``đồng``); never bare letters inside ordinary Vietnamese words
    # (``k``/``đ`` in ``không``/``đi`` are plain syllables).
    _CURRENCY = re.compile(
        r"(?<!\w)(?:đồng|vnđ|vnd|usd|dollar|đô|nghìn|triệu|tỷ|₫|đ|k)(?!\w)"
        r"|\d{1,15}(?:[.,]\d{1,15})?\s*(?:đ|₫|k)"
    )
    _GROUPED_NUMBER = re.compile(r"\d{1,3}(?:[.,]\d{3})+")
    # Any digit token, grouped or ungrouped (bare "50" counts like "1.234").
    _NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
    # Digit runs are bounded ({1,15}) so findall over a pathological long
    # digit run stays linear: an unbounded ``\d+`` retries every position
    # of the run (quadratic) before failing on the missing suffix.
    _PERCENT = re.compile(r"\d{1,15}(?:[.,]\d{1,15})?\s*%")
    # ASCII dotted acronyms only (U.S.A., e.g.): the broad Unicode uppercase
    # range counted diacritic letter runs as acronyms.
    _ACRONYM = re.compile(r"\b(?:[A-Z]\.){2,}[A-Z]?\.?")
    _WORD = re.compile(r"[\w]+")
    _SENTENCE_END = re.compile(r"[.!?…]")
    _PAUSE_COMMA = re.compile(r"[,;:]")
    _PAUSE_OTHER = re.compile(r"[()\"“”]")
    _DIGIT_CHUNK = re.compile(r"\d")
    # Chars of the Vietnamese alphabet, including diacritic vowels, so an
    # ASCII word adjacent to one (e.g. "sỉ", "bơ") is one Vietnamese
    # syllable, not an English token plus a separate syllable.
    _VI_CHARS = frozenset(
        "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵ"
        "aăâbcdđeêghiklmnoôơpqrstuưvxy"
    )

    # Bounded digit weight: 6 digit-groups (nghìn→tỷ) is the practical
    # ceiling for spoken Vietnamese; beyond that, cap at 6 to stay finite.
    _MAX_DIGIT_GROUPS = 6
    # Bounded complexity per syllable: ``SKU-P004``-style strings never read
    # faster than plain words, but the factor is capped so any input stays
    # finite.
    _MAX_SYLLABLE_COMPLEXITY = 3.0

    def __init__(self, coefficients: DurationCoefficients | None = None) -> None:
        self._c = coefficients if coefficients is not None else DurationCoefficients()

    # -- estimation --------------------------------------------------------

    def estimate_ms(self, text: str) -> float:
        """Estimated spoken duration of ``text`` in milliseconds.

        Deterministic: same text → same estimate. Returns a finite
        nonnegative float for ANY input (including empty text — 0.0).
        """
        if not text:
            return 0.0

        syllables = self._estimate_syllables(text)
        number_tokens = len(self._NUMBER.findall(text))
        percents = len(self._PERCENT.findall(text))
        currencies = len(self._CURRENCY.findall(text))

        digit_chars = len(self._DIGIT_CHUNK.findall(text))
        if digit_chars > 0:
            syllables += 0.5 * min(digit_chars, 12)

        multiplier = 1.0
        multiplier *= self._c.number_multiplier ** min(number_tokens, self._MAX_DIGIT_GROUPS)
        multiplier *= self._c.currency_multiplier ** min(currencies, 4)
        multiplier *= self._c.percent_multiplier ** min(percents, 3)
        multiplier *= self._c.acronym_multiplier ** min(self._count_acronyms(text), 3)

        syllable_ms = self._c.syllable_ms * multiplier
        base_ms = syllables * syllable_ms

        pauses = self._count_punctuation_pauses(text)
        base_ms += (
            self._c.punctuation_pause_ms * pauses["sentence"]
            + self._c.comma_pause_ms * pauses["comma"]
            + self._c.phrase_break_pause_ms * pauses["phrase"]
        )

        return float(min(base_ms, self._bound_estimate(text)))

    # -- internal helpers --------------------------------------------------

    def _estimate_syllables(self, text: str) -> float:
        """Syllable-like units over Unicode word runs.

        A word is a maximal ``\\w`` run (``_WORD``). If every character is
        in the Vietnamese alphabet (ASCII or diacritic), it is one
        Vietnamese syllable unit with a bounded complexity factor for
        unusually long words — diacritic words are never length-scored, so
        ASCII accents do not gate English detection. Otherwise the word is
        an ASCII English/product token: one base unit, inflated by
        ``ascii_multiplier``, times the same bounded complexity. Digits and
        grouped numbers are excluded here: their multiplier/counters are
        handled separately in ``estimate_ms`` (``_NUMBER``)."""
        total = 0.0
        index = 0
        while index < len(text):
            char = text[index]
            if char.isspace():
                index += 1
                continue
            word_match = self._WORD.match(text, index)
            if word_match:
                word = word_match.group()
                if all(c in self._VI_CHARS for c in word):
                    # Vietnamese syllable unit (one per word run), with a
                    # bounded complexity factor for unusually long words.
                    total += 1.0 + min(len(word) - 1, self._MAX_SYLLABLE_COMPLEXITY - 1.0)
                else:
                    # ASCII English/product token: base unit times the
                    # ascii multiplier (reads letter-by-letter).
                    total += (
                        1.0 + min(len(word) - 1, self._MAX_SYLLABLE_COMPLEXITY - 1.0)
                    ) * self._c.ascii_multiplier
                index = word_match.end()
            else:
                total += 1.0
                index += 1
        return total

    def _count_acronyms(self, text: str) -> int:
        return len(self._ACRONYM.findall(text))

    def _worst_multiplier(self) -> float:
        """Overflow-safe ceiling of the compact-form multiplier product.

        Multipliers are validated ``<= 100`` (see ``DurationCoefficients``),
        and each exponent is capped (``_MAX_DIGIT_GROUPS`` digits, 4
        currencies, 3 percents/acronyms), so the product can never
        overflow: at most 100**6 * 100**4 * 100**3 * 100**3 = 10**32, far
        below ``sys.float_info.max``.
        """
        return self._c.number_multiplier**self._MAX_DIGIT_GROUPS * (
            self._c.currency_multiplier**4
            * self._c.percent_multiplier**3
            * self._c.acronym_multiplier**3
        )

    def _count_punctuation_pauses(self, text: str) -> dict[str, int]:
        return {
            "sentence": len(self._SENTENCE_END.findall(text)),
            "comma": len(self._PAUSE_COMMA.findall(text)),
            "phrase": len(self._PAUSE_OTHER.findall(text)),
        }

    def _bound_estimate(self, text: str) -> float:
        # Ceiling: syllables (raw chars worst-case) at the max multiplier,
        # plus one max pause per character. Finiteness is a hard invariant:
        # worst_multiplier is bounded by the validated coefficient caps
        # (<= 10**32), so the product stays far below float overflow even
        # for pathological inputs.
        worst_syllables = len(text) * self._MAX_SYLLABLE_COMPLEXITY
        base = worst_syllables * self._c.syllable_ms * self._worst_multiplier()
        return base + len(text) * max(
            self._c.punctuation_pause_ms,
            self._c.comma_pause_ms,
            self._c.phrase_break_pause_ms,
        )
