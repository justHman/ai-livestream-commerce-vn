"""Deterministic malformed / replay-flood / spam checks (tasks 3.1, 3.2).

All checks are PURE: bounded inputs in, stable ``ReasonCode`` values out, no
I/O, no global state, never raise on weird input (a malformed check must not
itself crash the gate). The engine composes them; the replay rule additionally
takes a caller-provided ``ReplayWindow`` so ingestion state stays owned by the
caller (cluster C2), not this package.

Spam rules are explicitly HEURISTICS: conservative and combined — no signal
rejects on its own (a single URL never rejects), and thresholds are
deliberately loose to protect live-comment false positives.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import re

from .decision import ReasonCode

__all__ = [
    "check_malformed",
    "check_replay_flood",
    "check_spam",
    "ReplayWindow",
    "MAX_TEXT_LENGTH",
    "REPLAY_WINDOW_SECONDS",
    "REPLAY_FLOOD_COUNT",
]

# Curated deterministic bounds (policy version 1). The gate runs before any
# embedding or Agent context creation, so these guard resource usage as much
# as content: a 10 MB comment would be a DoS vector even if benign.
MAX_TEXT_LENGTH = 2000
REPLAY_WINDOW_SECONDS = 10.0
REPLAY_FLOOD_COUNT = 5

# URL / emoji / all-caps thresholds for the spam heuristics; conservative by
# design (see module docstring).
_MAX_URLS = 5
_MIN_EMOJI_RUN = 8
_MAX_ALL_CAPS_RATIO = 0.7
_MIN_ALPHA_LENGTH = 6
_MAX_PUNCT_RUN = 8
_MAX_REPEATED_TOKENS = 4

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMOJI_RE = re.compile(r"[\U0001F300-\U0001FAFF☀-➿️⭐←-⇿⏩-⏯❤]+")
_CAP_PUNCT_RE = re.compile(r"[A-Z!?….,。]+")
_REPEATED_TOKEN_RE = re.compile(r"(\w{3,})\s+\1(?:\s+\1){1,}", re.IGNORECASE)
# Promotional sales-spam template markers (curated; every marker is a
# multi-word template, never a single innocent word like "like").
_SPAM_MARKER_RE = re.compile(
    r"like and subscribe|like,? share and subscribe|tag (?:a friend|your friends)|"
    r"share this (?:video|post)|comment (?:below|down below) (?:to win|for a chance)|"
    r"gi?ft ?follow|follow for more",
    re.IGNORECASE,
)


def _is_whitespace_only(text: str) -> bool:
    return text.strip() == ""


def check_malformed(text: str | None) -> list[ReasonCode]:
    """Reject structurally unusable text (empty, oversized, control-only).

    Runs first so later checks never see degenerate input. ``text`` may be
    ``None`` from a malformed transport payload: the gate must not crash on
    the very input it exists to reject.
    """
    if text is None:
        return [ReasonCode.MALFORMED]
    if _is_whitespace_only(text):
        return [ReasonCode.MALFORMED]
    if len(text) > MAX_TEXT_LENGTH:
        return [ReasonCode.MALFORMED]
    if not any(char.isalnum() for char in text):
        # No letters or digits: pure control chars / zero-width is empty of
        # meaning, not a deliverable comment. Link-only text is NOT malformed
        # — a single URL is a legitimate live-comment ("link đây ạ"); URL
        # floods are the spam rule's job.
        return [ReasonCode.MALFORMED]
    return []


@dataclass
class ReplayWindow:
    """Caller-owned per-viewer (or per-session) replay state.

    Purely mechanical bookkeeping: keeps the N most recent normalized texts
    with their timestamps and answers "did this repeat enough to be a flood?"
    This package holds no session state — the ingestion layer (C2) owns
    windows and passes the instance in.
    """

    window_seconds: float = REPLAY_WINDOW_SECONDS
    flood_count: int = REPLAY_FLOOD_COUNT
    recent: deque[tuple[str, float]] = field(default_factory=deque)

    def normalized(self, text: str) -> str:
        """Deterministic normalization: lowercase, collapse whitespace."""
        return " ".join(text.lower().split())

    def observe(self, text: str, ts: float) -> ReasonCode | None:
        """Record one comment; return REPLAY_FLOOD when the flood bound hits.

        Timestamps must be monotonic and come from the caller's clock (the
        gate never reads the real clock — tests pass explicit ``ts`` values).
        """
        norm = self.normalized(text)
        if not norm:
            return None
        recent: deque[tuple[str, float]] = deque(
            (n, t) for n, t in self.recent if ts - t <= self.window_seconds
        )
        identical = sum(1 for n, _ in recent if n == norm)
        if identical >= self.flood_count - 1:
            # The window already holds enough identical texts; the engine
            # rejects before persisting this one, so it never enters history.
            return ReasonCode.REPLAY_FLOOD
        recent.append((norm, ts))
        self.recent = recent
        return None


def check_replay_flood(
    text: str,
    window: ReplayWindow | None,
    ts: float,
) -> list[ReasonCode]:
    """Reject identical normalized text repeated within the time window.

    ``window=None`` disables the check (caller has no replay state); ``ts``
    is explicit so the function stays deterministic and testable.
    """
    if window is None:
        return []
    if window.observe(text, ts) is ReasonCode.REPLAY_FLOOD:
        return [ReasonCode.REPLAY_FLOOD]
    return []


def _all_caps_ratio(text: str) -> float:
    alpha = _CAP_PUNCT_RE.findall(text)
    if not alpha:
        return 0.0
    caps = sum(1 for token in alpha for char in token if char.isupper())
    letters = sum(1 for token in alpha for char in token if char.isalpha())
    if letters < _MIN_ALPHA_LENGTH:
        return 0.0
    return caps / letters


def check_spam(text: str | None) -> list[ReasonCode]:
    """Combined conservative spam heuristics (see module docstring).

    No single signal rejects: each contributing heuristic needs a second
    signal or an extreme threshold, and known spam markers are the only
    standalone triggers.
    """
    if text is None:
        return []
    if _SPAM_MARKER_RE.search(text):
        return [ReasonCode.SPAM]
    urls = len(_URL_RE.findall(text))
    if urls > _MAX_URLS:
        return [ReasonCode.SPAM]
    signals = 0
    if _all_caps_ratio(text) > _MAX_ALL_CAPS_RATIO:
        signals += 1
    if max((len(run) for run in _EMOJI_RE.findall(text)), default=0) > _MIN_EMOJI_RUN:
        signals += 1
    if max((len(run) for run in _CAP_PUNCT_RE.findall(text)), default=0) > _MAX_PUNCT_RUN:
        signals += 1
    if _REPEATED_TOKEN_RE.search(text):
        signals += 1
    if signals >= 2:
        return [ReasonCode.SPAM]
    return []
