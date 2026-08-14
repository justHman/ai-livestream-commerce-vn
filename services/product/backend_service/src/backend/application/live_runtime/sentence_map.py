"""Deterministic sentence-span derivation over the approved script (task 13.1).

A ``SentenceMap`` is a runtime DERIVATIVE of the immutable approved
``spoken_text`` (Change B artifact): the exact text is sliced into
contiguous sentences at the shared Change A sentence-terminator set
(``.``/``!``/``?``/``…``), with protected spans (URLs, emails,
currency/number runs, SKUs, abbreviations) never split inside. Span texts
are exact slices and concatenate back to ``spoken_text`` byte-for-byte
(the 13.2 proof), so the map never rewrites or rephrases the approved
artifact and never creates a new authoring version.

Punctuation policy is mirrored from
``backend.application.text_chunker.boundaries`` (Change A owns the shared
sentence-terminator set); protected-span semantics reuse the same ideas
(decimal points in prices, dots in URLs/emails, abbreviations) WITHOUT
importing any private name from that module. No phrase-sized chunking
happens here — this module never imports ``text_chunker``.
"""

from __future__ import annotations

import re as _re
from dataclasses import dataclass, field
from typing import Optional, Tuple

from backend.application.script_authoring.runtime_handoff import ResolvedApprovedScript

__all__ = [
    "SentenceMap",
    "SentenceSpan",
    "derive_sentence_map",
    "map_from_binding",
]

# Shared Change A sentence-terminator set (approved policy): a sentence ends
# at one of these; protected spans may contain them without splitting.
_SENTENCE_TERMINATORS = frozenset({".", "!", "?", "…"})

# Protected spans mirror the Change A boundary-protection semantics:
# -- URLs / emails (dots inside stay inside);
# -- alnum runs (with separators) ending in a currency/percent suffix
#    (``199.000đ``, ``50%``), so a decimal point in a price never splits;
# -- SKU-like tokens (alnum + ``-``/``_``/``/``/``#``), dotted acronyms
#    (``U.S.A.``) and common abbreviations (``vs.``), so their internal
#    dots stay inside the sentence.
_URL_RE = _re.compile(r"(?:https?://|www\.)[^\s<>]+")
_EMAIL_RE = _re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)
_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
_SKU_SEPARATORS = frozenset({"-", "_", "/", "#"})
_CURRENCY_SUFFIXES = frozenset({"đ", "₫", "%"})
_URL_EMAIL_TERMINALS = ".,!?;:"
_ABBREVIATIONS = frozenset({"mr.", "mrs.", "dr.", "st.", "vs.", "etc."})


@dataclass(frozen=True)
class SentenceSpan:
    """One exact sentence slice of the approved ``spoken_text``.

    ``start``/``end`` are exact slice offsets into the original artifact;
    ``text`` equals ``spoken_text[start:end]``. The trailing terminator is
    included, plus any whitespace run that follows it (so spans stay
    contiguous and ``concat()`` reproduces the artifact); a span never
    starts with whitespace. The text is a plain string — never coupled to
    ``TextChunk``.
    """

    index: int
    start: int
    end: int
    text: str


@dataclass(frozen=True, init=False)
class SentenceMap:
    """Immutable approved-script identity plus its derived sentence spans.

    Built once at bind from the exact approved text (13.1); the span
    sequence is the cursor's only movement domain (13.3). ``concat()``
    reproduces the approved artifact byte-for-byte (13.2).
    """

    script_set_id: str
    approved_version_id: str
    product_id: str
    spoken_text: str
    spans: Tuple[SentenceSpan, ...] = field(default_factory=tuple)

    def __init__(
        self,
        spoken_text: str,
        *,
        script_set_id: str = "",
        approved_version_id: str = "",
        product_id: str = "",
        spans: Optional[Tuple[SentenceSpan, ...]] = None,
    ) -> None:
        object.__setattr__(self, "spoken_text", spoken_text)
        object.__setattr__(self, "script_set_id", script_set_id)
        object.__setattr__(self, "approved_version_id", approved_version_id)
        object.__setattr__(self, "product_id", product_id)
        # The speaker tests construct SentenceMap(text) directly; the map is
        # still derived deterministically from the approved text.
        object.__setattr__(
            self, "spans", spans if spans is not None else _derive_spans(spoken_text)
        )

    @property
    def last_index(self) -> int:
        """Index of the final span, or -1 for an empty map."""
        return len(self.spans) - 1

    def __len__(self) -> int:
        return len(self.spans)

    def concat(self) -> str:
        """Concatenation of the exact span texts (the 13.2 proof)."""
        return "".join(span.text for span in self.spans)

    def sentence(self, index: int) -> Optional[SentenceSpan]:
        """Exact span at ``index``, or None when out of range."""
        if 0 <= index < len(self.spans):
            return self.spans[index]
        return None

    def next_after(self, index: int) -> Optional[SentenceSpan]:
        """Exact span immediately after ``index``, or None at the end."""
        return self.sentence(index + 1)


# -- protected spans (mirrors ``boundaries.protected_spans`` semantics) ------


def _trim_terminals(start: int, end: int, text: str) -> tuple[int, int]:
    """Cut trailing sentence punctuation off a URL/email span end.

    ``https://x.com/a.`` keeps ``/a`` protected and leaves the final ``.``
    outside, so a sentence ending right after a link stays a real boundary.
    """
    while end > start and text[end - 1] in _URL_EMAIL_TERMINALS:
        end -= 1
    return start, end


