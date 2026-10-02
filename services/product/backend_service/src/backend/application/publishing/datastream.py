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


class AvatarIOError(RuntimeError):
    """A data-stream open/write/close exceeded its application deadline (no secrets inside)."""


@dataclass
class _Play:
    """One utterance's playback record; events are matched to records, never to 'whatever is open'."""

    id: str
    wrote: bool = False  # at least one chunk reached the avatar (playback_started is plausible)
    closed: bool = False  # stream closed normally (playback_finished is plausible)
    cancelled: bool = False  # interrupted: its events are tombstoned, never credited
    timed_out: bool = False  # waiter gave up: a late finish is consumed here, not by the next one
    started_at: float | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)


class AvatarAudioChannel:
    """One room's audio path to one avatar participant. Loop-thread only.

    Epoch fence: ``epoch`` only grows. ``send`` is bound to the epoch it was called
    with and re-checks it after EVERY await (open, each write, close); a stale send
    aborts and closes its stream. ``clear_buffer`` bumps the epoch first and then
    waits for the in-flight send to finish aborting, so the clear is never overtaken
    by stale audio.

    ASSUMPTION [not verified against LemonSlice]: playback RPCs carry no utterance id
    on the wire (an optional ``utterance_id`` in the JSON payload is honoured when
    present). Without one, events are matched in FIFO order to records that are
    plausible for the event (started: wrote; finished: closed), interrupted records
    are tombstoned, and a timed-out record stays queued to absorb its late finish.
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
    ) -> None:
        self._room = room
        self._dest = destination_identity
        self.sample_rate = sample_rate
        self._clock = clock
        self._io_timeout = io_timeout_s
        self._history = history
        self.epoch = 0
        self._io = asyncio.Lock()
        self._writer: Any = None
        self._utterance: str | None = None
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

    def _match(self, payload: dict, ok: Callable[[_Play], bool]) -> _Play | None:
        wire_id = payload.get("utterance_id")
        if wire_id is not None:
            rec = self._by_id.get(str(wire_id))
            return rec if rec is not None and ok(rec) else None
        return next((r for r in self._plays if ok(r)), None)

    async def _on_started(self, data: Any) -> str:
        if data.caller_identity != self._dest:
            return "ok"
        rec = self._match(
            self._payload(data), lambda r: r.wrote and r.started_at is None and not r.cancelled
        )
        if rec is not None:
            rec.started_at = self._clock()
            self.playback_started_at[rec.id] = rec.started_at
            while len(self.playback_started_at) > self._history:
                self.playback_started_at.popitem(last=False)
        return "ok"

    async def _on_finished(self, data: Any) -> str:
        if data.caller_identity != self._dest:
            return "ok"
        if self._clearing is not None:  # the answer to our own clear_buffer
            self._clearing.set()
            return "ok"
        payload = self._payload(data)
        if payload.get("interrupted"):
            return "ok"  # never completes a live utterance
        rec = self._match(payload, lambda r: r.closed)
        if rec is not None:
            self._retire(rec)
            if not rec.cancelled and not rec.timed_out:
                rec.done.set()
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
        try:
            return await asyncio.wait_for(awaitable, self._io_timeout)
        except asyncio.TimeoutError:
            raise AvatarIOError(f"avatar stream {what} timed out") from None

    async def send(
        self, pcm: bytes, *, src_rate: int, utterance_id: str, epoch: int, final: bool
    ) -> bytes | None:
        """Write one window; returns the resampled PCM, or None if the epoch fence aborted it."""
        async with self._io:
            return await self._send(pcm, src_rate, utterance_id, epoch, final)

    async def _send(
        self, pcm: bytes, src_rate: int, utterance_id: str, epoch: int, final: bool
    ) -> bytes | None:
        if self._fenced(epoch):
            return None
        try:
            if self._writer is not None and self._utterance != utterance_id:
                await self._close(reason=None)  # previous utterance ended without a final
                if self._fenced(epoch):
                    return None
            if self._writer is None:
                rec = _Play(utterance_id)
                self._plays.append(rec)
                self._by_id[utterance_id] = rec
                self._utterance = utterance_id
                self._resampler = UtteranceResampler(src_rate, self.sample_rate)
                self._carry = b""
                self.sent_ms = 0.0
                self._writer = await self._bounded(
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
                if self._fenced(epoch):
                    await self._abort()
                    return None
                await self._bounded(self._writer.write(out[i : i + step]), "write")
                rec = self._by_id.get(utterance_id)
                if rec is not None:
                    rec.wrote = True
                if self._fenced(epoch):
                    await self._abort()
                    return None
            self.sent_ms += len(out) / 2 / self.sample_rate * 1000
            if final:
                await self._close(reason=None)
                if self._fenced(epoch):
                    return None
            return out
        except AvatarIOError:
            await self._abort()
            raise

    async def _close(self, *, reason: str | None) -> None:
        writer, self._writer = self._writer, None
        rec = self._by_id.get(self._utterance or "")
        if rec is not None and reason is None:
            rec.closed = True
        if writer is not None:
            await self._bounded(
                writer.aclose(reason=reason) if reason else writer.aclose(), "close"
            )

    async def _abort(self) -> None:
        """Best-effort abort of the open stream (bounded; never raises)."""
        try:
            await self._close(reason="interrupted")
        except Exception as exc:
            log.warning("avatar stream abort failed error_type=%s", type(exc).__name__)

    async def wait_finished(self, utterance_id: str, timeout_s: float) -> bool:
        """True only if ``lk.playback_finished`` was matched to THIS utterance in time."""
        rec = self._by_id.get(utterance_id)
        if rec is None:
            return False
        try:
            await asyncio.wait_for(rec.done.wait(), timeout_s)
        except asyncio.TimeoutError:
            rec.timed_out = True  # stays queued so its late finish is not credited to the next
            return False
        return not rec.cancelled

    async def clear_buffer(self, timeout_s: float = 2.0) -> None:
        """Fence stale sends, abort the open stream, clear the avatar buffer, await its answer."""
        self.epoch += 1  # synchronous: every suspended send is now stale
        for rec in self._plays:
            rec.cancelled = True
            rec.done.set()
        clearing = self._clearing = asyncio.Event()
        try:
            try:  # the stale send resumes, sees the new epoch and aborts; bounded
                await asyncio.wait_for(self._io.acquire(), self._io_timeout)
                acquired = True
            except asyncio.TimeoutError:
                acquired = False
                log.warning("avatar interrupt did not wait for a stuck send")
            try:
                await self._abort()
            finally:
                if acquired:
                    self._io.release()
            confirmed = False
            try:
                await self._room.local_participant.perform_rpc(
                    destination_identity=self._dest,
                    method=RPC_CLEAR_BUFFER,
                    payload=json.dumps({}),
                    response_timeout=timeout_s,
                )
                await asyncio.wait_for(clearing.wait(), timeout_s)
                confirmed = True
            except Exception as exc:  # bounded; interrupt must never raise into the coordinator
                log.warning("avatar clear_buffer not confirmed error_type=%s", type(exc).__name__)
            if confirmed:  # the avatar buffer is empty: nothing older can still arrive
                for rec in [r for r in self._plays if r.cancelled]:
                    self._retire(rec)
            while len(self._plays) > self._history:
                self._retire(self._plays[0])
        finally:
            self._clearing = None
            self._utterance = None
