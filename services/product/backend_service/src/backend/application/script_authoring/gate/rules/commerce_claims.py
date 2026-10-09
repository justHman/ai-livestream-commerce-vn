"""Commerce-claim rules comparing script claims against authoritative facts.

Task 3.7: price, discount, promotion, SKU/product identity, and configured
factual claims are validated against ``context.facts`` (the authoritative
product/promotion data). A claim that references a value the backend cannot
confirm is an ERROR: an unverified claim is an unsupported claim.

Pure pattern matching, no AI detection heuristics.
"""

from __future__ import annotations

import re

from ..context import ProductFacts
from ..results import RuleViolation, Severity, TextSpan

__all__ = [
    "check_price_claims",
    "check_discount_claims",
    "check_identity_claims",
    "check_factual_claims",
    "RULE_CLAIM_PRICE",
    "RULE_CLAIM_DISCOUNT",
    "RULE_CLAIM_IDENTITY",
    "RULE_CLAIM_FACTUAL",
]

# Compact Vietnamese price forms: "299.000đ", "299.000 đ", "299,000đ",
# "299k", "2.99 triệu", "2 triệu". The group separator is "." or "," (both
# appear in Vietnamese commerce text); "k" and "triệu" are unit suffixes.
_UNIT = r"(?:k\s?[đ₫]|₫|đồng|đ|(?i:vnđ|vnd)|nghìn|ngàn|triệu|tr|k|K)"
_PRICE_RE = re.compile(
    # grouped number with an optional unit (so "299.000k" is read whole, not as 299.000),
    # or any number followed by a currency/multiplier unit.
    # Attached/spaced suffix variants ("kđ", "k đ", "k₫", "₫") belong to the token so a
    # multiplier can never be dropped and the remainder compared as a plain price.
    r"\d{1,3}(?:[.,]\d{3})+(?:\s*" + _UNIT + r"(?!\w))?"
    r"|\d+(?:[.,]\d+)?\s*" + _UNIT + r"(?!\w)",
    re.UNICODE | re.IGNORECASE,
)

# Discount forms: "giảm 20%", "-20%", "giảm giá 20%", "khuyến mãi 50%",
# "off 20%".
_DISCOUNT_RE = re.compile(r"(?:giảm|giảm giá|khuyến mãi|off)\s*-?\s*\d+(?:[.,]\d+)?\s*%")

# Number-only discount (bare "20%" with no verb) is a WARNING: it may be a
# price/discount ambiguity ("20% của giá").
_BARE_PERCENT_RE = re.compile(r"\d+(?:[.,]\d+)?\s*%")

# SKU/product identity: alphanumeric codes with optional dashes, e.g.
# "SKU123", "ABC-X1", "SP-299". Also catches quoted product names.
_SKU_RE = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)?\d{2,}[A-Z0-9-]*\b")

# Benefit/capability signals (15.4 real-LLM E2E redesign). A sentence is an
# unsupported factual claim ONLY when it asserts a specific product benefit or
# capability that is not among the authoritative allowed claims. The old
# trigger was a handful of generic verbs matched as substrings ("có", "giúp",
# "làm", "chứa", "tăng", "giảm") — but those words appear in natural
# Vietnamese scene-setting/transitions ("đi làm về", "bình chứa cồng kềnh",
# "nhu cầu tăng cao"), so the rule rejected normal prose a real LLM produces
# every run and blocked REVIEWABLE deterministically. This vocabulary is
# specific compound phrases that only a real product claim carries; generic
# verbs are deliberately absent.
_BENEFIT_SIGNALS = (
    # capability / effect verbs (specific to product performance)
    "loại bỏ",
    "lọc sạch",
    "khử",
    "diệt",
    "ngăn ngừa",
    "ngăn chặn",
    "cải thiện",
    "nâng cao",
    "tăng cường",
    "bảo vệ",
    "duy trì",
    "tiết kiệm",
    "tối ưu",
    "giảm bớt",
    "làm sạch",
    "làm trắng",
    "làm mềm",
    "bền bỉ",
    # quality / safety adjectives
    "an toàn",
    "hiệu quả",
    "đáng tin cậy",
    "chất lượng",
    "ổn định",
    # design / usage attributes
    "gọn nhẹ",
    "nhỏ gọn",
    "dễ dàng",
    "đơn giản",
    "tiện lợi",
    "thiết kế",
    # attribute / spec nouns
    "công suất",
    "tuổi thọ",
    "độ bền",
    "nguyên liệu",
    "thành phần",
    "bảo hành",
    "bảo trì",
    "không dùng điện",
    "không cần điện",
    "không tốn điện",
)

