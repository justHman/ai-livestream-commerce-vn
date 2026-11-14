"""Pure offline script-quality metrics for live-selling scripts (task B0).

This module is the analysis core behind ``script_quality_qa.py``. It is:

- **Pure / offline**: no network, no LLM, no imports from ``services/``. The few
  Runtime constants it mirrors are re-declared here with their sources quoted
  in comments, so CI can import it without the backend service installed.
- **Heuristic**: every detector below is a deterministic approximation, not a
  semantic judge. See ``scripts/qa/README.md`` for the documented limitations.

Mirrored constants (do NOT import services code — copy + cite instead):

- ``VOICE_CHARS_PER_S = 16`` — source:
  ``services/product/backend_service/src/backend/application/script_authoring/
  generation/ordered_units.py:54`` (``_VOICE_CHARS_PER_S = 16``; measured live
  2026-10-15: 3672 characters spoken in 226 s ≈ 16 chars/second). The Runtime
  prompt and length guards budget ``seconds * 16`` chars, so the QA estimates
  spoken duration as ``chars / 16``.
- Duration floor/ceiling defaults ``180``/``240`` s — TASK_B_SCRIPT_QUALITY.md
  §6/§7/§8 (the per-product floor is a policy question; ``product_target_s`` in
  ``ordered_units.py`` clamps the requested policy value). The QA treats
  floor/ceiling as analysis parameters so different policies can be measured.
- Interaction cadence target ``90`` s — TASK_B_SCRIPT_QUALITY.md §6
  ("khoảng cách giữa các lần tương tác (mục tiêu ≤ 90 s)") and §7 item 6.

Input shapes:

- ``sections``: list of dicts ``{"text": str, "role": str (optional)}`` (plain
  strings are accepted too). ``role``/``kind`` is used to spot recap sections.
- ``claims``: list of approved claim sentences (strings).
- ``product``: optional dict; ``target_floor_s`` / ``target_ceiling_s`` may
  override the defaults per document.
"""

from __future__ import annotations

import re
import statistics
import unicodedata

__all__ = [
    "VOICE_CHARS_PER_S",
    "PRODUCT_FLOOR_DEFAULT_S",
    "PRODUCT_CEILING_DEFAULT_S",
    "INTERACTION_TARGET_S",
    "DURATION_MIN_FRACTION_OF_FLOOR",
    "DEFAULT_BUZZWORDS",
    "RECAP_ROLE_MARKERS",
    "INTERACTION_KEYWORDS",
    "fold_text",
    "split_sentences",
    "written_numbers",
    "extract_numbers",
    "analyze_script",
]

# ── Mirrored Runtime constants (see module docstring for sources) ────────────

VOICE_CHARS_PER_S = 16
PRODUCT_FLOOR_DEFAULT_S = 180.0
PRODUCT_CEILING_DEFAULT_S = 240.0
INTERACTION_TARGET_S = 90.0
DURATION_MIN_FRACTION_OF_FLOOR = 0.7

# Praising/hard-sell phrases to flag when NOT covered by an approved claim.
# First 7 are TASK_B_SCRIPT_QUALITY.md §1/§4 examples; the rest are the
# hard-sell vocabulary the Runtime gate already rejects (same file §1).
DEFAULT_BUZZWORDS: tuple[str, ...] = (
    "hot item",
    "đẳng cấp",
    "lựa chọn lý tưởng",
    "thương hiệu uy tín",
    "an tâm",
    "hợp lý",
    "ấm áp",
    "mua ngay",
    "đặt ngay",
    "chốt đơn ngay",
    "nhanh tay",
    "không bỏ lỡ",
    "đừng bỏ lỡ",
    "số lượng có hạn",
    "chỉ hôm nay",
)

# A section whose role/kind contains one of these markers (folded) is treated
# as a recap/revisit section for the verbatim-repetition check.
RECAP_ROLE_MARKERS: tuple[str, ...] = (
    "recap",
    "revisit",
    "summary",
    "tom tat",
    "nhac lai",
)

# A sentence counts as an interaction when it contains "?" or one of these
# (folded) substrings. Sources: TASK_B §6 flatness definition.
INTERACTION_KEYWORDS: tuple[str, ...] = (
    "binh luan",
    "ban nao",
    "chot ",
    "comment",
)

# ── Text folding (diacritic-insensitive matching) ────────────────────────────

_D_TO_D = str.maketrans({"đ": "d", "Đ": "d"})


