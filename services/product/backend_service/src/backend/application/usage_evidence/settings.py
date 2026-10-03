"""Settings (default off, fail closed) and the bounded in-memory COGS buffer."""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping
from urllib.parse import urlsplit

from backend.application.execution_contract import ExecutionIdentity

from . import envelope

ENV_PREFIX = "USAGE_EVIDENCE_"
EXACT_PATH = "/webhooks/ai/events"  # no trailing slash: the API router does not strip it
_TRUE = ("1", "true", "yes", "on")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


def _flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(ENV_PREFIX + name, "0").strip().lower() in _TRUE


def _num(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        value = float(env.get(ENV_PREFIX + name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


@dataclass(frozen=True)
class UsageEvidenceSettings:
    enabled: bool = False
    url: str = ""
    secret: str = field(default="", repr=False)  # = the API AI_WEBHOOK_SECRET; never printed
    producer_id: str = "runtime"
    # Gates for kinds whose dependency is not frozen. Off by default.
    gate_first_broadcast: bool = False
    gate_media_health: bool = False
    gate_hold: bool = False
    # Implementation defaults (not Business Rules).
    backoff_base: float = 1.0
    backoff_cap: float = 300.0
    sweep_age: float = 60.0
    sweep_interval: float = 30.0
    poll_seconds: float = 2.0
    lease_seconds: float = 60.0
    http_timeout: float = 10.0
    flush_timeout: float = 5.0
    cogs_buffer: int = 1000
    # Per-sweep wall-clock budget, and the backoff for a session whose lock is busy.
    sweep_budget: float = 20.0
    busy_backoff_base: float = 5.0
    busy_backoff_cap: float = 300.0
    # Delivered / permanently-failed / discarded rows are deleted after this many days.
    retention_days: float = 14.0
    retention_batch: int = 500

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "UsageEvidenceSettings":
        env = os.environ if env is None else env
        return cls(
            enabled=_flag(env, "ENABLED"),
            url=env.get(ENV_PREFIX + "URL", "").strip(),
            secret=env.get(ENV_PREFIX + "SECRET", "").strip(),
            producer_id=env.get(ENV_PREFIX + "PRODUCER_ID", "runtime").strip() or "runtime",
            gate_first_broadcast=_flag(env, "GATE_FIRST_BROADCAST"),
            gate_media_health=_flag(env, "GATE_MEDIA_HEALTH"),
            gate_hold=_flag(env, "GATE_HOLD"),
            backoff_base=_num(env, "BACKOFF_BASE_SECONDS", 1.0),
            backoff_cap=_num(env, "BACKOFF_CAP_SECONDS", 300.0),
            sweep_age=_num(env, "SWEEP_AGE_SECONDS", 60.0),
            cogs_buffer=int(_num(env, "COGS_BUFFER", 1000)),
            sweep_budget=_num(env, "SWEEP_BUDGET_SECONDS", 20.0),
            busy_backoff_base=_num(env, "BUSY_BACKOFF_BASE_SECONDS", 5.0),
            busy_backoff_cap=_num(env, "BUSY_BACKOFF_CAP_SECONDS", 300.0),
            retention_days=_num(env, "RETENTION_DAYS", 14.0),
        )

    @property
    def configured(self) -> bool:
        """Flag, secret and the exact receiver URL (https, or http to exact loopback)."""
        if not (self.enabled and self.secret):
            return False
        try:
            url = urlsplit(self.url)
            host = url.hostname
            url.port  # noqa: B018 - raises ValueError for a malformed port
        except ValueError:
            return False
        if not host or url.username is not None or url.password is not None:
            return False
        if url.query or url.fragment or url.path != EXACT_PATH:
            return False
        return url.scheme == "https" or (url.scheme == "http" and host in _LOOPBACK)

    @property
    def gates(self) -> envelope.Gates:
        return envelope.Gates(
            first_broadcast=self.gate_first_broadcast,
            media_health=self.gate_media_health,
            hold=self.gate_hold,
        )

    def capabilities(self) -> tuple[str, ...]:
        """Computed, never constant: sub-capabilities only while their gate is on."""
        caps = [envelope.CAP]
        if self.gate_first_broadcast:
            caps.append(envelope.CAP_FIRST_BROADCAST)
        if self.gate_media_health:
            caps.append(envelope.CAP_MEDIA_HEALTH)
        if self.gate_hold:
            caps.append(envelope.CAP_HOLD)
        return tuple(caps)


@dataclass(frozen=True)
class CogsSample:
    identity: ExecutionIdentity
    sample_id: str
    occurred_at: datetime
    model_id: str
    input_tokens: int
    output_tokens: int


class CogsBuffer:
    """Bounded, non-blocking, drop-oldest. Append never awaits and never does I/O.

    COGS is internal (never billed): unflushed samples may be lost on a crash.
    """

    def __init__(self, maxlen: int) -> None:
        self._items: deque[CogsSample] = deque(maxlen=max(1, maxlen))
        self.dropped = 0
        self.lost = 0

    def append(self, sample: CogsSample) -> None:
        if len(self._items) == self._items.maxlen:
            self.dropped += 1  # the deque drops the oldest
        self._items.append(sample)

    def drain(self, limit: int) -> list[CogsSample]:
        out: list[CogsSample] = []
        while self._items and len(out) < limit:
            out.append(self._items.popleft())
        return out

    def __len__(self) -> int:
        return len(self._items)