_BENEFIT_RE = re.compile(
    "|".join(re.escape(signal) for signal in _BENEFIT_SIGNALS),
    re.IGNORECASE,
)

# Vietnamese clause connectors: a factual sentence may combine a supported
# fragment with an invented extension ("thiết kế gọn nhẹ và bảo hành 10
# năm"). Splitting on these makes support checking clause-level so the
# supported fragment never authorizes an appended invented claim (reviewer
# R9.3). The set is deliberately small and generic (and/meanwhile + comma/
# semicolon); "với" is excluded because it is too ambiguous in prose.
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:\bvà\b|đồng thời|,|;)\s*")


def _split_clauses(sentence: str) -> list[str]:
    """Split one sentence into independent claim clauses.

    A claim support check must be clause-level (reviewer R9.3): a sentence
    can join an authorized fragment with an invented factual extension, and
    the supported fragment alone must not authorize the rest.
    """
    return [c.strip() for c in _CLAUSE_SPLIT_RE.split(sentence) if c.strip()]


# Vietnamese function words that must not count as claim-overlap evidence:
# they appear in nearly every sentence regardless of whether a claim is
# discussed, so including them lets a scene-setting sentence "overlap" an
# allowed claim on pure filler words.
_STOPWORDS = frozenset(
    """
    có này là một và để cho của với các những được không rất vô cùng
    tại trong khi thì về sẽ đã đang cũng từ nên vào ra lên xuống đó đây
    hơn còn mới mà do như nó anh chị em bạn mọi người gia đình cả mỗi
    ngày nữa quá thật đều chính vẫn lại xong luôn sẵn
    """.split()
)

_WORD_RE = re.compile(r"[\w]+", re.UNICODE)


def _content_words(text: str) -> set[str]:
    """Lowercased word tokens minus function words (claim-overlap evidence)."""
    return {w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS}


def _overlaps_allowed(lowered_clause: str, allowed: list[str]) -> bool:
    """True when the clause shares >=2 content words with any allowed claim.

    Clause-level, paraphrase-tolerant authorization (15.4 real-LLM E2E
    redesign + reviewer R9.3): a real LLM restates an allowed claim in
    natural words rather than verbatim ("thiết kế tinh tế gọn gàng hiện đại"
    for "thiết kế gọn nhẹ", "bảo trì định kỳ diễn ra đơn giản" for "bảo trì
    đơn giản"). Two shared content words are enough evidence that the clause
    describes that authorized claim; an invented clause (công suất 500 lít,
    bảo hành 5 năm) shares no content words with the allowed set and is still
    flagged.
    """
    words = _content_words(lowered_clause)
    for claim in allowed:
        if len(words & _content_words(claim)) >= 2:
            return True
    return False


def _span_of(match: re.Match[str]) -> TextSpan:
    return TextSpan(match.start(), match.end())


_UNIT_X1000 = ("k", "nghìn", "ngàn")
_UNIT_X1M = ("triệu", "tr")
_AMOUNT_RE = re.compile(
    r"(\d+(?:[.,]\d+)*)\s*(k\s?[đ₫]|₫|đồng|đ|vnđ|vnd|nghìn|ngàn|triệu|tr|k)?", re.IGNORECASE
)