def fold_text(text: str) -> str:
    """Fold Vietnamese text for matching: strip diacritics, đ→d, lowercase,
    collapse whitespace. ``"Đẳng Cấp"`` and ``"dang cap"`` fold identically."""
    decomposed = unicodedata.normalize("NFD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", stripped.translate(_D_TO_D).strip().lower())


# ── Sentence splitting ───────────────────────────────────────────────────────

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def split_sentences(text: str) -> list[str]:
    """Split one section's text into sentences on ``.!?…`` followed by space,
    or newlines. Keeps punctuation; drops empties."""
    sentences: list[str] = []
    for block in re.split(r"\n+", text):
        for candidate in _SENTENCE_SPLIT_RE.split(block):
            candidate = candidate.strip()
            if candidate:
                sentences.append(candidate)
    return sentences


# ── Vietnamese written-number normalization ──────────────────────────────────
# Goal (TASK_B §4): catch numbers spoken in words — e.g. ``0,84 kg`` spoken as
# "tám trăm bốn mươi kg" — by normalizing them to digits, then checking them
# against the approved claims like any digit number.

_DIGIT_WORDS: dict[str, int] = {
    "khong": 0,
    "mot": 1,
    "hai": 2,
    "ba": 3,
    "bon": 4,
    "tu": 4,  # canonical key for the surface "tư"; the preposition "từ" never maps here
    "nam": 5,  # canonical key for the surface "năm"; "nam" unmarked is matched as a unit
    "lam": 5,  # canonical key for the surface "lăm"; the verb "làm" never maps here
    "sau": 6,
    "bay": 7,
    "tam": 8,
    "chin": 9,
    "muoi": 10,  # "mười"; as tens suffix it is handled contextually below
}
# Close a <1000 group and multiply. "trăm" stays inside a group; nghìn/triệu/tỷ
# close the accumulated group.
_MAGNITUDE: dict[str, int] = {"tram": 100, "nghin": 1_000, "trieu": 1_000_000}
_MAG_BILLION = "ty"
_TENS_SUFFIX = "muoi"  # "mươi" — canonical key shared with the standalone "mười"
_GAP_MARKERS = frozenset({"linh", "le"})  # "linh"/"lẻ": next digit is a unit
_DECIMAL_MARK = "phay"  # "phẩy"
_UNIT_MARKER_YEAR = "nam"  # trailing "năm" after a plain digit run = years
_UNIT_MARKER_PERCENT = "%"

_NUMBER_VOCAB = set(_DIGIT_WORDS) | set(_MAGNITUDE) | {_MAG_BILLION, _TENS_SUFFIX} | _GAP_MARKERS
_NUMBER_VOCAB.add(_DECIMAL_MARK)
_NUMBER_VOCAB.add(_UNIT_MARKER_YEAR)
_NUMBER_VOCAB.add("phan")  # only meaningful in the pair "phần trăm" → %

# Surface form (NFC, lowercased) → canonical number-word key. Matching is
# diacritic-EXACT on purpose (the real-baseline fix): "bảy" (7) matches while
# "bây" (as in "Bây giờ") and the verb "bay" do not, "lăm" (5) matches while
# "làm" does not, "tư" (4) matches while the preposition "từ" does not. Tone
# and vowel marks carry meaning for number words; only unambiguous function
# words (units after a number) stay diacritic-insensitive via ``fold_text``.
_SURFACE_TO_CANON: dict[str, str] = {
    "không": "khong",
    "một": "mot",
    "mốt": "mot",
    "hai": "hai",
    "ba": "ba",
    "bốn": "bon",
    "tư": "tu",
    "năm": "nam",
    "lăm": "lam",
    "sáu": "sau",
    "bảy": "bay",
    "tám": "tam",
    "chín": "chin",
    "mười": "muoi",
    "mươi": "muoi",
    "trăm": "tram",
    "nghìn": "nghin",
    "ngàn": "nghin",
    "triệu": "trieu",
    "tỷ": "ty",
    "tỉ": "ty",
    "linh": "linh",
    "lẻ": "le",
    "phẩy": "phay",
    "phần": "phan",
}
# Spoken low–high range markers: "một sáu lăm đến một bảy tư" = 1m65–1m74.
_RANGE_MARKERS = frozenset({"đến", "tới"})
# Span tokens that make the reading unambiguous (grammar/magnitude words): a
# mismatch there is a hard "error". A bare digit-by-digit run ("một sáu tám
# năm hai") has an ambiguous segmentation, so its mismatch is "uncertain".
_DEFINITE_SPAN_TOKENS = (
    frozenset(_MAGNITUDE)
    | {_MAG_BILLION, _TENS_SUFFIX, _DECIMAL_MARK, _UNIT_MARKER_PERCENT}
    | _GAP_MARKERS
)

