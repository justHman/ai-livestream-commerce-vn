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
import contextvars
import queue
import threading
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.application.execution_contract import ExecutionIdentity

logger = logging.getLogger(__name__)

# tenant / business session / generation announced by the control plane just before the backend
# starts, so an avatar opened during start already knows its identity (the runtime session id
# is the backend's own session id, filled in at avatar_open).
_expected: contextvars.ContextVar[tuple[str, str, str] | None] = contextvars.ContextVar(
    "cost_expected_identity", default=None
)

LLM, TTS, AVATAR = "LLM", "TTS", "AVATAR"
COST_KINDS = (LLM, TTS, AVATAR)
_LOG_BURST = 20  # lines per reason before throttling to _LOG_RATE per second
_LOG_RATE = 1.0
_LOG_QUEUE: queue.Queue[str] = queue.Queue(maxsize=1000)
_log_writer_lock = threading.Lock()
_log_writer: threading.Thread | None = None
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


def _write_logs() -> None:
    """Daemon thread: the only place a log handler runs, so a slow or failing handler can
    never block or break a live producer thread."""
    while True:
        message = _LOG_QUEUE.get()
        try:
            logger.error("%s", message)
        except Exception:  # noqa: BLE001 - a broken handler must not kill the writer
            pass
        finally:
            _LOG_QUEUE.task_done()


def flush_logs(timeout: float = 2.0) -> bool:
    """Wait until queued cost log lines were handed to the handlers (tests, shutdown)."""
    deadline = time.monotonic() + timeout
    while _LOG_QUEUE.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(0.005)
    return not _LOG_QUEUE.unfinished_tasks


