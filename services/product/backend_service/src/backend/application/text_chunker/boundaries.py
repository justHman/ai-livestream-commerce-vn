"""Deterministic boundary-candidate extraction for speech text chunking.

Pure functions over original text spans (OpenSpec adaptive-speech-text-chunking
task 3.1/3.2). Every candidate's ``end`` is an exact slice end offset of the
original string; the extracted text is never rewritten. Protected spans
(decimal/grouped numbers, currency/percent, URLs/emails, acronyms/
abbreviations, SKU-like tokens, balanced quotes/parentheses) mark the
punctuation inside them as unsafe, so the future scorer (task 3.6) can
penalize or exclude those candidates.

The scorer seam is ``Candidate.kind`` and ``Candidate.protected``:
``kind`` is the evidence class (scorer weighs it), ``protected`` is the
safety flag (scorer hard-excludes unless nothing safer exists). Vietnamese
cue words are only features on WHITESPACE candidates, never decisions
(Decision 4 in design.md).
"""

from __future__ import annotations

import re as _re
from dataclasses import dataclass
from enum import IntEnum

__all__ = ["CandidateKind", "BoundaryCandidate", "extract_candidates", "protected_spans"]


class CandidateKind(IntEnum):
    """Boundary evidence class, strongest to weakest (Decision 4 order).

    ``VIETNAMESE_CUE`` ranks above plain whitespace but stays below the
    punctuation classes; cue words are features, never unconditional split
    commands.
    """

    PARAGRAPH = 1
    SENTENCE = 2
    CLAUSE = 3
    COMMA = 4
    VIETNAMESE_CUE = 5
    WHITESPACE = 6
    HARD_CAP = 7


@dataclass(frozen=True)
class BoundaryCandidate:
    """One candidate split.

    ``end`` is the exclusive slice offset of the ORIGINAL string, so
    ``text[:end]`` / ``text[end:]`` are the exact head/tail. ``strength`` is
    the deterministic kind rank; ``protected`` marks punctuation inside a
    protected span (scorer should exclude unless no safer candidate exists).
    """

    kind: CandidateKind
    end: int
    protected: bool
    hard_cap: bool = False

    @property
    def strength(self) -> int:
        """Deterministic split strength: lower is stronger.

        Ranks 1 (paragraph) through 7 (hard cap) in the same order the
        scorer weighs kinds; usable directly as a numeric weight.
        """
        return int(self.kind)


# -- protected-span punctuation --------------------------------------------

# Punctuation that can be a sentence/clause boundary OUTSIDE a protected span.
_SENTENCE_TERMINATORS = frozenset({".", "!", "?", "…"})
_CLAUSE_PUNCTUATION = frozenset({";", ":"})
_COMMA_PUNCTUATION = frozenset({","})
_ALL_BOUNDARY_PUNCTUATION = frozenset({".", "!", "?", "…", ";", ":", ","})

# Chars whose presence inside a run marks it protected (unit tests rely on
# these exact memberships).
_SKU_EXTRA_CHARS = frozenset({"-", "_", "/", "#"})
_ABBREV_EXTRA_CHARS = frozenset({"-", "."})
# Currency/percent suffixes that stick to digit runs (Vietnamese "đ").
_CURRENCY_SUFFIX_CHARS = frozenset({"đ", "₫", "%"})

_URL_RE = _re.compile(r"(?:https?://|www\.)[^\s<>]+")
# The local part is bounded (RFC 5321, 64 octets) and anchored with a
# lookbehind so a long letter run can never trigger catastrophic
# backtracking: the engine only tries positions preceded by a non-local
# char, and each attempt is bounded by the 64-char cap.
_EMAIL_RE = _re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)
# Terminal punctuation that ends a sentence AFTER a URL/email span; never
# part of the protected span itself. Paired closing delimiters (paren,
# quotes) are trimmed only when the URL does not itself contain the matching
# opener — see ``_trim_terminals``.
_URL_EMAIL_TERMINALS = ".,!?;:"
_URL_EMAIL_CLOSERS = {"(": ")", '"': '"', "'": "'", "“": "”", "‘": "’"}