# Units recognized after a number (folded). Wide on purpose: unknown units just
# degrade to bare-value matching against the claims.
_UNIT_VOCAB = frozenset(
    {
        "kg",
        "kgs",
        "g",
        "gr",
        "gram",
        "gam",
        "l",
        "lit",
        "ml",
        "dl",
        "d",
        "dong",
        "vnd",
        "%",
        "oc",
        "c",
        "w",
        "kw",
        "watt",
        "thang",
        "nam",
        "ngay",
        "tuan",
        "phut",
        "giay",
        "gio",
        "nguoi",
        "mieng",
        "mon",
        "lan",
        "mau",
        "cai",
        "bo",
        "hop",
        "chiec",
        "tui",
        "trang",
        "giot",
        "quyen",
        "cm",
        "mm",
        "m",
        "km",
    }
)
# Written-form numbers are only *flagged* when the unit is a real measurement
# (or the span is multi-token / value ≥ 100) — see ``_written_should_check``.
_STRICT_UNITS = frozenset(
    {
        "kg",
        "kgs",
        "g",
        "gr",
        "gram",
        "gam",
        "l",
        "lit",
        "ml",
        "dl",
        "d",
        "dong",
        "vnd",
        "%",
        "oc",
        "w",
        "kw",
        "thang",
        "ngay",
        "tuan",
        "phut",
        "giay",
        "gio",
        "nam",
        "cm",
        "mm",
        "m",
        "km",
    }
)

_TOKEN_STRIP = ".,!?…;:()\"'“”‘’«»"

# One alternation keeps digit-form matches non-overlapping; order matters
# (thousands "1.390.000" before plain "1", sizes "1m55" before plain).
_NUM_RE = re.compile(
    r"(?P<thousands>\d{1,3}(?:\.\d{3})+)"
    r"|(?P<meters>\d+m\d{2})"
    r"|(?P<decimal>\d+,\d+)"
    r"|(?P<plain>\d+)"
)
_UNIT_TAIL_RE = re.compile(r"[ \u00a0]*([°%A-Za-zĐđ]{1,5})")


def _number_tokens(text: str) -> list[str]:
    """Whitespace tokens with edge punctuation stripped; '/' acts as a space."""
    tokens = []
    for raw in text.replace("/", " ").split():
        token = raw.strip(_TOKEN_STRIP)
        if token:
            tokens.append(token)
    return tokens


def _fold_tokens(tokens: list[str]) -> list[str]:
    return [fold_text(t) for t in tokens]


def _surface_to_canon(token: str) -> str | None:
    """Canonical number-word key for one surface token, or ``None``.

    Diacritic-exact on purpose — see ``_SURFACE_TO_CANON``. ``"Bây"`` in
    "Bây giờ" is NOT read as the digit 7; "từ"/"làm" are never digits."""
    return _SURFACE_TO_CANON.get(unicodedata.normalize("NFC", token.lower()))


def _plain_digit_run(tokens: list[str]) -> bool:
    return all(t in _DIGIT_WORDS and _DIGIT_WORDS[t] <= 9 for t in tokens) and bool(tokens)