def _vnd_amount(value: str) -> int | None:
    """Whole-dong amount of a price, strictly; ``None`` = ambiguous (fail closed).

    "299.000đ", "299,000 đồng", "299000 VND" -> 299000; "299k" -> 299000 (k/nghìn/ngàn
    only after a plain integer); "2,5 triệu" -> 2500000 (tr/triệu after an integer or
    a 1-2 digit decimal). A thousands-grouped number with a multiplier ("299.000k"),
    a bare decimal ("2.5") or anything else is NOT a known price.
    """
    match = _AMOUNT_RE.fullmatch(value.strip())
    if match is None:
        return None
    number, unit = match.group(1), re.sub(r"\s", "", (match.group(2) or "").lower())
    if unit in ("kđ", "k₫"):
        unit = "k"
    if re.fullmatch(r"\d+[.,]0{1,2}", number) and unit in ("đ", "₫", "đồng", "vnđ", "vnd"):
        number = re.split(r"[.,]", number)[0]  # "100000.00 VND": zero cents
    grouped = re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", number) is not None
    plain = number.isdigit()
    if unit in _UNIT_X1000:
        return int(number) * 1000 if plain else None
    if unit in _UNIT_X1M:
        if plain:
            return int(number) * 1_000_000
        if re.fullmatch(r"\d+[.,]\d{1,2}", number):
            return round(float(number.replace(",", ".")) * 1_000_000)
        return None
    if plain:
        return int(number)
    return int(re.sub(r"[.,]", "", number)) if grouped else None


def _claims(claim: str, facts: ProductFacts) -> bool:
    """True when the claim value appears among the authoritative facts."""
    normalized = re.sub(r"\s+", " ", claim.strip().lower())
    for candidate in (*facts.prices, *facts.discounts, *facts.skus):
        if normalized == re.sub(r"\s+", " ", candidate.lower()):
            return True
    amount = _vnd_amount(claim)
    return amount is not None and any(_vnd_amount(price) == amount for price in facts.prices)


def _discount_stated(token: str, facts: ProductFacts) -> bool:
    """The discount token is an approved discount, or part of an approved discount/promotion
    statement ("giảm 20%" inside "Giảm 20% vào thứ Tư đầu tháng")."""
    if _claims(token, facts):
        return True
    needle = re.sub(r"\s+", " ", token.strip().lower())
    return any(
        needle in re.sub(r"\s+", " ", statement.lower())
        for statement in (*facts.discounts, *facts.allowed_claims)
    )


def check_price_claims(text: str, context) -> list[RuleViolation]:
    """Flag prices that have no authoritative counterpart in ``facts.prices``.

    ERROR: a price the backend cannot confirm must not be spoken.
    """
    violations: list[RuleViolation] = []
    for match in _PRICE_RE.finditer(text):
        if _claims(match.group(), context.facts):
            continue
        violations.append(
            RuleViolation(
                rule_id=RULE_CLAIM_PRICE,
                severity=Severity.ERROR,
                message=(
                    f"Price {match.group()!r} is not among the authoritative "
                    "product prices; verify before speaking."
                ),
                text_span=_span_of(match),
            )
        )
    return violations


def check_discount_claims(text: str, context) -> list[RuleViolation]:
    """Flag discounts not in ``facts.discounts``.

    ERROR for explicit discount verbs ("giảm 20%"); WARNING for a bare
    percentage that may be a discount or a price share.
    """
    violations: list[RuleViolation] = []
    for match in _DISCOUNT_RE.finditer(text):
        if _discount_stated(match.group(), context.facts):
            continue
        violations.append(
            RuleViolation(
                rule_id=RULE_CLAIM_DISCOUNT,
                severity=Severity.ERROR,
                message=(
                    f"Discount {match.group()!r} is not among the authoritative "
                    "promotion discounts; verify before speaking."
                ),
                text_span=_span_of(match),
            )
        )
    discount_spans = [(m.start(), m.end()) for m in _DISCOUNT_RE.finditer(text)]
    for match in _BARE_PERCENT_RE.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in discount_spans):
            # The percent is part of an authorized "giảm X%" span already.
            continue
        if _discount_stated(match.group(), context.facts):
            continue
        violations.append(
            RuleViolation(
                rule_id=RULE_CLAIM_DISCOUNT,
                severity=Severity.WARNING,
                message=(
                    f"Percentage {match.group()!r} is not tied to an "
                    "authoritative discount; confirm it is intended."
                ),
                text_span=_span_of(match),
            )
        )
    return violations


def check_identity_claims(text: str, context) -> list[RuleViolation]:
    """Flag SKU/product-code references not in ``facts.skus``.

    ERROR: a product identity the backend cannot confirm is unsupported.
    """
    violations: list[RuleViolation] = []
    for match in _SKU_RE.finditer(text):
        if _claims(match.group(), context.facts):
            continue
        violations.append(
            RuleViolation(
                rule_id=RULE_CLAIM_IDENTITY,
                severity=Severity.ERROR,
                message=(
                    f"Product/SKU code {match.group()!r} is not among the "
                    "authoritative SKUs; verify before speaking."
                ),
                text_span=_span_of(match),
            )
        )
    return violations