class CostMeter:
    def __init__(self) -> None:
        self.enabled = False
        self._buf: deque[CostSample] = deque()
        self._cap = 5000
        self._buf_lock = threading.Lock()
        self._ids: OrderedDict[str, ExecutionIdentity] = OrderedDict()
        # provider_session_id -> (session_id, monotonic start, wall start, start is derived,
        #                         identity known at open or None)
        self._open: OrderedDict[
            str, tuple[str, float, datetime, bool, ExecutionIdentity | None]
        ] = OrderedDict()
        self._unbound_pending: set[str] = set()  # unbind requested while an avatar was open
        self._attempts: OrderedDict[str, int] = OrderedDict()
        self._log_buckets: dict[str, tuple[float, float, int]] = {}
        self._log_lock = threading.Lock()
        self.log_suppressed = 0  # lines not written (rate limit / full queue); counters stay exact
        self._lock = threading.Lock()
        self.dropped = 0  # buffer overflow
        self.unbound = 0  # no identity for the session: shown as "unknown", not zero
        self.lost = 0  # dropped for good (each one logged once as cost_evidence_lost)
        self.conflicts = 0  # same semantic key, different bytes
        self._armed = False  # ever enabled: until then EVERY entry point is an absolute no-op
        self.reopened = 0  # avatar_open on a provider session that was still open
        self._closed = False  # shut down: late samples are logged as lost, never silent
        self.avatar_idle_bound_s = 0.0  # provider idle timeout: the end bound of a derived row
        self.evicted = 0  # identity / open avatar record removed to make room (an error)

    def configure(self, *, enabled: bool, buffer: int = 5000) -> None:
        with self._buf_lock:
            self.enabled = bool(enabled)
            self._armed = self._armed or self.enabled
            self._closed = False
            self._cap = max(1, buffer)

    # -- session binding (control plane, at start / stop) -------------------

    def expect(self, tenant_id: str, business_session_id: str, generation: str):
        """Announce the identity of the session about to start (contextvar; returns a token)."""
        if not self._armed:
            return None
        return _expected.set((tenant_id, business_session_id, generation))

    def expect_reset(self, token: Any) -> None:
        if token is not None:
            _expected.reset(token)

    def bind(self, session_id: str, identity: ExecutionIdentity) -> None:
        if not self._armed:
            return
        with self._lock:
            self._ids[session_id] = identity
            self._ids.move_to_end(session_id)
            forced = self._trim_ids() if len(self._ids) > _MAX_BOUND else []
        for psid, rec, ident in forced:  # outside the lock: the identity left, the row must not
            self._emit_avatar(
                psid, rec, confirmed=False, extra_s=self.avatar_idle_bound_s, identity=ident
            )

    def unbind(self, session_id: str) -> None:
        """Forget the identity, unless a provider avatar session of it is still open."""
        if not self._armed:
            return
        with self._lock:
            if any(rec[0] == session_id for rec in self._open.values()):
                self._unbound_pending.add(session_id)
            else:
                self._ids.pop(session_id, None)

    def _trim_ids(
        self,
    ) -> list[
        tuple[str, tuple[str, float, datetime, bool, ExecutionIdentity | None], ExecutionIdentity]
    ]:
        """Caller holds the lock. Identities of sessions with an open avatar go last; if one
        must go, its open avatar records are returned (with the identity) to be settled as
        derived by the caller outside the lock, so the later close is not an unreported orphan."""
        busy = {rec[0] for rec in self._open.values()}
        for sid in list(self._ids):
            if len(self._ids) <= _MAX_BOUND:
                return []
            if sid not in busy:
                del self._ids[sid]
                self.evicted += 1
                self._safe_log(
                    "evicted:identity", f"cost_evidence_evicted what=identity session={sid}"
                )
        forced = []
        while len(self._ids) > _MAX_BOUND:
            sid, ident = self._ids.popitem(last=False)
            self.evicted += 1
            self._safe_log(
                "evicted:identity_in_use",
                f"cost_evidence_evicted what=identity_in_use session={sid}",
            )
            for psid, rec in list(self._open.items()):
                if rec[0] == sid:
                    del self._open[psid]
                    forced.append((psid, rec, ident))
            self._unbound_pending.discard(sid)
        return forced

    def _safe_log(self, reason: str, message: str) -> None:
        """The ONLY logging path of the meter. Never raises, never blocks, rate limited per
        reason (burst, then ~1 line/s with the suppressed count on the next line). The
        counters (lost, dropped, evicted...) are incremented by the callers for every sample."""
        try:
            now = time.monotonic()
            with self._log_lock:
                tokens, last, suppressed = self._log_buckets.get(reason, (_LOG_BURST, now, 0))
                tokens = min(float(_LOG_BURST), tokens + (now - last) * _LOG_RATE)
                if tokens < 1.0:
                    self._log_buckets[reason] = (tokens, now, suppressed + 1)
                    self.log_suppressed += 1
                    return
                self._log_buckets[reason] = (tokens - 1.0, now, 0)
            if suppressed:
                message += f" (+{suppressed} similar lines suppressed)"
            global _log_writer
            if _log_writer is None:
                with _log_writer_lock:
                    if _log_writer is None:
                        _log_writer = threading.Thread(
                            target=_write_logs, name="cost-evidence-log", daemon=True
                        )
                        _log_writer.start()
            _LOG_QUEUE.put_nowait(message)
        except Exception:  # noqa: BLE001 - logging can never reach the live path
            self.log_suppressed += 1

    def lose(self, sample: CostSample, reason: str) -> None:
        """The one reconciliation line for a sample that had to be dropped for good."""
        self.lost += 1
        self._safe_log(
            "lost:" + reason,
            f"cost_evidence_lost key={sample.cost_key} tenant={sample.identity.tenant_id} "
            f"kind={sample.cost_kind} reason={reason}",
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
        _identity: ExecutionIdentity | None = None,
        **fields: Any,
    ) -> None:
        if not self._armed:
            return
        try:
            identity = _identity or self._ids.get(session_id)
            if identity is None:
                if self.enabled:
                    self.unbound += 1  # intended: a session without execution_contract
                else:
                    self._late(key, "unknown", kind)
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
            with self._buf_lock:  # enabled-check + capacity + append are ONE step
                if not self.enabled:
                    state = "closed"
                elif len(self._buf) >= self._cap:
                    state = "full"
                else:
                    state = "ok"
                    self._buf.append(sample)
            if state == "closed":
                self._late(key, identity.tenant_id, kind)
            elif state == "full":  # only a NEW sample is refused; queued ones are never evicted
                self.dropped += 1
                self.lose(sample, "buffer_full")
        except Exception as exc:  # never reaches the live path
            self.lost += 1
            self._safe_log(
                "lost:record_failed",
                f"cost_evidence_lost reason=record_failed error_type={type(exc).__name__}",
            )

    def _late(self, key: str, tenant: str, kind: str) -> None:
        self.lost += 1
        self._safe_log(
            "lost:late_after_shutdown",
            f"cost_evidence_lost key={key} tenant={tenant} kind={kind} reason=late_after_shutdown",
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
        if not self._armed:
            return 1
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
        if not (self._armed and provider_session_id):
            return
        if not self.enabled:
            if self._closed:  # a late reaper after shutdown: record it now, never silently
                identity = self._ids.get(session_id)
                tenant = identity.tenant_id if identity else "unknown"
                self._late(provider_session_id, tenant, "AVATAR")
            return
        expected = _expected.get()
        known = (
            ExecutionIdentity(
                tenant_id=expected[0],
                business_session_id=expected[1],
                runtime_session_id=session_id,
                generation=expected[2],
            )
            if expected
            else None
        )
        with self._lock:
            prior = self._open.pop(provider_session_id, None)
            self._open[provider_session_id] = (
                session_id,
                time.monotonic(),
                datetime.now(timezone.utc),
                derived,
                known,
            )
            expired = self._trim_open() if len(self._open) > _MAX_BOUND else []
        if prior is not None:  # same provider session opened twice: settle the old one first
            self.reopened += 1
            self._safe_log(
                "reopened",
                f"cost_evidence_reopened provider_session={provider_session_id}",
            )
            expired.append((provider_session_id, prior))
        for psid, rec in expired:  # emitted outside the lock: _push/_emit_avatar take it again
            self._emit_avatar(psid, rec, confirmed=False, extra_s=self.avatar_idle_bound_s)

    def _trim_open(
        self,
    ) -> list[tuple[str, tuple[str, float, datetime, bool, ExecutionIdentity | None]]]:
        """Caller holds the lock. Returns the records to settle as derived (expired ones first,
        then, only if still over the limit, the oldest open ones: counted + logged as evicted).
        The caller emits them after releasing the lock, so nothing is dropped silently."""
        now = time.monotonic()
        out = []
        for psid, rec in list(self._open.items()):
            if len(self._open) <= _MAX_BOUND:
                break
            if now - rec[1] > _OPEN_MAX_AGE_S:
                del self._open[psid]
                out.append((psid, rec))
        while len(self._open) > _MAX_BOUND:
            psid, rec = self._open.popitem(last=False)
            self.evicted += 1
            self._safe_log(
                "evicted:avatar_open",
                f"cost_evidence_evicted what=avatar_open provider_session={psid}",
            )
            out.append((psid, rec))
        return out

    def avatar_close(
        self, provider_session_id: str, *, confirmed: bool = True, extra_s: float = 0.0
    ) -> None:
        """Provider-CONFIRMED end (``confirmed=False`` only for a final settle). Idempotent.

        Unconfirmed: ``usage_source='derived'`` and the end is the moment we know for sure
        plus ``extra_s`` (the provider idle timeout: the latest it may still have been running).
        """
        if not (self._armed and provider_session_id):
            return
        with self._lock:
            rec = self._open.pop(provider_session_id, None)
        if rec is not None:
            self._emit_avatar(provider_session_id, rec, confirmed=confirmed, extra_s=extra_s)

    def avatar_settle_all(self, extra_s: float) -> None:
        """Shutdown: every still-open record is settled as derived (end = now + idle bound)."""
        if not self._armed:
            return
        with self._lock:
            records, self._open = list(self._open.items()), OrderedDict()
        for psid, rec in records:
            self._emit_avatar(psid, rec, confirmed=False, extra_s=extra_s)

    def _emit_avatar(
        self,
        psid: str,
        rec: tuple[str, float, datetime, bool, ExecutionIdentity | None],
        *,
        confirmed: bool,
        extra_s: float,
        identity: ExecutionIdentity | None = None,
    ) -> None:
        session_id, mono, started, derived_start, opened_identity = rec
        extra = 0.0 if confirmed else max(0.0, float(extra_s))
        self._push(
            session_id,
            AVATAR,
            psid,
            provider="lemonslice",
            _identity=identity or opened_identity,
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

    def close(self) -> None:
        """Shutdown: stop accepting; anything arriving later is logged as lost."""
        with self._buf_lock:
            if self._armed:
                self.enabled = False
                self._closed = True

    def lose_remaining(self, reason: str) -> int:
        """Shutdown leftovers that could not be flushed: one log line per sample, counted.

        Closes admission in the same locked step as the drain, so no sample can slip into the
        buffer after it (a later one is a late_after_shutdown loss instead)."""
        with self._buf_lock:
            left = list(self._buf)
            self._buf.clear()
            if self._armed:
                self.enabled = False
                self._closed = True
        for sample in left:
            self.lose(sample, reason)
        return len(left)

    def buffered(self) -> int:
        return len(self._buf)

    def open_count(self) -> int:
        return len(self._open)

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