def _parse_number_tokens(tokens: list[str]) -> str | None:
    """Parse folded number-word tokens into a canonical value string.

    Grammar (documented in README): ``[N triệu] [N nghìn] [N trăm] [N mươi] [N]``
    with "linh/lẻ" tens-gap markers, "phẩy" decimals and "phần trăm" percents.
    Returns ``None`` when the span does not parse (caller then ignores it).
    """
    if not tokens:
        return None
    if tokens[-1] == _UNIT_MARKER_PERCENT:
        head = _parse_number_tokens(tokens[:-1])
        return head  # caller strips the % marker and uses it as the unit
    if _DECIMAL_MARK in tokens:
        idx = tokens.index(_DECIMAL_MARK)
        head_tokens, dec_tokens = tokens[:idx], tokens[idx + 1 :]
        if not dec_tokens or any(
            t not in _DIGIT_WORDS or _DIGIT_WORDS[t] >= 10 for t in dec_tokens
        ):
            return None
        head = _parse_number_tokens(head_tokens) if head_tokens else "0"
        if head is None or "," in head:
            return None
        return f"{head},{('').join(str(_DIGIT_WORDS[t]) for t in dec_tokens)}"

    if _plain_digit_run(tokens):
        # Digit-by-digit reading: every word is one digit ("một sáu tám" → 168,
        # "năm hai" → 52). This replaces the old positional grammar, which
        # mis-merged "một sáu tám năm hai" (1m68, 52) into 31.
        return "".join(str(_DIGIT_WORDS[t]) for t in tokens)

    total = 0
    group = 0
    pending: int | None = None
    slot = "hundreds"  # where the next plain digit lands: hundreds|tens|units
    for token in tokens:
        if token in _MAGNITUDE:
            if token == "tram":
                group += (pending if pending is not None else 1) * 100
                pending = None
                slot = "tens"
            else:  # nghìn/ngàn/triệu close the group
                total += (group + (pending if pending is not None else 0)) * _MAGNITUDE[token]
                group, pending, slot = 0, None, "hundreds"
        elif token == _MAG_BILLION:
            total = (total + group + (pending if pending is not None else 0)) * 1_000_000_000
            group, pending, slot = 0, None, "hundreds"
        elif token == _TENS_SUFFIX:
            if pending is not None and pending != 0:
                group += pending * 10  # "X mươi"
            else:
                group += 10  # standalone "mười"
            pending, slot = None, "units"
        elif token in _GAP_MARKERS:
            pending, slot = None, "units"
        elif token in _DIGIT_WORDS:
            value = _DIGIT_WORDS[token]
            if slot == "units":
                group += value
            elif pending is not None:
                # Two bare digits in a row ("năm ba" → 53); the first was a tens digit.
                group += pending * 10 + value
                pending, slot = None, "units"
            else:
                pending = value
        else:
            return None  # not a number token — caller only feeds vocab tokens
    value = total + group + (pending if pending is not None else 0)
    # Zero is legal here ("không" as a decimal head); single zero spans are
    # never flagged (see _written_should_check) so this stays harmless.
    return str(value)


def _written_should_check(tokens: list[str], value: str, unit: str) -> bool:
    """False-positive guard for *flagging* written numbers (they are still
    extracted for claim-side coverage). Skip single filler words like
    "một"/"hai" with no unit; check multi-token spans, measured units and
    values ≥ 100 (prices, weights)."""
    if len(tokens) >= 2:
        return True
    if unit in _STRICT_UNITS:
        return True
    try:
        return float(value.replace(",", ".")) >= 100.0
    except ValueError:
        return True


def _candidates(value: str, tokens: list[str]) -> list[str]:
    """Matching candidates for one occurrence. A bare two-digit pair ("năm ba"
    for "Bluetooth 5.3") also matches the joined, dotted and single-digit
    readings so legit spellings are not flagged (TASK_B §4 example)."""
    if len(tokens) == 2 and _plain_digit_run(tokens):
        a, b = (_DIGIT_WORDS[t] for t in tokens)
        return [f"{a}{b}", f"{a}.{b}", str(a), str(b)]
    return [value]


def written_numbers(text: str) -> list[dict]:
    """Find Vietnamese numbers written in words and normalize them to digits.

    Returns occurrences ``{"value", "unit", "as_written", "tokens", "checked",
    "definite", "range_low"}`` — the "tám trăm bốn mươi kg" case normalizes to
    ``value="840", unit="kg"``. ``definite`` is False for bare digit-by-digit
    runs ("một sáu tám"), whose segmentation is ambiguous: analyze reports a
    mismatch on those as "uncertain" instead of "error". A "đến"/"tới" between
    two readings marks a spoken range — the high side carries ``range_low``.
    Unparseable spans are silently skipped (documented limitation).
    """
    occurrences: list[dict] = []
    tokens = _number_tokens(text)
    folded = _fold_tokens(tokens)
    canons = [_surface_to_canon(t) for t in tokens]
    i = 0
    last_value: str | None = None
    armed_low: str | None = None
    while i < len(tokens):
        if canons[i] is None:
            if unicodedata.normalize("NFC", tokens[i].lower()) in _RANGE_MARKERS and (
                last_value is not None
            ):
                armed_low = last_value  # "A đến B": B's occurrence carries range_low=A
            i += 1
            continue
        j = i
        span: list[str] = []
        while j < len(canons) and canons[j] is not None:
            # "phần trăm" pair becomes a % marker; a lone "phần" breaks the span.
            if canons[j] == "phan":
                if j + 1 < len(canons) and canons[j + 1] == "tram":
                    span.append(_UNIT_MARKER_PERCENT)
                    j += 2
                    continue
                break
            span.append(canons[j])
            j += 1
        unit = ""
        if j < len(folded) and folded[j] in _UNIT_VOCAB:
            unit = folded[j]
        value = None
        definite = any(t in _DEFINITE_SPAN_TOKENS for t in span)
        if span and span[-1] == _UNIT_MARKER_PERCENT:
            value = _parse_number_tokens(span)
            unit = "%"
        elif (
            len(span) >= 2
            and span[-1] == _UNIT_MARKER_YEAR
            and (_plain_digit_run(span[:-1]) or span[:-1][-1] == _TENS_SUFFIX)
        ):
            # "một năm"/"hai mươi năm" is a year count, not "15"/"25": the
            # trailing "năm" after a completed count is the unit.
            value = _parse_number_tokens(span[:-1])
            unit = "nam"
        else:
            value = _parse_number_tokens(span)
        if value is not None:
            # Show the spoken words including the unit token ("tám trăm bốn mươi kg").
            span_end = j + 1 if unit and j < len(folded) and folded[j] == unit else j
            occurrences.append(
                {
                    "value": value,
                    "unit": unit,
                    "as_written": " ".join(tokens[i:span_end]),
                    "tokens": list(span),
                    "checked": _written_should_check(span, value, unit),
                    "definite": definite,
                    "range_low": armed_low,
                }
            )
            armed_low = None
            last_value = value
        i = max(j, i + 1)
    return occurrences