# Vietnamese clause cues (lowercase). Features for whitespace candidates
# only; never a split decision by themselves.
_VIETNAMESE_CUES = frozenset(
    {
        "và",
        "nhưng",
        "như",
        "rằng",
        "nên",
        "còn",
        "để",
        "bởi",
        "vì",
        "tuy",
        "dù",
        "khi",
        "nếu",
        "hay",
        "hoặc",
        "mà",
        "là",
        "sau",
        "trước",
        "tại",
        "với",
    }
)
# Anchored lookbehind: the pre-cue word must start after whitespace, so a
# single long non-space run (e.g. nested parens) never triggers a quadratic
# match attempt from every position (each position fails the lookbehind in
# O(1), and the greedy ``\\S{3,}`` never spans whitespace).
# Cue word captured as group 1 so ``_vi_cue_ends`` keeps returning the
# whitespace offset before the cue (the split point).
_CUE_PATTERN = _re.compile(
    r"(?<=\s)(?:\S{3,})\s+(" + "|".join(sorted(_VIETNAMESE_CUES)) + r")(?!\S)"
)


# -- protected-span detection -----------------------------------------------


def _covers(start: int, end: int, span: tuple[int, int]) -> bool:
    return span[0] < end and start < span[1]


def _run_char_ranges(text: str, start: int, end: int, predicate) -> list[tuple[int, int]]:
    """[start, end) split into maximal runs of chars satisfying ``predicate``."""
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index in range(start, end):
        if predicate(text[index]):
            if run_start is None:
                run_start = index
        elif run_start is not None:
            runs.append((run_start, index))
            run_start = None
    if run_start is not None:
        runs.append((run_start, end))
    return runs


def _token_span_is_number(token: str) -> bool:
    """True when the whole token is digits with optional separators and a
    trailing currency/percent suffix (e.g. ``199.000đ``, ``50%``)."""
    cleaned = token.rstrip(".,!?;:…").rstrip("đ₫%").replace(",", "").replace(".", "")
    return cleaned.isdigit()


def _token_span_is_sku(token: str) -> bool:
    """True for SKU/product-code tokens: alphanumeric with a separator.

    Requires at least one letter AND one digit around a separator
    (``-``/``_``/``/``/``#``), case-insensitive, so ``SKU-P004``,
    ``sku-p004``, ``áo-01`` are protected but plain words (``mua``),
    natural slash forms (``a/b``, ``hôm/nay``) and bare digit runs are not.
    """
    if not any(ch.isdigit() for ch in token) or not any(ch.isalpha() for ch in token):
        return False
    return any(ch in _SKU_EXTRA_CHARS for ch in token)


def _token_span_is_acronym(token: str) -> bool:
    """True for dotted initials (U.S.A.) or 2+ uppercase letters (AI, TTS).

    A trailing period (``OK.``) is allowed so the whole run protects the
    acronym's dot too.
    """
    base = token[:-1] if token.endswith(".") else token
    if len(base) >= 2 and base.isupper() and base.isascii():
        return True
    parts = base.split(".")
    return len(parts) >= 2 and all(part.isupper() and part.isascii() for part in parts)


def _token_span_is_abbreviation(token: str) -> bool:
    """True for common lowercase abbreviations ending with a period."""
    return token.lower() in {"mr.", "mrs.", "dr.", "st.", "vs.", "etc.", "e.g.", "i.e."}


