"""Deterministic ``display_text`` -> ``spoken_text`` compilation (task 4.1).

Decision 4: a draft/version carries a pretty ``display_text`` for humans and
an exact ``spoken_text`` that VieNeu/Change A will actually receive. This
module turns the display form into the spoken form through a fixed chain of
deterministic, idempotent normalizers — prices (``299.000đ``), percentages
(``20%``), bare numbers, acronyms/SKU codes, punctuation, hidden control
characters, and unsupported markup.

The compile is PURE: no LLM, no network, no filesystem. It NEVER adds
semantic embellishment — every change is a mechanical read-aloud expansion
of what the display text already says, and the returned provenance list
names exactly which normalizer ids applied. ``compile_spoken_text`` is
idempotent: compiled text compiles to itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CompileResult",
    "CompiledScriptVersion",
    "compile_spoken_text",
    "expand_vietnamese_number",
    "NORMALIZER_IDS",
]

# Normalizer ids in application order. Stable: they are provenance values
# recorded on compiled versions, so their meaning never changes.
NORMALIZER_IDS: tuple[str, ...] = (
    "strip_markup_and_controls",
    "collapse_whitespace",
    "punctuation_and_hyphen",
    "acronym_spelling",
    "currency_and_price",
    "percent",
    "number_to_words",
)

# Vietnamese number words (exact, canonical forms used in spoken commerce).
_UNITS = (
    "",
    "một",
    "hai",
    "ba",
    "bốn",
    "năm",
    "sáu",
    "bảy",
    "tám",
    "chín",
)
_TEENS_PREFIX = (
    "mười",
    "mười một",
    "mười hai",
    "mười ba",
    "mười bốn",
    "mười lăm",
    "mười sáu",
    "mười bảy",
    "mười tám",
    "mười chín",
)
_TENS = (
    "",
    "mười",
    "hai mươi",
    "ba mươi",
    "bốn mươi",
    "năm mươi",
    "sáu mươi",
    "bảy mươi",
    "tám mươi",
    "chín mươi",
)
_HUNDRED = "trăm"
_GROUPS = ("", "nghìn", "triệu", "tỷ")

# A grouped price with an optional currency suffix: "299.000đ", "299.000 đ",
# "1.299.000", "20.000k" (vietnamese commerce uses both separators).
# Longer alternates first ("đồng" before "đ") so a full suffix is never
# partially consumed.
_GROUPED_PRICE_RE = re.compile(r"\d{1,3}(?:[.,]\d{3})+\s*(?:đồng|VND|vnđ|₫|đ|k|K)?")

# A compact price with a currency suffix: "299.000đ", "50k", "99đ".
_COMPACT_CURRENCY_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:[.,]\d{1,3})+|[1-9]\d*)\s*(đ|₫|đồng|k|K)(?!\w)")

# Percentages: "20%", "12,5%", "20 %".
_PERCENT_RE = re.compile(r"(?<!\w)(\d{1,3}(?:[.,]\d{1,3})?)\s*%(?!\w)")

# A bare integer or decimal number (excludes grouped prices and percents,
# which are expanded by the earlier normalizers).
_NUMBER_RE = re.compile(r"(?<![\w.,])(\d{1,15}(?:[.,]\d{1,3})?)(?![\w.,%])")

# Uppercase acronyms/product codes: "ABC", "SKU-123", "HT1" -> spelled out
# letter-by-letter ("A B C", "S K U một hai ba").
_ACRONYM_RE = re.compile(r"\b[A-Z]{2,}(?:-\d+)?\b")

# Unsupported markup / hidden control characters (task 3.8 duplicate).
# The control range covers C0/C1 controls plus the common invisible
# Unicode chars that corrupt spoken output: soft hyphen (U+00AD), zero-width
# space (U+200B), bidi embeddings, word joiner (U+2060), BOM (U+FEFF).
_MARKUP_RE = re.compile(r"<[^>]+>|\[[^\]]+\]\([^)]+\)|\*\*[^*]+\*\*|`[^`]+`|^#{1,6}\s", re.MULTILINE)
# C0/C1 controls are removed; invisible space chars (soft hyphen, zero-width
# space, word joiner, BOM) become a space so adjacent words do not merge.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_INVISIBLE_SPACE_RE = re.compile("[\xad​⁠﻿]")

# Em/en dashes and internal hyphens -> a spoken-friendly comma.
_DASH_RE = re.compile(r"[—–]|(?<=[^\s])-(?=[^\s])")


def _number_to_words(n: int) -> str:
    """Convert a nonnegative integer to canonical spoken Vietnamese."""
    if n == 0:
        return "không"
    parts: list[str] = []
    group = 0
    while n > 0:
        chunk = n % 1000
        if chunk or group == 0:
            words = _three_digits(chunk)
            if words:
                if group > 0 and words:
                    parts.append(_GROUPS[group])
                parts.append(words)
        n //= 1000
        group += 1
    parts.reverse()
    return " ".join(parts)


def _three_digits(n: int) -> str:
    """Spoken Vietnamese for 0..999 (no leading group name)."""
    if n == 0:
        return ""
    if n < 10:
        return _UNITS[n]
    if n < 20:
        return _TEENS_PREFIX[n - 10]
    if n < 100:
        tens = n // 10
        units = n % 10
        result = _TENS[tens]
        if units:
            if units == 1:
                result += " mốt"
            elif units == 5:
                result += " lăm"
            else:
                result += f" {_UNITS[units]}"
        return result
    # 100..999: "một trăm lẻ một" for 101, "hai trăm ba mươi" for 230.
    result = f"{_UNITS[n // 100]} {_HUNDRED}"
    rest = n % 100
    if rest:
        if rest < 10:
            result += f" lẻ {_UNITS[rest]}"
        else:
            result += f" {_three_digits(rest)}"
    return result


def expand_vietnamese_number(value: str) -> str:
    """Spoken Vietnamese for a plain number string (task 4.2 numbers).

    ``"299"`` -> ``"hai trăm chín mươi chín"``; thousands grouping is
    stripped (``"1.299.000"`` -> 1299000); a decimal (``"12,5"`` or
    ``"12.5"``) becomes ``"mười hai phẩy năm"``.
    """
    value = value.strip().replace(",", ".")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", value):
        # Thousands grouping: strip the group dots before parsing.
        return _number_to_words(int(value.replace(".", "")))
    if "." in value:
        whole, _, fraction = value.partition(".")
        if not fraction:
            return _number_to_words(int(whole))
        return f"{_number_to_words(int(whole))} phẩy {_number_to_words(int(fraction))}"
    return _number_to_words(int(value))


def _spell_acronym(match: re.Match[str]) -> str:
    """Letter-by-letter spelling of an uppercase token (acronym/SKU)."""
    token = match.group()
    return " ".join(ch for ch in token if ch.isalnum())


def _expand_currency(match: re.Match[str]) -> str:
    """Expand a compact price form into spoken Vietnamese words."""
    number, unit = match.group(1), match.group(2).lower()
    spoken = expand_vietnamese_number(number)
    if unit == "k":
        return f"{spoken} nghìn"
    if unit == "đồng":
        return spoken
    # "đ" / "₫"
    return f"{spoken} đồng"


def _expand_percent(match: re.Match[str]) -> str:
    return f"{expand_vietnamese_number(match.group(1))} phần trăm"


def _expand_bare_number(match: re.Match[str]) -> str:
    return expand_vietnamese_number(match.group(1))


@dataclass(frozen=True)
class CompileResult:
    """Compiled spoken text plus the provenance of applied normalizers.

    ``applied`` lists normalizer ids (see ``NORMALIZER_IDS``) in application
    order; only the ids that actually changed the text are recorded.
    """

    spoken_text: str
    applied: tuple[str, ...]


def compile_spoken_text(display_text: str, *, denomination: str = "đồng") -> CompileResult:
    """Compile a display string into the exact spoken form (task 4.1).

    Deterministic and idempotent: applying ``compile_spoken_text`` to the
    produced ``spoken_text`` returns the same text (and no normalizers
    applied, since spoken forms are already spoken forms).

    ``denomination`` names the currency suffix word used for bare "đ"
    prices (``"đồng"`` by default).
    """
    if denomination not in ("đồng", "VND", "đô"):
        raise ValueError(f"unsupported denomination {denomination!r}")
    applied: list[str] = []

    def _changed(old: str, new: str, nid: str) -> str:
        if new != old:
            applied.append(nid)
        return new

    text = _MARKUP_RE.sub("", display_text)
    text = _CONTROL_RE.sub("", text)
    text = _INVISIBLE_SPACE_RE.sub(" ", text)
    text = _changed(display_text, text, "strip_markup_and_controls")

    # Collapse whitespace: double spaces, tabs, space before punctuation.
    whitespace_stripped = re.sub(r"[ \t]{2,}|[ ]+[,.;:!?]|\t|[ ]+$", " ", text, flags=re.MULTILINE)
    whitespace_stripped = " ".join(whitespace_stripped.split())
    text = _changed(text, whitespace_stripped, "collapse_whitespace")

    dashes = _DASH_RE.sub(",", text)
    if dashes != text:
        applied.append("punctuation_and_hyphen")
    # Remove spaces before punctuation so recompiling is stable (" ," -> ",").
    dashes = re.sub(r"\s+([,.;:!?])", r"\1", dashes)
    text = re.sub(r"\s+", " ", dashes).strip()

    # Grouped prices and compact currency forms expand to full words first,
    # so the number normalizer never sees digits inside them.
    def _grouped_expand(match: re.Match[str]) -> str:
        token = match.group()
        digits = re.match(r"[\d.,]+", token).group()  # type: ignore[union-attr]
        suffix = token[len(digits) :].strip().lower()
        spoken = expand_vietnamese_number(digits)
        if suffix in ("đ", "₫"):
            suffix = f" {denomination}"
        elif suffix == "k":
            suffix = " nghìn"
        elif suffix in ("đồng", "vnd", "vnđ"):
            # The literal currency word already follows the digits; keep it.
            suffix = f" {'VND' if suffix == 'vnd' else suffix}"
        return f"{spoken}{suffix}"

    stage_before_currency = text
    text = _GROUPED_PRICE_RE.sub(_grouped_expand, text)
    text = _COMPACT_CURRENCY_RE.sub(_expand_currency, text)
    # Track provenance against the text as it was BEFORE this stage.
    if text != stage_before_currency:
        applied.append("currency_and_price")

    stage_before_percent = text
    text = _PERCENT_RE.sub(_expand_percent, text)
    if text != stage_before_percent:
        applied.append("percent")

    stage_before_number = text
    text = _NUMBER_RE.sub(_expand_bare_number, text)
    if text != stage_before_number:
        applied.append("number_to_words")

    stage_before_acronym = text
    text = _ACRONYM_RE.sub(_spell_acronym, text)
    if text != stage_before_acronym:
        applied.append("acronym_spelling")

    # The acronym step may leave doubled spaces ("S K U  một"); tidy up
    # deterministically without re-adding normalizer ids for pure spacing.
    text = re.sub(r"\s+", " ", text).strip()
    return CompileResult(spoken_text=text, applied=tuple(applied))


class CompiledScriptVersion(BaseModel):
    """Compiled product script from an ordered list of segment versions (task 4.3).

    ``segment_version_ids`` is the exact ordered list of selected immutable
    segment version IDs. ``compiled_spoken_text`` joins the segments' spoken
    texts in that order with sentence punctuation between them — the exact
    artifact the Full Script Gate and the approval hash bind to (Decision 9/
    14). It is NOT recomputed from mutable source: it derives deterministically
    from the segment spoken texts at compile time and is immutable.
    """

    model_config = ConfigDict(extra="forbid")

    script_item_id: str = Field(min_length=1)
    segment_versions: list[dict] = Field(default_factory=list)
    segment_spoken_texts: list[str] = Field(default_factory=list)
    segment_version_ids: list[str] = Field(default_factory=list)
    plan_version: int = Field(default=1, ge=1)

    def compiled_spoken_text(self) -> str:
        """Join the segment spoken texts in exact order (task 4.3)."""
        return " ".join(
            seg.strip().rstrip(".!?…") + "."
            for seg in self.segment_spoken_texts
            if seg.strip()
        )