def _digit_numbers(text: str) -> list[dict]:
    """Digit-form numbers ("299.000đ", "0,84 kg", "1m55", "1.500W") with their
    attached unit, if the unit is recognized."""
    occurrences: list[dict] = []
    for match in _NUM_RE.finditer(text):
        kind = match.lastgroup
        raw = match.group(0)
        if kind == "thousands":
            value = str(int(raw.replace(".", "")))
        elif kind == "decimal":
            value = raw
        elif kind == "meters":
            value = raw  # "1m55" matches the claim-side extractor symmetrically
        else:
            value = str(int(raw))
        unit = ""
        tail = _UNIT_TAIL_RE.match(text[match.end() :])
        if tail:
            candidate = fold_text(tail.group(1))
            if candidate in _UNIT_VOCAB:
                unit = candidate
        occurrences.append(
            {
                "value": value,
                "unit": unit,
                "as_written": raw,
                "checked": True,
                "start": match.start(),
                "end": match.end(),
            }
        )
    return occurrences


def extract_numbers(text: str) -> list[dict]:
    """All number occurrences in ``text`` (digit form + written form).

    Each item: ``{"value", "unit", "as_written", "written", "checked"}``.
    ``checked=False`` items (filler written words like "một") still contribute
    to claim-side coverage sets but are never reported as violations.
    """
    found = []
    for occurrence in _digit_numbers(text):
        occurrence["written"] = False
        found.append(occurrence)
    for occurrence in written_numbers(text):
        occurrence["written"] = True
        found.append(occurrence)
    return found


# ── Claim coverage ───────────────────────────────────────────────────────────

# Text allowed between two claim numbers for them to form a low–high range
# ("1m65–1m74", "52–58kg", "2 tới 4 ngày").
_RANGE_JOIN_RE = re.compile(r"^(?:\s*[-–—~]\s*|\s+(?:tới|đến)\s+)$")


def _digits_of(value: str) -> str:
    return re.sub(r"\D", "", value)


def _in_claim_range(digits: str, ranges: list[tuple[int, int, str]]) -> bool:
    """True when an all-digit reading falls inside a claim's low–high range
    ("một sáu lăm" → 165 inside the claim "1m60–1m68"). The digit length must
    match the endpoints so "16" is only covered via its padded form "160"."""
    if not 2 <= len(digits) <= 4:
        return False
    number = int(digits)
    return any(len(str(low)) == len(digits) and low <= number <= high for low, high, _ in ranges)


def _claim_coverage(claims: list[str]) -> dict:
    """Coverage sets built from the approved claims:

    - ``values`` — exact value strings ("0,84", "2990000");
    - ``pairs`` — ``"value|unit"`` so "0,84 kg" only matches a claim with the unit;
    - ``digit_strings`` — digit-only projections ("1m68" → "168") so digit-by-
      digit spoken readings ("một sáu tám") match;
    - ``ranges`` — ``(low, high, unit)`` triples for claim text like
      "1m65–1m74" or "52–58kg"; a reading inside the range is covered.
    """
    values: set[str] = set()
    pairs: set[str] = set()
    digit_strings: set[str] = set()
    ranges: list[tuple[int, int, str]] = []
    for claim in claims:
        text = (
            claim if isinstance(claim, str) else str(claim.get("claim") or claim.get("text") or "")
        )
        found = extract_numbers(text)
        for occurrence in found:
            values.add(occurrence["value"])
            pairs.add(f"{occurrence['value']}|{occurrence['unit']}")
            digits = _digits_of(occurrence["value"])
            if digits:
                digit_strings.add(digits)
        for previous, following in zip(found, found[1:]):
            if "start" not in previous or "start" not in following:
                continue  # only digit-form occurrences carry positions
            if not _RANGE_JOIN_RE.match(text[previous["end"] : following["start"]]):
                continue
            low, high = _digits_of(previous["value"]), _digits_of(following["value"])
            if low and high and len(low) <= 4 and len(high) <= 4 and int(low) <= int(high):
                unit = following.get("unit") or previous.get("unit") or ""
                ranges.append((int(low), int(high), unit))
    return {"values": values, "pairs": pairs, "digit_strings": digit_strings, "ranges": ranges}