def _protect_balances(
    spans: list[tuple[int, int]], text: str, pairs: str, other: str
) -> list[tuple[int, int]]:
    """Protect balanced delimiter regions via a linear pair stack.

    ``pairs`` maps an opening char to its closing char (``()``, ``""``,
    ``''``, curly quotes). Each stack entry is ``(opening_index, closer)``;
    a closing char matches ONLY the closer expected by the current top, so
    mixed/nested delimiters resolve in proper LIFO order (a quote inside
    parentheses closes before the parenthesis). Same-char pairs (quotes)
    toggle: a quote that matches the top closes it, otherwise it pushes a
    new opener. A mismatched closer is ignored, so it never blocks an outer
    pair from closing. A region [open, close] whose interior contains any
    boundary punctuation (``other``), any whitespace, or touches an existing
    span becomes a protected span — whitespace counts so word splits inside
    a quoted/parenthesized region are flagged too, not only punctuation.
    Unbalanced delimiters protect nothing, so punctuation after a stray
    opener stays eligible. ``other`` is every boundary-punctuation
    character (e.g. ``.,!?;,``).
    """
    opens = {pairs[i]: pairs[i + 1] for i in range(0, len(pairs), 2)}
    stack: list[tuple[int, str]] = []
    # Prefix count of breakable chars (boundary punctuation/whitespace) so
    # the interior scan of a closed region is O(1): has_breakable(i, j) ==
    # prefix[j] > prefix[i]. A per-close interior scan would be O(n^2) on
    # deeply nested delimiters.
    prefix = [0] * (len(text) + 1)
    for index, char in enumerate(text):
        prefix[index + 1] = prefix[index] + (1 if (char in other or char.isspace()) else 0)
    for index, char in enumerate(text):
        expected = opens.get(char)
        # Closing char: pop only when it matches the current top's expected
        # closer; a stray/mismatched closer is ignored. Same-char pairs
        # (quotes) fall through to the opener branch when they do not close
        # the top, so they toggle correctly.
        if char in opens.values() and stack and stack[-1][1] == char:
            opening = stack.pop()[0]
            region = (opening, index + 1)
            has_breakable = prefix[index] > prefix[opening + 1]
            touches_span = any(_covers(start, end, region) for start, end in spans)
            if has_breakable or touches_span:
                spans.append(region)
        elif expected is not None:
            stack.append((index, expected))
    return spans


def _protect_special_ranges(
    text: str, spans: list[tuple[int, int]], extra: str
) -> list[tuple[int, int]]:
    """Extend spans across ASCII words containing ``extra`` chars.

    A span touching a digit/letter run containing an SKU/abbreviation
    separator swallows the whole run: ``199.000đ``, ``SKU-P004``, ``U.S.A.``.
    Each original span is extended in place; unions between spans are merged
    by the caller.

    Word runs are found with a linear scanner instead of a ``(?:X+)+``
    regex, which catastrophically backtracks on long letter/digit runs.
    """
    alnum = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    sep_chars = frozenset(extra)
    words: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] not in alnum and text[i] not in sep_chars:
            i += 1
            continue
        j = i
        saw_sep = False
        while j < n and (text[j] in alnum or text[j] in sep_chars):
            saw_sep = saw_sep or text[j] in sep_chars
            j += 1
        # Require at least one separator so plain words never register.
        if saw_sep:
            words.append((i, j))
        i = j
    extended: list[tuple[int, int]] = []
    for start, end in spans:
        changed = True
        while changed:
            changed = False
            for word_start, word_end in words:
                # Only extend when the word actually reaches beyond the
                # current span; touching words fully inside the span must
                # not re-trigger the loop.
                if start < word_end and word_start < end and (word_start < start or word_end > end):
                    start, end = min(start, word_start), max(end, word_end)
                    changed = True
        extended.append((start, end))
    return extended


