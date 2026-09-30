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
from collections.abc import Callable
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


class AvatarAudioChannel:
    """One room's audio path to one avatar participant. Loop-thread only."""

    def __init__(
        self,
        room: Any,
        destination_identity: str,
        *,
        sample_rate: int = 16000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._room = room
        self._dest = destination_identity
        self.sample_rate = sample_rate
        self._clock = clock
        self._writer: Any = None
        self._utterance: str | None = None
        self._resampler: UtteranceResampler | None = None
        self.sent_ms = 0.0
        self.playback_started_at: dict[str, float] = {}
        self._finished = asyncio.Event()
        self._finished_utterance: str | None = None
        lp = room.local_participant
        lp.register_rpc_method(RPC_PLAYBACK_STARTED, self._on_started)
        lp.register_rpc_method(RPC_PLAYBACK_FINISHED, self._on_finished)

    # -- avatar -> runtime RPCs (only the avatar identity is trusted)
    async def _on_started(self, data: Any) -> str:
        if data.caller_identity == self._dest and self._utterance is not None:
            self.playback_started_at.setdefault(self._utterance, self._clock())
        return "ok"

    async def _on_finished(self, data: Any) -> str:
        if data.caller_identity == self._dest:
            self._finished_utterance = self._utterance
            self._finished.set()
        return "ok"

    @property
    def open_utterance(self) -> str | None:
        return self._utterance

    async def send(
        self, pcm: bytes, *, src_rate: int, utterance_id: str, epoch: int, final: bool
    ) -> bytes:
        """Write one window (returns the resampled PCM); the final window closes the stream."""
        if self._writer is not None and self._utterance != utterance_id:
            await self._close(reason=None)  # previous utterance ended without a final
        if self._writer is None:
            self._utterance = utterance_id
            self._resampler = UtteranceResampler(src_rate, self.sample_rate)
            self.sent_ms = 0.0
            self._finished.clear()
            self._writer = await self._room.local_participant.stream_bytes(
                name=f"livento-{utterance_id}",
                topic=AUDIO_TOPIC,
                destination_identities=[self._dest],
                attributes={
                    "sample_rate": str(self.sample_rate),
                    "num_channels": "1",
                    "livento.utterance_id": utterance_id,
                    "livento.epoch": str(epoch),
                },
            )
        assert self._resampler is not None
        out = self._resampler.push(pcm) + (self._resampler.flush() if final else b"")
        # Engines may return whole utterances: write in <= 200 ms slices (even sample count).
        step = self.sample_rate // 5 * 2
        for i in range(0, len(out), step):
            await self._writer.write(out[i : i + step])
        self.sent_ms += len(out) / 2 / self.sample_rate * 1000
        if final:
            await self._close(reason=None)
        return out

    async def _close(self, *, reason: str | None) -> None:
        writer, self._writer = self._writer, None
        if writer is not None:
            await writer.aclose(reason=reason) if reason else await writer.aclose()

    async def wait_finished(self, utterance_id: str, timeout_s: float) -> bool:
        """True if ``lk.playback_finished`` arrived for this utterance in time."""
        try:
            await asyncio.wait_for(self._finished.wait(), timeout_s)
        except asyncio.TimeoutError:
            return False
        return self._finished_utterance == utterance_id

    async def clear_buffer(self, timeout_s: float = 2.0) -> None:
        """Abort the open stream, clear the avatar buffer, await playback_finished."""
        self._finished.clear()
        await self._close(reason="interrupted")
        try:
            await self._room.local_participant.perform_rpc(
                destination_identity=self._dest,
                method=RPC_CLEAR_BUFFER,
                payload=json.dumps({}),
                response_timeout=timeout_s,
            )
            await asyncio.wait_for(self._finished.wait(), timeout_s)
        except Exception as exc:  # bounded; interrupt must never raise into the coordinator
            log.warning("avatar clear_buffer not confirmed error_type=%s", type(exc).__name__)
        self._utterance = None