def _run_ranges(text: str, chars: frozenset[str]) -> list[tuple[int, int]]:
    """Maximal [start, end) runs of characters in ``chars`` (linear scan)."""
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index, char in enumerate(text):
        if char in chars:
            if run_start is None:
                run_start = index
        elif run_start is not None:
            runs.append((run_start, index))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(text)))
    return runs


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Union overlapping/adjacent spans, sorted ascending."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _is_number_token(token: str) -> bool:
    """True when ``token`` is digits with separators and an optional
    currency/percent suffix (``199.000đ``, ``50%``)."""
    return token.rstrip(".,!?;…").rstrip("đ₫%").replace(",", "").replace(".", "").isdigit()


def _is_acronym_token(token: str) -> bool:
    """True for dotted initials (``U.S.A.``) or 2+ uppercase letters
    (``AI``, ``TTS``); a trailing period is allowed (``OK.``)."""
    base = token[:-1] if token.endswith(".") else token
    if len(base) >= 2 and base.isupper() and base.isascii():
        return True
    parts = base.split(".")
    return len(parts) >= 2 and all(part.isupper() and part.isascii() for part in parts)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Protected [start, end) ranges inside which terminators never split."""
    spans: list[tuple[int, int]] = []
    for match in _URL_RE.finditer(text):
        spans.append(_trim_terminals(*match.span(), text))
    for match in _EMAIL_RE.finditer(text):
        spans.append(_trim_terminals(*match.span(), text))
    for start, end in _run_ranges(text, _ALNUM | _SKU_SEPARATORS):
        token = text[start:end]
        if (
            _is_number_token(token)
            or _is_acronym_token(token)
            or (
                any(ch.isdigit() for ch in token)
                and any(ch.isalpha() for ch in token)
                and any(ch in _SKU_SEPARATORS for ch in token)
            )
        ):
            spans.append((start, end))
    for start, end in _run_ranges(text, _ALNUM | _SKU_SEPARATORS | _CURRENCY_SUFFIXES | {"."}):
        token = text[start:end]
        if _is_number_token(token):
            spans.append((start, end))
    for start, end in _run_ranges(text, _ALNUM | _SKU_SEPARATORS | {"."}):
        token = text[start:end]
        if token.lower() in _ABBREVIATIONS or _is_acronym_token(token):
            spans.append((start, end))
    return _merge_spans(spans)


def _in_protected(spans: list[tuple[int, int]], position: int) -> bool:
    """True when ``position`` (exclusive slice end) cuts INSIDE a span.

    A cut exactly at a span end is a boundary AFTER the protected token
    (``OK.`` end, ``199.000đ`` end) and stays eligible.
    """
    for start, end in spans:
        if position <= start:
            return False
        if start < position < end:
            return True
    return False


# -- deterministic sentence derivation ----------------------------------------


def _sentence_ends(text: str) -> list[int]:
    """Exclusive ends of every terminator in ``text`` (shared Change A set)."""
    return [index + 1 for index, char in enumerate(text) if char in _SENTENCE_TERMINATORS]


def _derive_spans(spoken_text: str) -> Tuple[SentenceSpan, ...]:
    """Deterministic exact sentence slices of ``spoken_text``.

    Each sentence ends at the first shared terminator (``.``/``!``/``?``/
    ``…``) not inside a protected span; the terminator is included in its
    sentence, as is the whitespace run that follows it (leading whitespace
    of a sentence is never included — the previous span consumed it). A
    protected span whose own tail is terminators (``1...``) ends exactly at
    the span end, the same boundary Change A leaves eligible. Empty
    sentences are never emitted. Concatenating the span texts reproduces
    ``spoken_text`` byte-for-byte.
    """
    spans = _protected_spans(spoken_text)

    ends: set[int] = set()
    for end in _sentence_ends(spoken_text):
        if not _in_protected(spans, end):
            ends.add(end)
    for start, end in spans:
        if end > start and spoken_text[end - 1] in _SENTENCE_TERMINATORS:
            ends.add(end)

    ordered = sorted(ends)
    if ordered:
        ordered[-1] = len(spoken_text)

    span_list: list[SentenceSpan] = []
    start = 0
    for index, end in enumerate(ordered):
        while start < len(spoken_text) and spoken_text[start].isspace():
            start += 1
        if start >= end:
            # Duplicate/overlapping boundary inside already-consumed text:
            # position never moves backward.
            continue
        span_end = end
        while span_end < len(spoken_text) and spoken_text[span_end].isspace():
            span_end += 1
        span_list.append(
            SentenceSpan(index=index, start=start, end=span_end, text=spoken_text[start:span_end])
        )
        start = span_end

    if not span_list and spoken_text.strip():
        stripped = spoken_text.strip()
        span_list.append(
            SentenceSpan(
                index=0,
                start=spoken_text.index(stripped[0]),
                end=len(spoken_text),
                text=stripped,
            )
        )

    return tuple(span_list)


def derive_sentence_map(
    spoken_text: str,
    *,
    script_set_id: str = "",
    approved_version_id: str = "",
    product_id: str = "",
) -> SentenceMap:
    """Derive the deterministic sentence map from the exact approved text.

    Deterministic exact sentence slices (see ``_derive_spans``); the map
    carries the approved-script identity (task 13.1).
    """
    return SentenceMap(
        spoken_text,
        script_set_id=script_set_id,
        approved_version_id=approved_version_id,
        product_id=product_id,
    )


def map_from_binding(script_set_id: str, script: ResolvedApprovedScript) -> SentenceMap:
    """Convenience wrapper deriving the map from a resolved approved script."""
    return derive_sentence_map(
        script.spoken_text,
        script_set_id=script_set_id,
        approved_version_id=script.approved_version_id,
        product_id=script.product_id,
    )