def _trim_terminals(span: tuple[int, int], text: str) -> tuple[int, int]:
    """Strip terminal punctuation from a URL/email span end.

    ``https://x.com/a.`` keeps ``/a`` protected but leaves the final ``.``
    outside, so a sentence ending right after a link stays a real boundary.
    A paired closing delimiter (``)``, quote) right after the URL is external
    only when the URL itself never opened that pair — e.g. ``Xem
    (https://example.com).`` keeps ``https://example.com`` protected and
    leaves ``)`` and ``.`` outside, while a URL containing ``(`` (wikipedia)
    keeps its own ``)``.
    """
    start, end = span
    url = text[start:end]
    while end > start and text[end - 1] in _URL_EMAIL_TERMINALS:
        end -= 1
    while end > start:
        closer = text[end - 1]
        opener = next((o for o, c in _URL_EMAIL_CLOSERS.items() if c == closer), None)
        if opener is None or opener in url:
            break
        end -= 1
        url = text[start:end]
    return start, end


def protected_spans(text: str) -> list[tuple[int, int]]:
    """Protected spans in ``text``: [start, end) pairs, non-overlapping.

    Protected spans include decimal/grouped numbers, currency/percent forms,
    URLs/emails, acronyms/abbreviations, SKU-like tokens, and balanced
    quote/paren regions. A span marks punctuation INSIDE it as unsafe.
    """
    spans: list[tuple[int, int]] = []

    for match in _URL_RE.finditer(text):
        spans.append(_trim_terminals(match.span(), text))
    for match in _EMAIL_RE.finditer(text):
        spans.append(_trim_terminals(match.span(), text))
    for run_start, run_end in _run_char_ranges(
        text, 0, len(text), lambda c: c.isalnum() or c in _SKU_EXTRA_CHARS
    ):
        token = text[run_start:run_end]
        if (
            _token_span_is_number(token)
            or _token_span_is_acronym(token)
            or _token_span_is_sku(token)
        ):
            spans.append((run_start, run_end))
    for run_start, run_end in _run_char_ranges(
        text, 0, len(text), lambda c: c.isalnum() or c in _SKU_EXTRA_CHARS or c == "."
    ):
        token = text[run_start:run_end]
        if _token_span_is_acronym(token):
            spans.append((run_start, run_end))
    for run_start, run_end in _run_char_ranges(
        text,
        0,
        len(text),
        lambda c: c.isalnum() or c in _SKU_EXTRA_CHARS or c in _CURRENCY_SUFFIX_CHARS or c == ".",
    ):
        token = text[run_start:run_end]
        if _token_span_is_number(token):
            spans.append((run_start, run_end))
    for run_start, run_end in _run_char_ranges(
        text, 0, len(text), lambda c: c.isalnum() or c in _ABBREV_EXTRA_CHARS
    ):
        token = text[run_start:run_end]
        if _token_span_is_abbreviation(token):
            spans.append((run_start, run_end))

    spans = _protect_special_ranges(text, spans, _SKU_EXTRA_CHARS)
    spans = _protect_balances(spans, text, "()\"\"''“”‘’", "".join(_ALL_BOUNDARY_PUNCTUATION))

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _in_protected(spans: list[tuple[int, int]], position: int) -> bool:
    """True when ``position`` (an exclusive slice end) cuts inside a span.

    Strictly inside: a split exactly at a span's end is a boundary AFTER the
    protected token (``OK.`` end, ``199.000đ`` end), which stays eligible —
    only splits cutting INSIDE the token are unsafe.
    """
    for start, end in spans:
        if position <= start:
            return False
        if start < position < end:
            return True
    return False


# -- candidate extraction ----------------------------------------------------


def _paragraph_ends(text: str) -> list[int]:
    """Line ends (``\n``, ``\r\n``, ``\r``) — paragraph candidates.

    Each line break yields exactly one candidate at the end of its newline;
    ``\r\n`` is consumed by the ``\n`` branch so it never duplicates.
    """
    ends: list[int] = []
    for index, char in enumerate(text):
        if char == "\n":
            ends.append(index + 1)
        elif char == "\r":
            if index + 1 >= len(text) or text[index + 1] != "\n":
                ends.append(index + 1)
    return ends


def _punctuation_ends(text: str, chars: frozenset[str]) -> list[int]:
    return [index + 1 for index, char in enumerate(text) if char in chars]