def _segmentation_covered(digits: str, coverage: dict) -> bool:
    """True when a long digit-by-digit run can be split so that EVERY part
    matches the claims ("một sáu tám năm hai" → 168 + 52 against claims that
    have 1m68 and 52–58kg). Parts are 2–3 digits (height/weight readings);
    runs outside 4–6 digits are not split (no realistic reading needs it)."""
    if not 4 <= len(digits) <= 6:
        return False
    values = coverage["values"]
    digit_strings = coverage["digit_strings"]
    ranges = coverage["ranges"]

    def part_ok(part: str) -> bool:
        candidates = {part, part + "0"} if len(part) <= 3 else {part}
        return any(
            c in values or c in digit_strings or _in_claim_range(c, ranges) for c in candidates
        )

    def walk(remaining: str) -> bool:
        if not remaining:
            return True
        for size in (2, 3):
            if len(remaining) >= size and part_ok(remaining[:size]) and walk(remaining[size:]):
                return True
        return False

    return walk(digits)


def _covered(occurrence: dict, coverage: dict) -> bool:
    values = coverage["values"]
    pairs = coverage["pairs"]
    digit_strings = coverage["digit_strings"]
    ranges = coverage["ranges"]
    unit = occurrence.get("unit", "")
    tokens = occurrence.get("tokens") or []
    value = occurrence["value"]
    pure_run = (
        bool(tokens) and _plain_digit_run(tokens) and value.isdigit() and len(tokens) == len(value)
    )
    candidates = list(_candidates(value, tokens))
    if pure_run and 2 <= len(value) <= 3:
        # Height read digit-by-digit: "một sáu" → 1m60 (padded "160").
        candidates.append(value + "0")
    for candidate in candidates:
        if f"{candidate}|{unit}" in pairs:
            return True
        if candidate in values:
            return True
        if candidate.isdigit() and candidate in digit_strings:
            return True
        if candidate.isdigit() and _in_claim_range(candidate, ranges):
            return True
    low = occurrence.get("range_low")
    if (
        low is not None
        and value.isdigit()
        and low.isdigit()
        and 1 <= len(low) <= 4
        and 1 <= len(value) <= 4
        and int(low) <= int(value)
    ):
        spoken = (int(low), int(value))
        for claim_low, claim_high, _unit in ranges:
            if spoken == (claim_low, claim_high) or (
                claim_low <= spoken[0] and spoken[1] <= claim_high
            ):
                return True
    return bool(pure_run and _segmentation_covered(value, coverage))


# ── Repetition / openings ────────────────────────────────────────────────────


def _sentence_key(sentence: str) -> str:
    return fold_text(sentence)


def _section_opener(section_text: str, words: int = 5) -> str:
    tokens = fold_text(section_text).split()
    return " ".join(tokens[:words])


def _is_recap_role(role: str) -> bool:
    folded = fold_text(role)
    return any(marker in folded for marker in RECAP_ROLE_MARKERS)


# ── Flatness ─────────────────────────────────────────────────────────────────

_QUESTION_STARTS = ("ai ", "gi ", "nao ", "dau ", "sao ", "bao nhieu", "bao gio", "the nao")
_INVITATION_STARTS = (
    "hay ",
    "cung ",
    "thu ",
    "nho ",
    "moi ",
    "dung quen",
    "chot ",
    "comment",
    "bam ",
    "viet ",
    "quan tam",
    "theo doi",
    "ghé ",
    "ghe ",
)
_INVITATION_ENDS = ("nha", "nhe")


def _classify_sentence(sentence: str) -> str:
    folded = fold_text(sentence)
    if "?" in sentence or folded.startswith(_QUESTION_STARTS):
        return "question"
    if folded.startswith(_INVITATION_STARTS) or folded.rstrip().endswith(_INVITATION_ENDS):
        return "invitation"
    return "short_affirmative" if len(sentence) < 60 else "long"


