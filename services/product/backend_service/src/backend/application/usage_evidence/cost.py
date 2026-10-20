"""M7.3: per-session cost evidence (``ai.cost.reported``). Evidence of quantities, never money.

``cost_meter`` is a process singleton, DEFAULT OFF. Every ``record_*`` is synchronous,
never awaits, never does I/O and never raises: a failure here must not touch the live
path. Samples wait in a bounded in-memory buffer and the sender flushes them into the
durable ``usage_evidence_outbox`` (kind ``cost:<COST_KIND>``), retrying with backoff while
the outbox is unwell.

ACCEPTED CEILING: a crash loses what is still in the buffer (about one sender pass, ~2 s,
longer only while the outbox is failing); an overflow drops the oldest. Every sample that
must be dropped is logged ONCE (``cost_evidence_lost``: key, tenant, kind, reason) and
counted in ``cost_lost`` so it can be reconciled by hand. No price, ``cost_usd`` or credit
exists anywhere in this module (BR-PRICING-001/002).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.application.execution_contract import ExecutionIdentity

logger = logging.getLogger(__name__)

LLM, TTS, AVATAR = "LLM", "TTS", "AVATAR"
COST_KINDS = (LLM, TTS, AVATAR)
_MAX_BOUND = 4096  # bound identities / open avatar sessions
_MAX_ATTEMPT_KEYS = 4096
_OPEN_MAX_AGE_S = 6 * 3600.0  # an avatar record older than this is settled as derived


@dataclass(frozen=True)
class CostSample:
    identity: ExecutionIdentity
    cost_kind: str
    cost_key: str  # unit of measure, unique inside (identity, cost_kind)
    occurred_at: datetime
    provider: str = ""
    model: str = ""
    usage_source: str = "measured"  # provider | measured | derived | unknown
    fields: dict[str, Any] = field(default_factory=dict)  # kind-specific quantities


class CostMeter:
    def __init__(self) -> None:
        self.enabled = False
        self._buf: deque[CostSample] = deque()
        self._cap = 5000
        self._buf_lock = threading.Lock()
        self._ids: OrderedDict[str, ExecutionIdentity] = OrderedDict()
        # provider_session_id -> (session_id, monotonic start, wall start, start is derived)
        self._open: OrderedDict[str, tuple[str, float, datetime, bool]] = OrderedDict()
        self._unbound_pending: set[str] = set()  # unbind requested while an avatar was open
        self._attempts: OrderedDict[str, int] = OrderedDict()
        self._lock = threading.Lock()
        self.dropped = 0  # buffer overflow
        self.unbound = 0  # no identity for the session: shown as "unknown", not zero
        self.lost = 0  # dropped for good (each one logged once as cost_evidence_lost)
        self.conflicts = 0  # same semantic key, different bytes
        self.evicted = 0  # identity / open avatar record removed to make room (an error)

    def configure(self, *, enabled: bool, buffer: int = 5000) -> None:
        self.enabled = bool(enabled)
        self._cap = max(1, buffer)

    # -- session binding (control plane, at start / stop) -------------------

    def bind(self, session_id: str, identity: ExecutionIdentity) -> None:
        with self._lock:
            self._ids[session_id] = identity
            self._ids.move_to_end(session_id)
            if len(self._ids) > _MAX_BOUND:
                self._trim_ids()

    def unbind(self, session_id: str) -> None:
        """Forget the identity, unless a provider avatar session of it is still open."""
        with self._lock:
            if any(rec[0] == session_id for rec in self._open.values()):
                self._unbound_pending.add(session_id)
            else:
                self._ids.pop(session_id, None)

    def _trim_ids(self) -> None:
        """Caller holds the lock. Identities of sessions with an open avatar go last."""
        busy = {rec[0] for rec in self._open.values()}
        for sid in list(self._ids):
            if len(self._ids) <= _MAX_BOUND:
                return
            if sid not in busy:
                del self._ids[sid]
                self.evicted += 1
                logger.error("cost_evidence_evicted what=identity session=%s", sid)
        while len(self._ids) > _MAX_BOUND:
            sid, _ = self._ids.popitem(last=False)
            self.evicted += 1
            logger.error("cost_evidence_evicted what=identity_in_use session=%s", sid)

    def lose(self, sample: CostSample, reason: str) -> None:
        """The one reconciliation line for a sample that had to be dropped for good."""
        self.lost += 1
        logger.error(
            "cost_evidence_lost key=%s tenant=%s kind=%s reason=%s",
            sample.cost_key,
            sample.identity.tenant_id,
            sample.cost_kind,
            reason,
        )

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
            sample = CostSample(
                identity,
                kind,
                key,
                datetime.now(timezone.utc),
                provider,
                model,
                usage_source,
                fields,
            )
            with self._buf_lock:  # check + append are one step: no silent deque eviction
                full = len(self._buf) >= self._cap
                if not full:
                    self._buf.append(sample)
            if full:  # only a NEW sample is refused; queued/in-flight ones are never evicted
                self.dropped += 1
                self.lose(sample, "buffer_full")
        except Exception as exc:  # never reaches the live path
            self.lost += 1
            logger.error(
                "cost_evidence_lost reason=record_failed error_type=%s", type(exc).__name__
            )

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

    def avatar_open(
        self, session_id: str, provider_session_id: str, *, derived: bool = False
    ) -> None:
        """A provider avatar session began (start, rollover, or a reaped late session).

        Identity is resolved at close (the session is bound only after start returns).
        ``derived`` marks a start time we could not observe.
        """
        if not (self.enabled and provider_session_id):
            return
        with self._lock:
            self._open[provider_session_id] = (
                session_id,
                time.monotonic(),
                datetime.now(timezone.utc),
                derived,
            )
            expired = self._trim_open() if len(self._open) > _MAX_BOUND else []
        for psid, rec in expired:  # emitted outside the lock: _push/_emit_avatar take it again
            self._emit_avatar(psid, rec, confirmed=False, extra_s=0.0)

    def _trim_open(self) -> list[tuple[str, tuple[str, float, datetime, bool]]]:
        """Caller holds the lock. Returns the expired records to settle (the caller emits them
        after releasing the lock); only if none can go is one evicted, as an error."""
        now = time.monotonic()
        expired = []
        for psid, rec in list(self._open.items()):
            if len(self._open) <= _MAX_BOUND:
                break
            if now - rec[1] > _OPEN_MAX_AGE_S:
                del self._open[psid]
                expired.append((psid, rec))
        while len(self._open) > _MAX_BOUND:
            psid, _ = self._open.popitem(last=False)
            self.evicted += 1
            logger.error("cost_evidence_evicted what=avatar_open provider_session=%s", psid)
        return expired

    def avatar_close(
        self, provider_session_id: str, *, confirmed: bool = True, extra_s: float = 0.0
    ) -> None:
        """Provider-CONFIRMED end (``confirmed=False`` only for a final settle). Idempotent.

        Unconfirmed: ``usage_source='derived'`` and the end is the moment we know for sure
        plus ``extra_s`` (the provider idle timeout: the latest it may still have been running).
        """
        if not provider_session_id:
            return
        with self._lock:
            rec = self._open.pop(provider_session_id, None)
        if rec is not None:
            self._emit_avatar(provider_session_id, rec, confirmed=confirmed, extra_s=extra_s)

    def avatar_settle_all(self, extra_s: float) -> None:
        """Shutdown: every still-open record is settled as derived (end = now + idle bound)."""
        with self._lock:
            records, self._open = list(self._open.items()), OrderedDict()
        for psid, rec in records:
            self._emit_avatar(psid, rec, confirmed=False, extra_s=extra_s)

    def _emit_avatar(
        self, psid: str, rec: tuple[str, float, datetime, bool], *, confirmed: bool, extra_s: float
    ) -> None:
        session_id, mono, started, derived_start = rec
        extra = 0.0 if confirmed else max(0.0, float(extra_s))
        self._push(
            session_id,
            AVATAR,
            psid,
            provider="lemonslice",
            usage_source="measured" if confirmed and not derived_start else "derived",
            seconds=round(max(0.0, time.monotonic() - mono) + extra, 3),
            started_at=started,
            ended_at=datetime.now(timezone.utc) + timedelta(seconds=extra),
            provider_session_id=psid,
        )
        with self._lock:
            if session_id in self._unbound_pending and not any(
                r[0] == session_id for r in self._open.values()
            ):
                self._unbound_pending.discard(session_id)
                self._ids.pop(session_id, None)

    # -- sender side -------------------------------------------------------

    def drain(self, limit: int) -> list[CostSample]:
        out: list[CostSample] = []
        with self._buf_lock:
            while self._buf and len(out) < limit:
                out.append(self._buf.popleft())
        return out

    def requeue(self, sample: CostSample) -> None:
        """Back to the FRONT after an outbox failure. Always succeeds: a sample that was once
        accepted keeps its place even if producers filled the buffer meanwhile (the overshoot
        is bounded by one drain batch); only NEW samples are refused when full."""
        with self._buf_lock:
            self._buf.appendleft(sample)

    def health(self) -> dict[str, Any]:
        return {
            "cost_enabled": self.enabled,
            "cost_buffered": len(self._buf),
            "cost_dropped": self.dropped,
            "cost_unbound": self.unbound,
            "cost_lost": self.lost,
            "cost_conflicts": self.conflicts,
            "cost_evicted": self.evicted,
            "cost_avatar_open": len(self._open),
        }


def new_call_id() -> str:
    return uuid.uuid4().hex


cost_meter = CostMeter()
