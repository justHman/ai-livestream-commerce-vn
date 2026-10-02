"""Avatar audio data-stream writer (P0-FB-010, Report #46 section 2b).

Minimal writer for the LiveKit avatar data-stream protocol: a byte stream on
topic ``lk.audio_stream`` (attributes ``sample_rate`` / ``num_channels``) sent
only to the avatar participant, plus the RPCs ``lk.clear_buffer``,
``lk.playback_started`` and ``lk.playback_finished``. It depends only on an
``rtc.Room``-shaped object (no ``livekit-agents``, which would be fallback B2).

ASSUMPTION [SRC, not verified against LemonSlice, LS2]: topic/RPC names and the
16 kHz input rate are taken from the public LiveKit avatar plugin source.

Known limitation (F2): ``broken`` is permanent for the channel and is triggered by a single
missed deadline (``io_timeout_s``, default 2 s) or a lost clear lock. Nothing restarts the session
automatically; the only surface is ``send`` raising ``AvatarChannelBroken`` and the backend's
``session_status`` returning ``channel_broken``. A transient stall therefore needs a session restart.

Principle: when ordering or ownership cannot be proven the channel FAILS CLOSED. It is
marked ``broken`` (typed error on every later send, a fresh session is required) or the
affected utterance is left explicitly unconfirmed; it never continues on a best-effort basis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

AUDIO_TOPIC = "lk.audio_stream"
RPC_CLEAR_BUFFER = "lk.clear_buffer"
RPC_PLAYBACK_STARTED = "lk.playback_started"
RPC_PLAYBACK_FINISHED = "lk.playback_finished"

# Worst case of one clear_buffer (lock wait <= 60 %, abort <= 0.5 s, RPC + ack the remainder).
DEFAULT_CLEAR_BUDGET_S = 5.0
DEFAULT_IO_TIMEOUT_S = 2.0


class UtteranceResampler:
    """Mono int16 resampler; a strict no-op when the rates already match."""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self._noop = src_rate == dst_rate
        self._rs = None
        if not self._noop:
            from livekit import rtc  # type: ignore

            self._rs = rtc.AudioResampler(src_rate, dst_rate, num_channels=1)

    def push(self, pcm: bytes) -> bytes:
        if self._rs is None:
            return pcm
        return b"".join(bytes(f.data) for f in self._rs.push(bytearray(pcm)))

    def flush(self) -> bytes:
        if self._rs is None:
            return b""
        return b"".join(bytes(f.data) for f in self._rs.flush())


class AvatarStreamError(RuntimeError):
    """Sanitized stream failure: a fixed message (+ exception class name), never a cause chain."""


class AvatarIOError(AvatarStreamError):
    """A data-stream open/write/close/RPC exceeded its application deadline."""


class AvatarProtocolError(AvatarStreamError):
    """The caller broke a stream invariant (e.g. a sample-rate change inside one utterance)."""


class AvatarChannelBroken(AvatarStreamError):
    """Ordering/ownership could not be proven: no further audio until a fresh session."""


def _swallow(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()  # retrieve it so a detached task never logs "never retrieved"


@dataclass
class _Play:
    """One utterance's playback record; events are matched to records, never to 'whatever is open'."""

    id: str
    wrote: bool = False  # a chunk was dispatched to the avatar (playback_started is plausible)
    closed: bool = False  # stream close was dispatched (playback_finished is plausible)
    cancelled: bool = False  # interrupted: stays unresolved until its own events arrive
    timed_out: bool = False  # waiter gave up: a late finish is consumed here, not by the next one
    finished: bool = False
    cancel_seq: int = 0  # the clear that interrupted it
    started_at: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Ack:
    """Pending answer to one clear_buffer; stays queued (abandoned) after its clear gave up."""

    seq: int
    event: asyncio.Event = field(default_factory=asyncio.Event)
    abandoned: bool = False


class _Stream:
    """Writer wrapper: once dead (aborted/closed/detached) nothing more is dispatched to it."""

    def __init__(self, writer: Any) -> None:
        self.writer = writer
        self.dead = False


