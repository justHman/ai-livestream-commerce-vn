"""Bounded social replies, separate from product demand and approved facts."""

from __future__ import annotations

import os
import re
import unicodedata
import threading
from functools import wraps
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from backend.application.safety_gate.text_matching import fold_text


def enabled() -> bool:
    return os.environ.get("SOCIAL_REPLY_ENABLED", "").lower() in {"1", "true", "yes", "on"}


def validated_host_name(value: str) -> str:
    """Names are data, never instructions, URLs or TTS markup."""
    value = unicodedata.normalize("NFC", value)
    if any(unicodedata.category(c).startswith("C") or c in "\r\n\t" for c in value):
        raise ValueError("invalid host name")
    value = " ".join(value.split())
    if not 1 <= len(value) <= 48 or not any(c.isalpha() for c in value):
        raise ValueError("invalid host name")
    if any(
        not (c.isalnum() or unicodedata.category(c).startswith("M") or c in " '-’") for c in value
    ):
        raise ValueError("invalid host name")
    from backend.application.script_authoring.answer_context import _abuse_patterns
    from backend.application.safety_gate.resources import match_curated

    if match_curated(value, _abuse_patterns()) or re.search(
        r"\b(?:ignore|instructions?|system|prompt|http|www|bo qua|chi dan|menh lenh)\b",
        fold_text(value),
    ):
        raise ValueError("invalid host name")
    return value


_HOST = r"(?:chi|anh|em|ban|bn|mc|nguoi dan|co|chu shop|shop)"
_TAIL = r"(?:\s+(?:a|ah|oi|nha|nhe|na|luon|qua|wa|lam))*"
_PATTERNS = (
    ("greeting", rf"(?:xin\s+)?(?:chao|hello|hi|hlo)(?:\s+{_HOST})?{_TAIL}"),
    ("thanks", rf"(?:cam on|thank you|thanks|tks)(?:\s+{_HOST})?{_TAIL}"),
    ("compliment", rf"{_HOST}\s+(?:xinh|dep|de thuong|duyen dang){_TAIL}"),
    ("wellbeing", rf"{_HOST}\s+(?:co\s+)?khoe\s+(?:khong|ko|k|hong){_TAIL}"),
    ("name", rf"{_HOST}\s+(?:co\s+)?ten\s+(?:la\s+)?(?:gi|j){_TAIL}"),
    ("identity", rf"{_HOST}\s+la\s+ai{_TAIL}"),
)


def social_intent(text: str) -> str | None:
    # Whole-comment matches preserve commerce priority and reject mixed abuse.
    normalized = " ".join(re.findall(r"[^\W_]+", fold_text(text)))
    for intent, pattern in _PATTERNS:
        if re.fullmatch(pattern, normalized):
            return intent
    return None


def replies(intent: str, host_name: str = "") -> tuple[str, ...]:
    if intent in {"name", "identity"}:
        return (
            f"Mình là {host_name}, người dẫn AI của phiên live này. Rất vui được gặp bạn nha!"
            if host_name
            else "Mình là người dẫn AI của phiên live này. Rất vui được gặp bạn nha!",
        )
    return {
        "greeting": (
            "Chào bạn, rất vui được gặp bạn trong phiên live hôm nay nha!",
            "Xin chào bạn! Bạn muốn hỏi gì về sản phẩm thì nhắn cho mình nhé.",
            "Chào bạn ghé chơi! Cùng theo dõi sản phẩm với mình nha.",
        ),
        "thanks": (
            "Cảm ơn bạn đã ghé tương tác cùng mình nha!",
            "Rất vui được đồng hành cùng bạn! Cần hỏi sản phẩm cứ nhắn mình nhé.",
            "Cảm ơn bạn nhiều nha! Mình tiếp tục giới thiệu sản phẩm cho mọi người nhé.",
        ),
        "compliment": (
            "Cảm ơn lời khen dễ thương của bạn nha!",
            "Bạn dễ thương quá, cảm ơn bạn đã cổ vũ mình nhé!",
            "Cảm ơn bạn nhiều! Cùng tiếp tục xem sản phẩm với mình nha.",
        ),
        "wellbeing": (
            "Cảm ơn bạn quan tâm, mình sẵn sàng đồng hành cùng mọi người nha!",
            "Cảm ơn bạn đã hỏi thăm! Mình sẵn sàng hỗ trợ mọi người về sản phẩm nhé.",
            "Cảm ơn lời hỏi thăm của bạn nha! Cùng theo dõi phiên live với mình nhé.",
        ),
    }.get(intent, ())


