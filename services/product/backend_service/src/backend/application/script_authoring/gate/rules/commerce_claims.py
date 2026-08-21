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
_PRICE_RE = re.compile(
    r"\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?\s*(?:đ|k|K|nghìn|triệu|tr)", re.UNICODE
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


def _claims(claim: str, facts: ProductFacts) -> bool:
    """True when the claim value appears among the authoritative facts."""
    normalized = re.sub(r"\s+", " ", claim.strip().lower())
    for candidate in (*facts.prices, *facts.discounts, *facts.skus):
        if normalized == re.sub(r"\s+", " ", candidate.lower()):
            return True
    return False


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
        if _claims(match.group(), context.facts):
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
        if _claims(match.group(), context.facts):
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
    violations: list[RuleViolation] = []
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