class AvatarAudioChannel:
    """One room's audio path to one avatar participant. Loop-thread only.

    Epoch fence: ``epoch`` only grows. ``send`` is bound to the epoch it was called with and
    re-checks it after EVERY await; a stale send aborts and closes its stream. Sends and
    ``clear_buffer`` share one lock and the clear holds it across the whole RPC.

    Hard deadlines: every stream operation runs in its own task. When the deadline expires the
    task is cancelled and DETACHED (it may ignore cancellation); the channel is then ``broken``.
    A detached open that finally returns a writer gets that writer closed by a done-callback; a
    detached write is guarded at the lowest layer we own (checked inside the task right before the
    SDK call: dead stream / old epoch / broken channel dispatch nothing). A write that was already
    handed to the SDK cannot be recalled; the stream is closed (``interrupted``) and the channel is
    broken, so nothing further is sent.

    ASSUMPTION [not verified against LemonSlice]: playback RPCs carry no utterance id on the wire
    (an optional ``utterance_id`` in the JSON payload is honoured when present). Without one an
    event may only be credited to the OLDEST unresolved record (no finish seen). Interrupted and
    timed-out records stay unresolved until their own finish arrives (or the ``history`` cap
    evicts them); there is no time-based expiry. While an older record is unresolved a newer
    utterance stays unconfirmed (playback_started_at None / playback_unconfirmed): safe, never a
    false credit.
    """

    def __init__(
        self,
        room: Any,
        destination_identity: str,
        *,
        sample_rate: int = 16000,
        clock: Callable[[], float] = time.time,
        io_timeout_s: float = DEFAULT_IO_TIMEOUT_S,
        history: int = 256,
        clear_budget_s: float = DEFAULT_CLEAR_BUDGET_S,
    ) -> None:
        self._room = room
        self._dest = destination_identity
        self.sample_rate = sample_rate
        self._clock = clock
        self._io_timeout = io_timeout_s
        self._history = history
        self._budget = clear_budget_s
        self.epoch = 0
        self.broken: str | None = None
        self._io = asyncio.Lock()
        self._stream: _Stream | None = None
        self._utterance: str | None = None
        self._src_rate: int | None = None
        self._resampler: UtteranceResampler | None = None
        self._carry = b""
        self.sent_ms = 0.0
        self._plays: deque[_Play] = deque()
        self._by_id: dict[str, _Play] = {}
        self._acks: deque[_Ack] = deque()  # FIFO, one per clear; abandoned ones absorb late acks
        self._clear_seq = 0
        # Set when the history cap evicted an UNRESOLVED record: an id-less event can no longer be
        # attributed to the oldest unresolved record (the true owner is gone), so none is credited
        # until a clear ack resolves everything cancelled before it.
        self._ambiguous = False
        self._detached: set[asyncio.Future] = set()
        self.playback_started_at: OrderedDict[str, float] = OrderedDict()
        lp = room.local_participant
        lp.register_rpc_method(RPC_PLAYBACK_STARTED, self._on_started)
        lp.register_rpc_method(RPC_PLAYBACK_FINISHED, self._on_finished)

    # -- fail closed
    def _break(self, why: str) -> None:
        if self.broken is None:
            self.broken = why
            log.error("avatar channel broken: %s", why)

    def _check_usable(self) -> None:
        if self.broken is not None:
            raise AvatarChannelBroken("avatar channel is broken; a fresh session is required")

    # -- avatar -> runtime RPCs (only the avatar identity is trusted)
    @staticmethod
    def _payload(data: Any) -> dict:
        try:
            p = json.loads(getattr(data, "payload", "") or "{}")
        except (TypeError, ValueError):
            return {}
        return p if isinstance(p, dict) else {}

    def _match(self, payload: dict, ok: Callable[[_Play], bool]) -> _Play | None:
        wire_id = payload.get("utterance_id")
        if wire_id is not None:
            rec = self._by_id.get(str(wire_id))
            return rec if rec is not None and ok(rec) else None
        # id-less: only the OLDEST unresolved record may take it; a newer utterance never does
        if self._ambiguous:
            return None
        oldest = next((r for r in self._plays if not r.finished), None)
        return oldest if oldest is not None and ok(oldest) else None

    async def _on_started(self, data: Any) -> str:
        if data.caller_identity != self._dest:
            return "ok"
        rec = self._match(self._payload(data), lambda r: r.wrote and r.started_at is None)
        if rec is not None:
            rec.started_at = self._clock()
            if not rec.cancelled:  # an interrupted utterance absorbs the event, never records it
                self.playback_started_at[rec.id] = rec.started_at
                while len(self.playback_started_at) > self._history:
                    self.playback_started_at.popitem(last=False)
        return "ok"

    async def _on_finished(self, data: Any) -> str:
        if data.caller_identity != self._dest:
            return "ok"
        payload = self._payload(data)
        wire_id = payload.get("utterance_id")
        if payload.get("interrupted"):
            # Only the answer to one of OUR clear_buffer calls counts, and only if it names
            # nothing foreign. The avatar says it flushed everything written before that clear,
            # so every record cancelled by it (or an earlier clear) is resolved. An abandoned
            # clear (it gave up) still takes its own late ack, so it cannot confirm a later one.
            if self._acks and (wire_id is None or str(wire_id) in self._by_id):
                ack = self._acks.popleft()
                if not ack.abandoned:
                    ack.event.set()
                    self._ambiguous = False  # the avatar flushed everything written before it
                for rec in [r for r in self._plays if r.cancelled and r.cancel_seq <= ack.seq]:
                    self._retire(rec)
            return "ok"
        rec = self._match(payload, lambda r: r.closed or (r.cancelled and r.wrote))
        if rec is not None:
            rec.finished = True
            if rec.cancelled or rec.timed_out:
                self._retire(rec)
            else:
                rec.done.set()  # stays indexed until wait_finished consumes it
        return "ok"

    def _retire(self, rec: _Play) -> None:
        try:
            self._plays.remove(rec)
        except ValueError:
            pass
        if self._by_id.get(rec.id) is rec:
            del self._by_id[rec.id]

    @property
    def open_utterance(self) -> str | None:
        return self._utterance

    def _fenced(self, epoch: int) -> bool:
        return epoch != self.epoch

    # -- hard-deadline operations
    def _detach(self, task: asyncio.Future, on_late: Callable[[Any], None] | None) -> None:
        task.cancel()
        self._detached.add(task)

        def late(t: asyncio.Future) -> None:
            self._detached.discard(t)
            if t.cancelled():
                return
            if t.exception() is None and on_late is not None:
                on_late(t.result())  # e.g. close a writer that was opened after its deadline

        task.add_done_callback(late)

    async def _bounded(
        self,
        awaitable: Any,
        what: str,
        *,
        limit: float | None = None,
        on_late: Callable[[Any], None] | None = None,
    ) -> Any:
        """Never waits past the deadline, even if the awaited task ignores cancellation."""
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=limit or self._io_timeout)
        except asyncio.CancelledError:
            self._detach(task, on_late)
            raise
        if task in done:
            return task.result()
        self._detach(task, on_late)
        self._break(f"{what} exceeded its deadline")
        raise AvatarIOError(f"avatar stream {what} timed out")

    def _orphan(self, writer: Any) -> None:
        """A writer produced after its caller gave up: nobody owns it, close it."""

        async def close() -> None:
            try:
                await asyncio.wait_for(writer.aclose(reason="interrupted"), self._io_timeout)
            except Exception as exc:
                log.warning("orphan stream close failed error_type=%s", type(exc).__name__)

        task = asyncio.ensure_future(close())
        self._detached.add(task)
        task.add_done_callback(self._detached.discard)

    async def shutdown(self) -> None:
        """Cancel everything still detached (session teardown)."""
        pending = [t for t in self._detached if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=1.0)

    async def _guarded_write(self, stream: _Stream, data: bytes, epoch: int) -> None:
        # Lowest layer we own: nothing is dispatched for a dead stream, an old epoch or a broken
        # channel, however late this task gets scheduled.
        if stream.dead or self._fenced(epoch) or self.broken is not None:
            return
        await stream.writer.write(data)

    async def send(
        self, pcm: bytes, *, src_rate: int, utterance_id: str, epoch: int, final: bool
    ) -> bytes | None:
        """Write one window; returns the resampled PCM, or None if the epoch fence aborted it."""
        self._check_usable()
        async with self._io:
            self._check_usable()
            failure: AvatarStreamError | None = None
            result: bytes | None = None
            try:
                result = await self._send(pcm, src_rate, utterance_id, epoch, final)
            except AvatarStreamError as exc:
                failure = exc
            except asyncio.CancelledError:
                await self._abort()
                raise
            except Exception as exc:
                failure = AvatarStreamError(f"avatar stream failed error_type={type(exc).__name__}")
            if failure is not None:  # raised outside any except block: no __context__ / __cause__
                await self._abort()
                raise failure
            return result

    async def _send(
        self, pcm: bytes, src_rate: int, utterance_id: str, epoch: int, final: bool
    ) -> bytes | None:
        if self._fenced(epoch):
            return None
        if self._stream is not None and self._utterance == utterance_id:
            if src_rate != self._src_rate:
                raise AvatarProtocolError("sample rate changed inside one utterance")
        elif self._stream is not None:
            await self._close(reason=None)  # previous utterance ended without a final
            if self._fenced(epoch):
                return None
        if self._stream is None:
            rec = _Play(utterance_id)
            self._plays.append(rec)
            self._by_id[utterance_id] = rec
            while len(self._plays) > self._history:  # cap on insert (count, not time)
                if not self._plays[0].finished:
                    self._ambiguous = True  # its own events may still arrive, unattributable
                self._retire(self._plays[0])
            self._utterance = utterance_id
            self._src_rate = src_rate
            self._resampler = UtteranceResampler(src_rate, self.sample_rate)
            self._carry = b""
            self.sent_ms = 0.0
            writer = await self._bounded(
                self._room.local_participant.stream_bytes(
                    name=f"livento-{utterance_id}",
                    topic=AUDIO_TOPIC,
                    destination_identities=[self._dest],
                    attributes={
                        "sample_rate": str(self.sample_rate),
                        "num_channels": "1",
                        "livento.utterance_id": utterance_id,
                        "livento.epoch": str(epoch),
                    },
                ),
                "open",
                on_late=self._orphan,
            )
            self._stream = _Stream(writer)
            if self._fenced(epoch):
                await self._abort()
                return None
        assert self._resampler is not None
        pcm = self._carry + pcm
        if len(pcm) % 2:  # int16 samples: never split one across windows
            pcm, self._carry = pcm[:-1], pcm[-1:]
        else:
            self._carry = b""
        out = self._resampler.push(pcm) + (self._resampler.flush() if final else b"")
        # Engines may return whole utterances: write in <= 200 ms slices (even sample count).
        step = self.sample_rate // 5 * 2
        for i in range(0, len(out), step):
            stream = self._stream
            if self._fenced(epoch) or stream is None or stream.dead:
                await self._abort()
                return None
            rec = self._by_id.get(utterance_id)
            if rec is not None:
                rec.wrote = True  # dispatched: the avatar may report started before write returns
            await self._bounded(self._guarded_write(stream, out[i : i + step], epoch), "write")
            if self._fenced(epoch):
                await self._abort()
                return None
        self.sent_ms += len(out) / 2 / self.sample_rate * 1000
        if final:
            await self._close(reason=None)
            if self._fenced(epoch):
                return None
        return out

    async def _close(self, *, reason: str | None, limit: float | None = None) -> None:
        """Close the open stream; the reference is dropped only after the close succeeded."""
        stream = self._stream
        if stream is None:
            return
        rec = self._by_id.get(self._utterance or "")
        if rec is not None and reason is None:
            rec.closed = True  # before the await: a finish may arrive while aclose() is pending
        if reason is not None:
            stream.dead = True
        await self._bounded(
            stream.writer.aclose(reason=reason) if reason else stream.writer.aclose(),
            "close",
            limit=limit,
        )
        stream.dead = True
        if self._stream is stream:
            self._stream = None

    async def _abort(self, limit: float | None = 0.5) -> None:
        """Abort the open stream. If it cannot be proven closed the channel fails closed."""
        stream = self._stream
        if stream is None:
            return
        stream.dead = True
        try:
            await self._close(reason="interrupted", limit=limit)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._break(f"stream abort failed error_type={type(exc).__name__}")

    async def wait_finished(self, utterance_id: str, timeout_s: float) -> bool:
        """True only if ``lk.playback_finished`` was matched to THIS utterance (even if early)."""
        rec = self._by_id.get(utterance_id)
        if rec is None:
            return False
        try:
            await asyncio.wait_for(rec.done.wait(), timeout_s)
        except asyncio.TimeoutError:
            rec.timed_out = True  # stays queued so its late finish is not credited to the next
            return False
        ok = rec.finished and not rec.cancelled
        if ok:
            self._retire(rec)
        return ok

    async def clear_buffer(self, timeout_s: float | None = None) -> None:
        """Fence stale sends, abort the open stream, clear the avatar buffer, await its answer.

        ``timeout_s`` is the TOTAL budget (default ``DEFAULT_CLEAR_BUDGET_S``): lock wait <= 60 %,
        stream abort <= 0.5 s, RPC + ack the remainder. If the lock cannot be taken in time the
        channel is broken (no further audio); the clear RPC is still sent, as no send can run.
        """
        budget = self._budget if timeout_s is None else timeout_s
        end = time.monotonic() + budget
        # Audio may be buffered at the avatar: an open stream or a dispatched, unfinished record.
        needs_clear = self._stream is not None or any(
            r.wrote and not r.finished for r in self._plays
        )
        self.epoch += 1  # synchronous: every suspended send is now stale
        self._clear_seq += 1
        seq = self._clear_seq
        for rec in self._plays:
            if not rec.cancelled:
                rec.cancelled = True
                rec.cancel_seq = seq
                rec.done.set()
        owned = await self._acquire_bounded(budget * 0.6)
        if not owned:
            self._break("send lock not acquired before the clear deadline")
        ack = _Ack(seq)  # per-clear token: never shared with another clear
        self._acks.append(ack)
        while len(self._acks) > 16:
            self._acks.popleft()
        try:
            if owned:
                await self._abort(limit=max(0.05, min(0.5, end - time.monotonic())))
            remaining = max(0.05, end - time.monotonic())
            try:
                await self._bounded(
                    self._rpc_and_ack(ack, remaining, needs_clear),
                    "clear",
                    limit=remaining + 0.25,
                )
            except Exception as exc:  # bounded; interrupt must never raise into the coordinator
                log.warning("avatar clear_buffer not confirmed error_type=%s", type(exc).__name__)
        finally:
            ack.abandoned = True  # a late ack of THIS clear must not satisfy a later one
            for rec in [r for r in self._plays if r.cancelled and not r.wrote]:
                self._retire(rec)  # nothing was dispatched: no event can ever belong to it
            if owned:
                self._utterance = None
                self._io.release()

    async def _acquire_bounded(self, timeout: float) -> bool:
        """Take the send lock within ``timeout``; a timeout can never leave it held."""
        acq = asyncio.ensure_future(self._io.acquire())
        try:
            await asyncio.wait({acq}, timeout=timeout)
        except asyncio.CancelledError:
            self._give_up(acq)
            raise
        if acq.done() and not acq.cancelled() and acq.exception() is None:
            return True
        self._give_up(acq)
        return False

    def _give_up(self, acq: asyncio.Future) -> None:
        acq.cancel()

        def release_if_won(t: asyncio.Future) -> None:
            if not t.cancelled() and t.exception() is None:
                self._io.release()  # the acquire won the race with the cancel

        acq.add_done_callback(release_if_won)

    async def _rpc_and_ack(self, ack: _Ack, remaining: float, needs_clear: bool = True) -> None:
        start = time.monotonic()
        try:
            await self._room.local_participant.perform_rpc(
                destination_identity=self._dest,
                method=RPC_CLEAR_BUFFER,
                payload=json.dumps({}),
                response_timeout=remaining,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Buffered speech may still be playing: fail closed, new audio would overlap it.
            # Benign only when nothing had been dispatched to the avatar.
            if needs_clear:
                self._break(f"clear_buffer RPC failed error_type={type(exc).__name__}")
            raise
        await asyncio.wait_for(ack.event.wait(), max(0.05, remaining - (time.monotonic() - start)))
