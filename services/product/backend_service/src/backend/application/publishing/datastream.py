"""Avatar audio data-stream writer (P0-FB-010, Report #46 section 2b).

Minimal writer for the LiveKit avatar data-stream protocol: a byte stream on
topic ``lk.audio_stream`` (attributes ``sample_rate`` / ``num_channels``) sent
only to the avatar participant, plus the RPCs ``lk.clear_buffer``,
``lk.playback_started`` and ``lk.playback_finished``. It depends only on an
``rtc.Room``-shaped object (no ``livekit-agents``, which would be fallback B2).

ASSUMPTION [SRC, not verified against LemonSlice, LS2]: topic/RPC names and the
16 kHz input rate are taken from the public LiveKit avatar plugin source.
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


def _swallow(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()  # retrieve it so a detached task never logs "never retrieved"


@dataclass
class _Play:
    """One utterance's playback record; events are matched to records, never to 'whatever is open'."""

    id: str
    wrote: bool = False  # a chunk was dispatched to the avatar (playback_started is plausible)
    closed: bool = False  # stream close was dispatched (playback_finished is plausible)
    cancelled: bool = False  # interrupted: a tombstone that absorbs its own late events
    timed_out: bool = False  # waiter gave up: a late finish is consumed here, not by the next one
    finished: bool = False
    started_at: float | None = None
    expires_at: float | None = None  # tombstone lifetime (monotonic), set when the clear ends
    done: asyncio.Event = field(default_factory=asyncio.Event)


class _Stream:
    """Writer wrapper: once poisoned (aborted or detached after a deadline) it refuses writes."""

    def __init__(self, writer: Any) -> None:
        self.writer = writer
        self.dead = False