def _is_interaction(sentence: str, keywords: tuple[str, ...]) -> bool:
    folded = fold_text(sentence)
    return "?" in sentence or any(keyword in folded for keyword in keywords)


# ── Main entry point ─────────────────────────────────────────────────────────


def normalize_sections(sections: list) -> list[dict]:
    """Accept dicts ``{"text", "role"/"kind"}`` or plain strings; return
    ``[{"text": str, "role": str}]``."""
    normalized = []
    for section in sections or []:
        if isinstance(section, str):
            normalized.append({"text": section, "role": ""})
        else:
            normalized.append(
                {
                    "text": str(section.get("text", "")),
                    "role": str(section.get("role") or section.get("kind") or ""),
                }
            )
    return normalized


def analyze_script(
    sections: list,
    claims: list[str],
    product: dict | None = None,
    *,
    floor_s: float | None = None,
    ceiling_s: float | None = None,
    interaction_target_s: float = INTERACTION_TARGET_S,
    buzzwords: tuple[str, ...] | None = None,
) -> dict:
    """Full offline analysis of one script. Returns the metric report dict —
    see ``scripts/qa/README.md`` for the exact shape and heuristics."""
    product = product or {}
    floor = float(
        floor_s if floor_s is not None else product.get("target_floor_s", PRODUCT_FLOOR_DEFAULT_S)
    )
    ceiling = float(
        ceiling_s
        if ceiling_s is not None
        else product.get("target_ceiling_s", PRODUCT_CEILING_DEFAULT_S)
    )
    phrases = tuple(buzzwords) if buzzwords is not None else DEFAULT_BUZZWORDS
    normalized = normalize_sections(sections)
    claim_texts = [
        c if isinstance(c, str) else str(c.get("claim") or c.get("text") or "")
        for c in (claims or [])
    ]
    coverage = _claim_coverage(claim_texts)
    claim_folded = [fold_text(c) for c in claim_texts if fold_text(c)]

    # ── totals + duration ────────────────────────────────────────────────────
    total_chars = sum(len(s["text"]) for s in normalized)
    sentences: list[dict] = []  # {"text", "section", "offset"} offset in joined text
    joined_offset = 0
    for index, section in enumerate(normalized):
        for sentence in split_sentences(section["text"]):
            sentences.append({"text": sentence, "section": index, "offset": joined_offset})
            joined_offset += len(sentence) + 1
        joined_offset += 1  # newline between sections
    estimated_s = total_chars / VOICE_CHARS_PER_S
    pct_of_floor = (estimated_s / floor * 100.0) if floor > 0 else 0.0

    totals = {
        "chars": total_chars,
        "sections": len(normalized),
        "sentences": len(sentences),
        "estimated_duration_s": round(estimated_s, 1),
        "target_floor_s": floor,
        "target_ceiling_s": ceiling,
        "duration_pct_of_floor": round(pct_of_floor, 1),
    }

    issues: list[str] = []
    if estimated_s < DURATION_MIN_FRACTION_OF_FLOOR * floor:
        issues.append("duration_below_70pct_floor")

    # ── number fraud ────────────────────────────────────────────────────────
    digit_violations: list[dict] = []
    written_violations: list[dict] = []
    written_uncertain: list[dict] = []
    checked_count = 0
    for sentence_info in sentences:
        for occurrence in extract_numbers(sentence_info["text"]):
            if occurrence["checked"]:
                checked_count += 1
            if not occurrence["checked"] or _covered(occurrence, coverage):
                continue
            violation = {
                "value": occurrence["value"],
                "unit": occurrence["unit"],
                "as_written": occurrence["as_written"],
                "written": occurrence["written"],
                "section": sentence_info["section"],
                "sentence": sentence_info["text"][:160],
            }
            if occurrence["written"] and not occurrence.get("definite", True):
                # Ambiguous digit-by-digit run ("một bảy tám tám sáu"): report
                # it, but as "uncertain" — it must not fail the document.
                violation["severity"] = "uncertain"
                written_uncertain.append(violation)
            else:
                violation["severity"] = "error"
                (written_violations if occurrence["written"] else digit_violations).append(
                    violation
                )
    if digit_violations:
        issues.append("digit_number_not_in_claims")
    if written_violations:
        issues.append("written_number_not_in_claims")
    numbers = {
        "checked_occurrences": checked_count,
        "digit_violations": digit_violations,
        "written_violations": written_violations,
        "written_uncertain": written_uncertain,
        "violation_count": len(digit_violations) + len(written_violations),
        "uncertain_count": len(written_uncertain),
    }

    # ── buzzwords (only when not covered by an approved claim) ──────────────
    buzz_hits: list[dict] = []
    for phrase in phrases:
        folded_phrase = fold_text(phrase)
        if not folded_phrase:
            continue
        if any(folded_phrase in claim for claim in claim_folded):
            continue
        for index, section in enumerate(normalized):
            folded_section = fold_text(section["text"])
            if folded_phrase in folded_section:
                buzz_hits.append({"phrase": phrase, "section": index})
                break
    if buzz_hits:
        issues.append("buzzword_not_in_claims")
    buzzwords_report = {
        "hits": buzz_hits,
        "hit_count": len(buzz_hits),
        "phrases_checked": len(phrases),
    }

    # ── repetition (main vs recap; fallback: any cross-section repeat) ──────
    recap_sections = [i for i, s in enumerate(normalized) if _is_recap_role(s["role"])]
    has_roles = any(s["role"] for s in normalized)
    by_key: dict[str, list[int]] = {}
    for sentence_info in sentences:
        by_key.setdefault(_sentence_key(sentence_info["text"]), []).append(sentence_info["section"])
    verbatim_main_recap = []
    repeated_across_sections = []
    for key, section_ids in by_key.items():
        distinct = sorted(set(section_ids))
        if len(distinct) < 2:
            continue
        if (
            has_roles
            and any(i in recap_sections for i in distinct)
            and any(i not in recap_sections for i in distinct)
        ):
            verbatim_main_recap.append(
                {
                    "sentence": key,
                    "main_sections": [i for i in distinct if i not in recap_sections],
                    "recap_sections": [i for i in distinct if i in recap_sections],
                }
            )
        repeated_across_sections.append({"sentence": key, "sections": distinct})
    openings: dict[str, list[int]] = {}
    quay_lai: list[str] = []
    for index, section in enumerate(normalized):
        opener = _section_opener(section["text"])
        if not opener:
            continue
        openings.setdefault(opener, []).append(index)
        if opener.startswith(("minh quay lai", "quay lai")):
            quay_lai.append(opener)
    similar_openings = [
        {"prefix": prefix, "sections": ids}
        for prefix, ids in sorted(openings.items())
        if len(ids) >= 2
    ]
    if verbatim_main_recap:
        issues.append("verbatim_repeat_main_recap")
    if not has_roles and repeated_across_sections:
        issues.append("sentence_repeated_across_sections")
    if similar_openings:
        issues.append("similar_openings")
    repetition = {
        "has_roles": has_roles,
        "recap_sections": recap_sections,
        "verbatim_main_recap": verbatim_main_recap,
        "repeated_across_sections": repeated_across_sections,
        "similar_openings": similar_openings,
        "quay_lai_openings": quay_lai,
    }

    # ── flatness ────────────────────────────────────────────────────────────
    lengths = [len(s["text"]) for s in sentences]
    interaction_positions = [
        s["offset"] for s in sentences if _is_interaction(s["text"], INTERACTION_KEYWORDS)
    ]
    gaps_s: list[float] = []
    previous = 0
    for position in interaction_positions:
        gaps_s.append((position - previous) / VOICE_CHARS_PER_S)
        previous = position
    max_gap_s = max(gaps_s) if gaps_s else estimated_s
    structure_counts: dict[str, int] = {}
    for sentence_info in sentences:
        kind = _classify_sentence(sentence_info["text"])
        structure_counts[kind] = structure_counts.get(kind, 0) + 1
    if interaction_positions:
        if max_gap_s > interaction_target_s:
            issues.append("interaction_gap_over_target")
    else:
        issues.append("no_interaction")
    flatness = {
        "avg_sentence_chars": round(statistics.fmean(lengths), 1) if lengths else 0.0,
        "stdev_sentence_chars": round(statistics.pstdev(lengths), 1) if len(lengths) > 1 else 0.0,
        "sentence_count": len(lengths),
        "interaction_count": len(interaction_positions),
        "interaction_ratio": (
            round(len(interaction_positions) / len(sentences), 3) if sentences else 0.0
        ),
        "max_interaction_gap_s": round(max_gap_s, 1),
        "interaction_target_s": interaction_target_s,
        "structure_types": structure_counts,
        "structure_type_count": len(structure_counts),
    }

    return {
        "totals": totals,
        "numbers": numbers,
        "buzzwords": buzzwords_report,
        "repetition": repetition,
        "flatness": flatness,
        "issues": issues,
        "passed": not issues,
    }