def _vi_cue_ends(text: str) -> list[int]:
    """Whitespace offsets immediately before a Vietnamese clause cue.

    The cue is a feature, not a decision: it only upgrades a whitespace
    candidate's kind, and only when the word before it is speakable
    (3+ alphanumeric chars).
    """
    return [match.start(1) for match in _CUE_PATTERN.finditer(text)]


def _whitespace_ends(text: str) -> list[int]:
    return [index + 1 for index, char in enumerate(text) if char.isspace()]


def _hard_cap_end(text: str, max_chars: int) -> int | None:
    """Forced split position at or before ``max_chars``, or None if the
    whole text already fits under the cap.

    Prefers the last whitespace at or before the cap (word boundary; the
    whitespace stays in the head so exact slicing holds); otherwise cuts
    exactly at the cap. The forced split stays representable: the head may
    split a protected span only when no safe position exists at all.
    """
    if len(text) <= max_chars:
        return None
    for split_at in range(max_chars, 0, -1):
        if text[split_at - 1].isspace():
            return split_at
    return max_chars


def extract_candidates(text: str, max_chars: int) -> list[BoundaryCandidate]:
    """All candidate splits for ``text`` under hard cap ``max_chars``.

    Sorted ascending by ``end``; each candidate's ``end`` is an exact slice
    offset of the original string. EVERY candidate (punctuation, whitespace,
    cue, paragraph) cutting inside a protected span is flagged
    ``protected=True`` (scorer excludes unless nothing safer exists); the
    hard-cap forced split is always present and marked ``hard_cap=True`` even
    when a stronger kind shares its offset.
    """
    if not text:
        return []
    if max_chars <= 0:
        raise ValueError(f"max_chars must be > 0, got {max_chars}")

    spans = protected_spans(text)

    candidates: list[BoundaryCandidate] = []
    for end in _paragraph_ends(text):
        candidates.append(
            BoundaryCandidate(CandidateKind.PARAGRAPH, end, _in_protected(spans, end))
        )
    for end in _punctuation_ends(text, _SENTENCE_TERMINATORS):
        candidates.append(BoundaryCandidate(CandidateKind.SENTENCE, end, _in_protected(spans, end)))
    for end in _punctuation_ends(text, _CLAUSE_PUNCTUATION):
        candidates.append(BoundaryCandidate(CandidateKind.CLAUSE, end, _in_protected(spans, end)))
    for end in _punctuation_ends(text, _COMMA_PUNCTUATION):
        candidates.append(BoundaryCandidate(CandidateKind.COMMA, end, _in_protected(spans, end)))
    for end in _vi_cue_ends(text):
        candidates.append(
            BoundaryCandidate(CandidateKind.VIETNAMESE_CUE, end, _in_protected(spans, end))
        )
    for end in _whitespace_ends(text):
        candidates.append(
            BoundaryCandidate(CandidateKind.WHITESPACE, end, _in_protected(spans, end))
        )

    cap = _hard_cap_end(text, max_chars)
    if cap is not None:
        candidates.append(
            BoundaryCandidate(CandidateKind.HARD_CAP, cap, _in_protected(spans, cap), True)
        )

    seen: set[int] = set()
    deduped: list[BoundaryCandidate] = []
    # Strongest kind wins when several candidates share an end: ascending
    # (end, kind) order keeps the FIRST (lowest kind = strongest) one; the
    # hard-cap flag survives the merge so forced status is representable even
    # when the cap offset coincides with a stronger candidate.
    for candidate in sorted(candidates, key=lambda c: (c.end, c.kind)):
        if candidate.end in seen:
            deduped[-1] = BoundaryCandidate(
                deduped[-1].kind,
                deduped[-1].end,
                deduped[-1].protected or candidate.protected,
                deduped[-1].hard_cap or candidate.hard_cap,
            )
            continue
        seen.add(candidate.end)
        deduped.append(candidate)
    return deduped