class AvatarAudioChannel:
    """One room's audio path to one avatar participant. Loop-thread only.

    Epoch fence: ``epoch`` only grows. ``send`` is bound to the epoch it was called
    with and re-checks it after EVERY await (open, each write, close); a stale send
    aborts and closes its stream. Sends and ``clear_buffer`` share one lock and the
    clear holds it across the whole RPC, so no new-epoch stream can open or write
    while the avatar buffer is being cleared. Every stream operation has a hard
    deadline: the awaited task is cancelled and DETACHED when it expires, even if it
    swallows cancellation, and its stream is poisoned.

    ASSUMPTION [not verified against LemonSlice]: playback RPCs carry no utterance id
    on the wire (an optional ``utterance_id`` in the JSON payload is honoured when
    present). Without one, events are matched in FIFO order to plausible records
    (started: wrote; finished: closed). Interrupted records stay as tombstones for
    ``tombstone_ttl_s`` after the clear so a late id-less event of the interrupted
    utterance is absorbed there; if attribution is ambiguous the new utterance is
    left unconfirmed, never credited.
    """

    def __init__(
        self,
        room: Any,
        destination_identity: str,
        *,
        sample_rate: int = 16000,
        clock: Callable[[], float] = time.time,
        io_timeout_s: float = 5.0,
        history: int = 256,
        tombstone_ttl_s: float = 2.0,
    ) -> None:
        self._room = room
        self._dest = destination_identity
        self.sample_rate = sample_rate
        self._clock = clock
        self._io_timeout = io_timeout_s
        self._history = history
        self._ttl = tombstone_ttl_s
        self.epoch = 0
        self._io = asyncio.Lock()
        self._stream: _Stream | None = None
        self._utterance: str | None = None
        self._src_rate: int | None = None
        self._resampler: UtteranceResampler | None = None
        self._carry = b""
        self.sent_ms = 0.0
        self._plays: deque[_Play] = deque()
        self._by_id: dict[str, _Play] = {}
        self._clearing: asyncio.Event | None = None
        self.playback_started_at: OrderedDict[str, float] = OrderedDict()
        lp = room.local_participant
        lp.register_rpc_method(RPC_PLAYBACK_STARTED, self._on_started)
        lp.register_rpc_method(RPC_PLAYBACK_FINISHED, self._on_finished)

    # -- avatar -> runtime RPCs (only the avatar identity is trusted)
    @staticmethod
    def _payload(data: Any) -> dict:
        try:
            p = json.loads(getattr(data, "payload", "") or "{}")
        except (TypeError, ValueError):
            return {}
        return p if isinstance(p, dict) else {}

    def _purge(self) -> None:
        now = time.monotonic()
        for rec in [r for r in self._plays if r.expires_at is not None and r.expires_at <= now]:
            self._retire(rec)

    def _match(self, payload: dict, ok: Callable[[_Play], bool]) -> _Play | None:
        self._purge()
        wire_id = payload.get("utterance_id")
        if wire_id is not None:
            rec = self._by_id.get(str(wire_id))
            return rec if rec is not None and ok(rec) else None
        return next((r for r in self._plays if ok(r)), None)  # oldest first: tombstones win

    async def _on_started(self, data: Any) -> str:
        if data.caller_identity != self._dest:
            return "ok"
        rec = self._match(
            self._payload(data), lambda r: r.wrote and r.started_at is None and not r.finished
        )
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
            # Only the answer to OUR clear_buffer counts, and only if it names nothing foreign.
            if self._clearing is not None and (wire_id is None or str(wire_id) in self._by_id):
                self._clearing.set()
            return "ok"
        rec = self._match(
            payload, lambda r: not r.finished and (r.closed or (r.cancelled and r.wrote))
        )
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

    async def _bounded(self, awaitable: Any, what: str) -> Any:
        """Hard deadline: never waits past ``io_timeout`` even if the awaited task ignores cancel."""
        task = asyncio.ensure_future(awaitable)
        try:
            done, _ = await asyncio.wait({task}, timeout=self._io_timeout)
        except asyncio.CancelledError:
            task.cancel()
            task.add_done_callback(_swallow)
            raise
        if task in done:
            return task.result()
        task.cancel()
        task.add_done_callback(_swallow)
        raise AvatarIOError(f"avatar stream {what} timed out")

    async def send(
        self, pcm: bytes, *, src_rate: int, utterance_id: str, epoch: int, final: bool
    ) -> bytes | None:
        """Write one window; returns the resampled PCM, or None if the epoch fence aborted it."""
        async with self._io:
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
            while len(self._plays) > self._history:  # cap on insert
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
            await self._bounded(stream.writer.write(out[i : i + step]), "write")
            if self._fenced(epoch):
                await self._abort()
                return None
        self.sent_ms += len(out) / 2 / self.sample_rate * 1000
        if final:
            await self._close(reason=None)
            if self._fenced(epoch):
                return None
        return out

    async def _close(self, *, reason: str | None) -> None:
        stream, self._stream = self._stream, None
        rec = self._by_id.get(self._utterance or "")
        if rec is not None and reason is None:
            rec.closed = True  # before the await: a finish may arrive while aclose() is pending
        if stream is None:
            return
        if reason is not None:
            stream.dead = True
        try:
            await self._bounded(
                stream.writer.aclose(reason=reason) if reason else stream.writer.aclose(), "close"
            )
        finally:
            stream.dead = True

    async def _abort(self) -> None:
        """Best-effort abort of the open stream (bounded; never raises)."""
        try:
            await self._close(reason="interrupted")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("avatar stream abort failed error_type=%s", type(exc).__name__)

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

    async def clear_buffer(self, timeout_s: float = 2.0) -> None:
        """Fence stale sends, abort the open stream, clear the avatar buffer, await its answer.

        The send lock is held across the whole RPC, so a new-epoch send cannot open or write
        while the avatar buffer is being cleared.
        """
        self.epoch += 1  # synchronous: every suspended send is now stale
        for rec in self._plays:
            if not rec.cancelled:
                rec.cancelled = True
                rec.done.set()
        owned = False
        try:
            # Every holder is bounded by io_timeout per operation and stops at the next fence check.
            await asyncio.wait_for(self._io.acquire(), self._io_timeout * 3 + 1)
            owned = True
        except asyncio.TimeoutError:
            log.warning("avatar interrupt could not take the send lock before its deadline")
        try:
            if owned:
                await self._abort()
            elif self._stream is not None:
                self._stream.dead = True  # refuse any further write by the stuck send
            self._clearing = clearing = asyncio.Event()
            try:
                await self._bounded_rpc(timeout_s)
                await asyncio.wait_for(clearing.wait(), timeout_s)
            except Exception as exc:  # bounded; interrupt must never raise into the coordinator
                log.warning("avatar clear_buffer not confirmed error_type=%s", type(exc).__name__)
        finally:
            self._clearing = None
            expires = time.monotonic() + self._ttl
            for rec in self._plays:
                if rec.cancelled and rec.expires_at is None:
                    rec.expires_at = expires  # tombstone until the late events had time to land
            if owned:
                self._utterance = None
                self._io.release()

    async def _bounded_rpc(self, timeout_s: float) -> None:
        await self._bounded(
            self._room.local_participant.perform_rpc(
                destination_identity=self._dest,
                method=RPC_CLEAR_BUFFER,
                payload=json.dumps({}),
                response_timeout=timeout_s,
            ),
            "rpc",
        )