# Numbers with a unit in product talk. Advisory by design: the safety invariant is that
# only exact owner-approved text is spoken. Two tiers, evaluated PER CLAUSE:
#   ERROR   - a CONTRADICTION: the clause is about the same thing as an approved statement
#             (shared subject words) that has a number of the same dimension, and the clause's
#             number/unit differs from all of them ("bảo hành 10 năm" vs "Bảo hành 1 năm");
#   WARNING - a number+unit that no approved statement speaks about (unsupported).
# Prices and discount percentages keep their own strict rules; bare numbers are ignored.
_UNIT_DIMENSION = {
    **dict.fromkeys(("năm", "tháng", "ngày", "tuần"), "time"),
    **dict.fromkeys(("giờ", "phút", "giây"), "clock"),
    **dict.fromkeys(("cm", "mm", "m", "inch"), "length"),
    **dict.fromkeys(("kg", "g", "mg"), "mass"),
    **dict.fromkeys(("ml", "l", "lít"), "volume"),
    "%": "percent",
    "size": "size",
    "mah": "battery",
    "watt": "power",
    "w": "power",
    "v": "voltage",
    "tuổi": "age",
    "lần": "count",
    "vòng": "count",
}
_NUM_RE = re.compile(
    r"(?:(?P<pre>size)\s*)?(?P<num>\d+(?:[.,]\d+)*)\s*"
    r"(?P<unit>(?:" + "|".join(sorted(_UNIT_DIMENSION, key=len, reverse=True)) + r")(?!\w)|%)?",
    re.IGNORECASE,
)
_STRONG_SIGNAL_RE = re.compile(
    r"bảo hành|bảo trì|hạn dùng|hạn sử dụng|hiệu quả|dung tích|thành phần|kích thước|công suất"
    r"|tuổi thọ|độ bền|đổi trả|hoàn tiền|cam kết|miễn phí|tặng|giảm|dùng được|sử dụng được"
    r"|trọng lượng|kích cỡ",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# A comma between digits is a decimal separator ("1,5 kg"), not a clause break.
_NUM_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:(?<!\d),|,(?!\d)|;|\bvà\b|\bnhưng\b|\bcòn\b|\s[-—–]\s)\s*"
)
# Words too generic to say two statements are about the same thing.
_GENERIC_WORDS = frozenset(
    "dùng sản phẩm mọi người nhà shop hôm nay được trong bạn mình cả nhé nha ạ".split()
)


def _num_key(number: str) -> str:
    """Canonical value: "1,5" -> "1.5"; a thousands group ("299.000") -> "299000"."""
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", number):
        return re.sub(r"[.,]", "", number)
    return number.replace(",", ".")


def _numbers(text: str) -> list[tuple[str, str, int, int]]:
    """(value, unit, start, end) of every number in ``text``; unit may be empty."""
    found = []
    for m in _NUM_RE.finditer(text):
        unit = (m.group("unit") or m.group("pre") or "").lower()
        found.append((_num_key(m.group("num")), unit, m.start(), m.end()))
    return found


def _claim_context(text: str) -> set[str]:
    """Subject words of a statement: content words minus numbers, units, generic words."""
    return {
        w
        for w in _content_words(text)
        if not w.isdigit() and w not in _UNIT_DIMENSION and w not in _GENERIC_WORDS
    }


def _clause_items(text: str) -> list[tuple[str, set[str]]]:
    """(lowercase clause, subject words) per clause; a clause with no subject of its own
    ("Bảo hành, 10 năm") inherits the previous clause's."""
    items: list[tuple[str, set[str]]] = []
    previous: set[str] = set()
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        for clause in _NUM_CLAUSE_SPLIT_RE.split(sentence):
            lowered = clause.lower()
            words = _claim_context(lowered) or previous
            previous = words
            items.append((lowered, words))
    return items


