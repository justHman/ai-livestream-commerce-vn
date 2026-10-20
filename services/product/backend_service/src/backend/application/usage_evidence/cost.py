"""M7.3: per-session cost evidence (``ai.cost.reported``). Evidence of quantities, never money.

``cost_meter`` is a process singleton, DEFAULT OFF. Every ``record_*`` is synchronous,
never awaits, never does I/O and never raises: a failure here must not touch the live
path. Samples wait in a bounded in-memory buffer and the sender flushes them into the
durable ``usage_evidence_outbox`` (kind ``cost:<COST_KIND>``). No price, ``cost_usd`` or
credit exists anywhere in this module (BR-PRICING-001/002).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.application.execution_contract import ExecutionIdentity

logger = logging.getLogger(__name__)

LLM, TTS, AVATAR = "LLM", "TTS", "AVATAR"
COST_KINDS = (LLM, TTS, AVATAR)
_MAX_BOUND = 256  # bound sessions / open avatar sessions / attempt counters
_MAX_ATTEMPT_KEYS = 4096


@dataclass(frozen=True)
class CostSample:
    identity: ExecutionIdentity
    cost_kind: str
    cost_key: str  # unit of measure, unique inside (identity, cost_kind)
    occurred_at: datetime
    provider: str = ""
    model: str = ""
    usage_source: str = "measured"  # provider | measured | unknown
    fields: dict[str, Any] = field(default_factory=dict)  # kind-specific quantities
    attempts: int = 0  # sender flush retries (not part of the identity)


class CostMeter:
    def __init__(self) -> None:
        self.enabled = False
        self._buf: deque[CostSample] = deque(maxlen=5000)
        self._ids: OrderedDict[str, ExecutionIdentity] = OrderedDict()
        self._open: OrderedDict[tuple[str, str], tuple[float, datetime]] = OrderedDict()
        self._attempts: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()
        self.dropped = 0  # buffer overflow
        self.unbound = 0  # no identity for the session: shown as "unknown", not zero
        self.lost = 0  # could not be written to the outbox
        self.conflicts = 0  # same semantic key, different bytes

    def configure(self, *, enabled: bool, buffer: int = 5000) -> None:
        self.enabled = bool(enabled)
        self._buf = deque(self._buf, maxlen=max(1, buffer))

    # -- session binding (control plane, at start / stop) -------------------

    def bind(self, session_id: str, identity: ExecutionIdentity) -> None:
        with self._lock:
            self._ids[session_id] = identity
            while len(self._ids) > _MAX_BOUND:
                self._ids.popitem(last=False)

    def unbind(self, session_id: str) -> None:
        with self._lock:
            self._ids.pop(session_id, None)

    # -- producers (speech path safe) ----------------------------------------

    def _push(
        self,
        session_id: str,
        kind: str,
        key: str,
        *,
        provider: str = "",
        model: str = "",
        usage_source: str = "measured",
        **fields: Any,
    ) -> None:
        if not self.enabled:
            return
        try:
            identity = self._ids.get(session_id)
            if identity is None:
                self.unbound += 1
                return
            if len(self._buf) == self._buf.maxlen:
                self.dropped += 1
            self._buf.append(
                CostSample(
                    identity,
                    kind,
                    key,
                    datetime.now(timezone.utc),
                    provider,
                    model,
                    usage_source,
                    fields,
                )
            )
        except Exception as exc:  # never reaches the live path
            self.lost += 1
            logger.warning("cost evidence record failed error_type=%s", type(exc).__name__)

    @property
    def include_usage(self) -> bool:
        """Ask the provider for stream token usage only while the producer is on."""
        return self.enabled

    def record_llm(
        self,
        session_id: str,
        *,
        call_id: str,
        provider: str,
        model: str,
        input_tokens: int | None,
        output_tokens: int | None,
    ) -> None:
        """Tokens only as the provider reported them; otherwise null + usage_source unknown."""
        known = input_tokens is not None and output_tokens is not None
        self._push(
            session_id,
            LLM,
            call_id,
            provider=provider,
            model=model,
            usage_source="provider" if known else "unknown",
            input_tokens=input_tokens if known else None,
            output_tokens=output_tokens if known else None,
        )

    def next_attempt(self, session_id: str, utterance_id: str, phrase_seq: int) -> int:
        key = f"{session_id}\x00{utterance_id}\x00{phrase_seq}"
        with self._lock:
            n = self._attempts.get(key, 0) + 1
            self._attempts[key] = n
            while len(self._attempts) > _MAX_ATTEMPT_KEYS:
                self._attempts.popitem(last=False)
            return n

    def record_tts(
        self,
        session_id: str,
        *,
        utterance_id: str,
        phrase_seq: int,
        attempt: int,
        provider: str,
        characters: int,
        audio_ms: int,
        synth_ms: int,
    ) -> None:
        """One row per TTS call (phrase), keyed ``utterance_id:phrase_seq:attempt``."""
        self._push(
            session_id,
            TTS,
            f"{utterance_id}:{phrase_seq}:{attempt}",
            provider=provider,
            characters=max(0, int(characters)),
            audio_ms=max(0, int(audio_ms)),
            synth_ms=max(0, int(synth_ms)),
        )

    def avatar_open(self, session_id: str, provider_session_id: str) -> None:
        """A provider avatar session began (start or rollover). Identity is resolved at close."""
        if not (self.enabled and provider_session_id):
            return
        with self._lock:
            self._open[(session_id, provider_session_id)] = (
                time.monotonic(),
                datetime.now(timezone.utc),
            )
            while len(self._open) > _MAX_BOUND:
                self._open.popitem(last=False)

    def avatar_close(self, session_id: str, provider_session_id: str) -> None:
        """The provider avatar session ended (rollover end or stop). Idempotent."""
        if not provider_session_id:
            return
        with self._lock:
            opened = self._open.pop((session_id, provider_session_id), None)
        if opened is None:
            return
        mono, started = opened
        self._push(
            session_id,
            AVATAR,
            provider_session_id,
            provider="lemonslice",
            seconds=round(max(0.0, time.monotonic() - mono), 3),
            started_at=started,
            ended_at=datetime.now(timezone.utc),
            provider_session_id=provider_session_id,
        )

    # -- sender side -------------------------------------------------------

    def drain(self, limit: int) -> list[CostSample]:
        out: list[CostSample] = []
        while self._buf and len(out) < limit:
            try:
                out.append(self._buf.popleft())
            except IndexError:
                break
        return out

    def requeue(self, sample: CostSample) -> None:
        """One bounded retry after an outbox write failure; then it counts as lost."""
        if sample.attempts >= 2:
            self.lost += 1
            return
        self._buf.appendleft(
            CostSample(
                sample.identity,
                sample.cost_kind,
                sample.cost_key,
                sample.occurred_at,
                sample.provider,
                sample.model,
                sample.usage_source,
                sample.fields,
                sample.attempts + 1,
            )
        )

    def health(self) -> dict[str, Any]:
        return {
            "cost_enabled": self.enabled,
            "cost_buffered": len(self._buf),
            "cost_dropped": self.dropped,
            "cost_unbound": self.unbound,
            "cost_lost": self.lost,
            "cost_conflicts": self.conflicts,
        }


def new_call_id() -> str:
    return uuid.uuid4().hex


cost_meter = CostMeter()