@dataclass
class PendingSocial:
    intent: str
    created: float
    members: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    viewers: set[str] = field(default_factory=set)


def _locked(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


class SocialQueue:
    """One pending group; dispatch quota is reserved synchronously, before LLM/TTS."""

    def __init__(self):
        self._lock = threading.RLock()
        self.pending: PendingSocial | None = None
        self.started: deque[float] = deque(maxlen=3)
        self.viewers: dict[str, float] = {}
        self.serial = 0
        self.dispatched_serial = 0
        self.dispatched_at: float | None = None
        self.reserved_viewers: set[str] = set()

    @_locked
    def add(self, intent, comment_id, event_id, viewer, now) -> str | None:
        self.viewers = {key: ts for key, ts in self.viewers.items() if now - ts < 60}
        if viewer in self.viewers:
            return "social_cooldown"
        if self.started and now - self.started[-1] < 20:
            return "social_cooldown"
        if self.pending is None:
            self.pending = PendingSocial(intent, now)
        item = self.pending
        if item.intent != intent or now - item.created > 3 or len(item.members) >= 200:
            return "social_capacity"
        if comment_id not in item.members:
            item.members.append(comment_id)
            item.events.append(event_id)
            item.viewers.add(viewer)
        return None

    @_locked
    def reserve(self, now) -> tuple[PendingSocial | None, str | None]:
        item, self.pending = self.pending, None
        if item is None:
            return None, None
        if not enabled():
            return item, "social_disabled"
        if now - item.created >= 15:
            return item, "social_expired"
        while self.started and now - self.started[0] >= 60:
            self.started.popleft()
        if self.started and (now - self.started[-1] < 20 or len(self.started) >= 3):
            return item, "social_cooldown"
        self.started.append(now)
        for viewer in item.viewers:
            self.viewers[viewer] = now
        self.serial += 1
        self.reserved_viewers = set(item.viewers)
        return item, None

    @_locked
    def dispatch(self, serial: int, now: float) -> str | None:
        if serial != self.serial:
            return "social_expired"
        if self.dispatched_serial != serial and self.dispatched_at is not None:
            if now - self.dispatched_at < 20:
                return "social_cooldown"
        self.dispatched_serial, self.dispatched_at = serial, now
        self.started[-1] = now  # anchor reservations and viewer limits to actual media start
        for viewer in self.reserved_viewers:
            self.viewers[viewer] = now
        return None


@dataclass
class SocialPermit:
    queue: SocialQueue
    serial: int
    deadline: float
    clock: Callable[[], float]
    priority_pending: Callable[[], bool] = lambda: False
    media_started: bool = False

    def check(self):
        from backend.application.script_authoring.approved_speech import SpeechRejected

        if not enabled():
            raise SpeechRejected("social_disabled")
        if not self.media_started and self.clock() >= self.deadline:
            raise SpeechRejected("social_expired")

    def dispatch(self, *, media=False):
        from backend.application.script_authoring.approved_speech import SpeechRejected

        self.check()
        if not self.media_started:
            if self.priority_pending():
                raise SpeechRejected("social_capacity")
            reason = self.queue.dispatch(self.serial, self.clock())
            if reason:
                raise SpeechRejected(reason)
            if media:
                self.media_started = True
