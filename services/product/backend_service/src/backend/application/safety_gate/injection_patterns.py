"""Deterministic prompt-injection pattern detection (task 3.4).

This module is a conservative DETECTION-only signal for viewer comments,
not a security boundary. The structural boundary is the compact
``ClusterEnvelope``: viewer text is always untrusted data and can never
select system prompts, tools, retries, or execution policy.

Pattern matching is deterministic, case-insensitive, and bilingual
(Vietnamese + English + common VN teencode). Because injection patterns
can false-positive on benign commerce questions ("bạn có thể tư vấn giá
không" must NOT be rejected), patterns are split into two tiers:

- ``reject`` — clear-cut injection attempts ("ignore all previous
  instructions", "bỏ qua tất cả hướng dẫn trước", persona-swap, tool
  coercion, system-prompt extraction).
- ``signal`` — suspicious but plausibly benign (instruction-like
  phrasing, meta-talk about the assistant). Signals never produce a
  reason code on their own.

Composition policy (accepted-with-signal vs rejected) lives in the
checks engine; this module only reports what it matched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .decision import ReasonCode

__all__ = [
    "InjectionPattern",
    "INJECTION_PATTERNS",
    "detect_injection",
    "detect_injection_signals",
]

# Tier names are stable policy identifiers, not display text.
TIER_REJECT = "reject"
TIER_SIGNAL = "signal"


@dataclass(frozen=True)
class InjectionPattern:
    """One deterministic injection pattern.

    ``pattern_id`` is a stable identifier for sanitized metrics and
    policy binding; ``tier`` selects reject vs signal semantics.
    """

    pattern_id: str
    pattern: re.Pattern[str]
    label: str
    tier: str


# Curated, versioned pattern set: 20 patterns, not exhaustive. Patterns
# are plain literal phrases (no capture groups, no backreferences) so
# matching is linear — no catastrophic backtracking possible. Vietnamese
# patterns keep full diacritics; a few high-frequency ones add
# no-diacritics variants because VN viewers frequently type without them.
INJECTION_PATTERNS: tuple[InjectionPattern, ...] = (
    # -- reject tier: clear-cut injection attempts ---------------------
    InjectionPattern(
        pattern_id="pi-ignore-instructions-en",
        pattern=re.compile(
            r"ignore (all )?(previous|prior) instructions?", re.IGNORECASE | re.UNICODE
        ),
        label="ignore previous instructions",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-ignore-instructions-vi",
        pattern=re.compile(
            r"bỏ qua (tất cả )?hướng dẫn (trước|trước đó)", re.IGNORECASE | re.UNICODE
        ),
        label="bỏ qua hướng dẫn trước",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-ignore-instructions-vi-nodiac",
        pattern=re.compile(r"bo qua huong dan (truoc|trc)", re.IGNORECASE | re.UNICODE),
        label="bỏ qua hướng dẫn trước (no diacritics)",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-ignore-instructions-disregard",
        pattern=re.compile(
            r"disregard (all )?(previous|prior) instructions?", re.IGNORECASE | re.UNICODE
        ),
        label="disregard previous instructions",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-ignore-instructions-vi-system",
        pattern=re.compile(r"ngừng làm hệ thống", re.IGNORECASE | re.UNICODE),
        label="ngừng làm hệ thống",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-persona-switch-en",
        pattern=re.compile(r"\b(act|pretend) as if\b", re.IGNORECASE | re.UNICODE),
        label="act as if / pretend",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-persona-switch-you-are",
        pattern=re.compile(r"\byou are now\b", re.IGNORECASE | re.UNICODE),
        label="you are now",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-persona-switch-vi",
        pattern=re.compile(r"(bạn bây giờ là|mày giờ là)", re.IGNORECASE | re.UNICODE),
        label="bạn bây giờ là / mày giờ là",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-persona-switch-vi-nodiac",
        pattern=re.compile(r"(ban|may) bay gio la", re.IGNORECASE | re.UNICODE),
        label="bạn bây giờ là (no diacritics)",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-persona-switch-doi-vai",
        pattern=re.compile(r"đổi vai", re.IGNORECASE | re.UNICODE),
        label="đổi vai",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-tool-call-en",
        pattern=re.compile(
            r"\b(call|execute|invoke|run) (the )?(function|tool|api)s?", re.IGNORECASE | re.UNICODE
        ),
        label="call/execute/invoke tool or function",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-tool-call-vi",
        pattern=re.compile(r"gọi (hàm |function |tool |api )", re.IGNORECASE | re.UNICODE),
        label="gọi hàm/function/tool/api",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-system-prompt-extract",
        pattern=re.compile(
            r"(show|tell|print|reveal|give|read) (me |us |your )?(the |your )?system prompt",
            re.IGNORECASE | re.UNICODE,
        ),
        label="show system prompt",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-system-prompt-extract-vi",
        pattern=re.compile(
            r"cho (tao |em |tui )?xem (system prompt|prompt của bạn)", re.IGNORECASE | re.UNICODE
        ),
        label="cho xem system prompt",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-jailbreak-en",
        pattern=re.compile(r"\bjailbreak\b", re.IGNORECASE | re.UNICODE),
        label="jailbreak",
        tier=TIER_REJECT,
    ),
    InjectionPattern(
        pattern_id="pi-jailbreak-hack",
        pattern=re.compile(
            r"\b(bypass|unlock|hack) (the )?(system|rules?|restrictions?|safet[ye]|guardrails?)",
            re.IGNORECASE | re.UNICODE,
        ),
        label="bypass/unlock/hack system rules",
        tier=TIER_REJECT,
    ),
    # -- signal tier: suspicious but plausibly benign ------------------
    InjectionPattern(
        pattern_id="pi-signal-instruction",
        pattern=re.compile(
            r"\b(bạn|em|mày|anh|chị) (hãy|có thể|phải|nên) (nói|trả lời|tư vấn|giúp)\b",
            re.IGNORECASE | re.UNICODE,
        ),
        label="instruction-like phrasing",
        tier=TIER_SIGNAL,
    ),
    InjectionPattern(
        pattern_id="pi-signal-noi-nhu",
        pattern=re.compile(r"nói như (mày|bạn|em) là", re.IGNORECASE | re.UNICODE),
        label="nói như mày là",
        tier=TIER_SIGNAL,
    ),
    InjectionPattern(
        pattern_id="pi-signal-meta-assistant",
        pattern=re.compile(
            r"\b(are you|bạn có phải là) (an? |the )?(ai|bot|assistant|chatbot|người máy)\b",
            re.IGNORECASE | re.UNICODE,
        ),
        label="meta-talk about the assistant",
        tier=TIER_SIGNAL,
    ),
)


def detect_injection(text: str) -> tuple[ReasonCode, ...]:
    """Return ``(ReasonCode.PROMPT_INJECTION,)`` on a reject-tier match.

    Signal-tier and non-matching text return ``()`` — composition policy
    (accepted-with-signal vs rejected) is the checks engine's decision,
    not this module's. Input is raw untrusted viewer text: never raises,
    never mutates it; text is lowered only for matching.
    """
    lowered = text.lower()
    for pattern in INJECTION_PATTERNS:
        if pattern.tier == TIER_REJECT and pattern.pattern.search(lowered):
            return (ReasonCode.PROMPT_INJECTION,)
    return ()


def detect_injection_signals(text: str) -> tuple[str, ...]:
    """Return matched pattern_ids (content-safe labels, not raw text).

    Deterministic: iterate in declaration order, include every tier's
    match. Callers may record these ids in sanitized metrics — a
    pattern_id carries no viewer content.
    """
    lowered = text.lower()
    matched: list[str] = []
    for pattern in INJECTION_PATTERNS:
        if pattern.pattern.search(lowered):
            matched.append(pattern.pattern_id)
    return tuple(matched)