def _approved_statements(facts: ProductFacts) -> list[tuple[set[tuple[str, str]], set[str]]]:
    """Every CLAUSE of every approved claim/discount/product name is its own source, so
    "Bảo hành 1 năm và tuổi thọ 10 năm" never lets the lifespan authorise the warranty."""
    texts = (*facts.allowed_claims, *facts.discounts, facts.product_name)
    out = []
    for t in texts:
        for lowered, words in _clause_items(t or ""):
            pairs = {(v, u) for v, u, _s, _e in _numbers(lowered) if u}
            if pairs:
                out.append((pairs, words))
    return out


def _numeric_violations(text: str, context) -> list[RuleViolation]:
    statements = _approved_statements(context.facts)
    violations: list[RuleViolation] = []
    for lowered, words in _clause_items(text):
        skip = [m.span() for m in _PRICE_RE.finditer(lowered)]
        skip += [m.span() for m in _DISCOUNT_RE.finditer(lowered)]
        for value, unit, start, end in _numbers(lowered):
            if not unit or any(a <= start and end <= b for a, b in skip):
                continue
            dimension = _UNIT_DIMENSION[unit]
            same_topic = [
                pairs
                for pairs, subject in statements
                if words & subject and any(_UNIT_DIMENSION[u] == dimension for _v, u in pairs)
            ]
            shown = lowered[start:end].strip()
            if same_topic:
                if any((value, unit) in pairs for pairs in same_topic):
                    continue
                violations.append(
                    RuleViolation(
                        rule_id=RULE_CLAIM_FACTUAL,
                        severity=Severity.ERROR,
                        message=(
                            f"Number {shown!r} contradicts the approved claim about the "
                            "same thing; use the approved value."
                        ),
                    )
                )
            elif (
                dimension != "clock"
                or _STRONG_SIGNAL_RE.search(lowered)
                or _BENEFIT_RE.search(lowered)
            ):
                violations.append(
                    RuleViolation(
                        rule_id=RULE_CLAIM_FACTUAL,
                        severity=Severity.WARNING,
                        message=(
                            f"Number {shown!r} is not in any approved claim; "
                            "confirm it is true before approving."
                        ),
                    )
                )
    return violations


def check_factual_claims(text: str, context) -> list[RuleViolation]:
    """Flag configured factual claims (sentences) absent from the allowed set.

    Product-agnostic, clause-level support (reviewer R9.3). ``allowed_claims``
    holds exact claim sentences known to be true; a claim the backend never
    authorized is an ERROR.

    A sentence is a claim candidate ONLY when it carries a specific
    benefit/capability/spec signal (``_BENEFIT_SIGNALS`` — category-agnostic
    capability words, never product nouns). Each signal-bearing CLAUSE must be
    authorized by clause-level word overlap with an allowed claim; an
    unsupported clause is an ERROR.

    The old product-reference guard (a hardcoded product-noun vocabulary) let
    unsupported claims escape when their nouns fell outside that vocabulary.
    Support is now derived purely from the allowed-claim set, so correctness
    does not depend on the product name/category.
    """
    violations: list[RuleViolation] = _numeric_violations(text, context)
    allowed = [claim.strip().lower() for claim in context.facts.allowed_claims]
    for sentence in re.split(r"[.!?]+", text):
        stripped = sentence.strip()
        if not stripped:
            continue
        # Sentences that only carry price/discount/SKU forms are covered by
        # the dedicated claim rules; do not re-flag them here.
        if _PRICE_RE.search(stripped) and not _BENEFIT_RE.search(stripped):
            continue
        for clause in _split_clauses(stripped):
            lowered = clause.lower()
            if not _BENEFIT_RE.search(lowered):
                continue
            if _overlaps_allowed(lowered, allowed):
                continue
            violations.append(
                RuleViolation(
                    rule_id=RULE_CLAIM_FACTUAL,
                    severity=Severity.ERROR,
                    message=(
                        f"Claim {clause!r} is not among the authoritative "
                        "allowed claims; do not state it."
                    ),
                )
            )
    return violations


# Stable rule IDs.
RULE_CLAIM_PRICE = "CLAIM_PRICE"
RULE_CLAIM_DISCOUNT = "CLAIM_DISCOUNT"
RULE_CLAIM_IDENTITY = "CLAIM_IDENTITY"
RULE_CLAIM_FACTUAL = "CLAIM_FACTUAL"
