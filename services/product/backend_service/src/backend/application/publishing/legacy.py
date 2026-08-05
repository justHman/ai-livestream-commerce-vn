"""LiveKit audio publish (backend -> SFU).

No-op unless LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET are set AND
LIVEKIT_PUBLISH=1. ``livekit-rtc`` remains optional and is imported lazily.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.application.render.windows import AudioWindow

log = logging.getLogger(__name__)

_FRAME_MS = 20
_PCM_SAMPLE_WIDTH = 2
_DEFAULT_SAMPLE_RATE = 24_000
_DEFAULT_NUM_CHANNELS = 1


def publish_enabled(env: dict[str, str] | None = None) -> bool:
    """True only when publish flag and all LiveKit credentials are present."""
    source = env if env is not None else os.environ
    flag = str(source.get("LIVEKIT_PUBLISH", "0")).lower() in {
        "1",
        "true",
        "on",
        "yes",
    }
    if not flag:
        return False
    return all(
        (source.get(key) or "").strip()
        for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
    )


def _validate_format(sample_rate: int, num_channels: int) -> None:
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
        raise ValueError("sample_rate must be a positive integer")
    if isinstance(num_channels, bool) or not isinstance(num_channels, int) or num_channels <= 0:
        raise ValueError("num_channels must be a positive integer")


def _validate_pcm(pcm: bytes, sample_rate: int, num_channels: int) -> bytes:
    _validate_format(sample_rate, num_channels)
    if not isinstance(pcm, (bytes, bytearray, memoryview)):
        raise TypeError("pcm must be bytes-like")
    data = bytes(pcm)
    sample_bytes = num_channels * _PCM_SAMPLE_WIDTH
    if not data or len(data) % sample_bytes:
        raise ValueError("pcm must contain complete interleaved 16-bit samples")
    return data


async def _await_result(result: Any) -> Any:
    return await result if inspect.isawaitable(result) else result


async def _disconnect(room_ctx: Any) -> None:
    if hasattr(room_ctx, "__aexit__"):
        await _await_result(room_ctx.__aexit__(None, None, None))
    elif hasattr(room_ctx, "disconnect"):
        await _await_result(room_ctx.disconnect())


class AudioTrackPublisher:
    """Publish PCM audio windows to a LiveKit room."""

    def __init__(
        self,
        session_id: str,
        *,
        room: str | None = None,
        identity: str | None = None,
        env: dict[str, str] | None = None,
        rtc_factory: Any | None = None,
    ) -> None:
        self.session_id = session_id
        self.room_name = room or session_id
        self.identity = identity or f"publisher-{session_id}"
        self._env = env
        self._rtc_factory = rtc_factory
        self._enabled = publish_enabled(env)
        self._started = False
        self._frames_published = 0
        self._room_ctx = None
        self._audio_track = None
        self._audio_source = None
        self._audio_format: tuple[int, int] | None = None
        self._lifecycle_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def started(self) -> bool:
        return self._started

    @property
    def frames_published(self) -> int:
        return self._frames_published

    def _bind_format(self, sample_rate: int, num_channels: int) -> None:
        _validate_format(sample_rate, num_channels)
        requested = (sample_rate, num_channels)
        if self._audio_format is None:
            self._audio_format = requested
        elif self._audio_format != requested:
            expected_rate, expected_channels = self._audio_format
            raise ValueError(
                f"PCM format mismatch: expected {expected_rate}Hz/{expected_channels}ch, "
                f"got {sample_rate}Hz/{num_channels}ch"
            )

    def _clear_resources(self) -> None:
        self._started = False
        self._room_ctx = None
        self._audio_track = None
        self._audio_source = None
        self._audio_format = None

    async def _cleanup_after_failure(self) -> None:
        room_ctx = self._room_ctx
        if room_ctx is None:
            self._clear_resources()
            return
        cleanup_task = asyncio.create_task(_disconnect(room_ctx))
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and hasattr(current, "uncancel"):
                current.uncancel()
            try:
                await cleanup_task
            except BaseException:
                log.debug("livekit cleanup failed", exc_info=True)
            finally:
                self._clear_resources()
            raise
        except BaseException:
            log.debug("livekit cleanup failed", exc_info=True)
        finally:
            self._clear_resources()

    async def _cleanup_cancellation(self) -> None:
        room_ctx = self._room_ctx
        if room_ctx is None:
            self._clear_resources()
            raise asyncio.CancelledError
        cleanup_task = asyncio.create_task(_disconnect(room_ctx))
        current = asyncio.current_task()
        if current is not None and hasattr(current, "uncancel"):
            current.uncancel()
        try:
            await cleanup_task
        except BaseException:
            log.debug("livekit cleanup failed", exc_info=True)
        finally:
            self._clear_resources()
        raise asyncio.CancelledError

    async def _cancelled_start(self) -> None:
        await self._cleanup_cancellation()

    async def _cancelled_capture(self) -> None:
        await self._cleanup_cancellation()

    async def _cancelled_stop(self) -> None:
        await self._cleanup_cancellation()

    async def _start_locked(self) -> None:
        if self._started:
            return
        env = self._env if self._env is not None else os.environ
        url = (env.get("LIVEKIT_URL") or "").strip()
        key = (env.get("LIVEKIT_API_KEY") or "").strip()
        secret = (env.get("LIVEKIT_API_SECRET") or "").strip()
        self._bind_format(*(self._audio_format or (_DEFAULT_SAMPLE_RATE, _DEFAULT_NUM_CHANNELS)))
        room_ctx = self._connect(url, self._mint_publish_token(key, secret))
        self._room_ctx = room_ctx
        try:
            room = await self._enter_room(room_ctx)
            self._audio_track = self._build_audio_track(room)
            await self._publish_track(room, self._audio_track)
            self._started = True
        except asyncio.CancelledError:
            await self._cancelled_start()
        except BaseException:
            await self._cleanup_after_failure()
            raise

    async def start(self) -> None:
        """Connect, create, and publish a local audio track."""
        if not self._enabled:
            log.debug("livekit_publish disabled (LIVEKIT_PUBLISH=1 + LIVEKIT_* creds)")
            return
        async with self._lifecycle_lock:
            await self._start_locked()
        log.info("livekit_publish started session=%s room=%s", self.session_id, self.room_name)

    def _mint_publish_token(self, key: str, secret: str) -> str:
        from .livekit import mint_room_token

        return mint_room_token(
            api_key=key,
            api_secret=secret,
            room=self.room_name,
            identity=self.identity,
            can_publish=True,
            can_subscribe=False,
        )

    def _connect(self, url: str, token: str) -> Any:
        """Return a Room object or async context manager for the given credentials."""
        if self._rtc_factory is not None:
            return self._rtc_factory(url, token)
        try:
            from livekit import rtc  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "livekit-rtc is required when LIVEKIT_PUBLISH=1; install the livekit extra"
            ) from exc
        return _RealRoomCtx(rtc.Room(), url, token)

    @staticmethod
    async def _enter_room(room_ctx: Any) -> Any:
        if hasattr(room_ctx, "__aenter__"):
            return await room_ctx.__aenter__()
        return room_ctx

    def _build_audio_track(self, room: Any) -> Any:
        if self._rtc_factory is not None:
            return getattr(room, "_audio_track", None) or room.track
        from livekit import rtc  # type: ignore

        sample_rate, num_channels = self._audio_format
        self._audio_source = rtc.AudioSource(sample_rate, num_channels)
        return rtc.LocalAudioTrack.create_audio_track(self._audio_source)

    async def _publish_track(self, room: Any, track: Any) -> None:
        participant = getattr(room, "local_participant", None)
        publish_track = getattr(participant, "publish_track", None)
        if publish_track is None:
            raise RuntimeError("LiveKit room has no local participant publish_track method")
        await _await_result(publish_track(track))

    async def _capture_real_pcm(self, pcm: bytes) -> None:
        from livekit import rtc  # type: ignore

        sample_rate, num_channels = self._audio_format
        bytes_per_frame = max(
            num_channels * _PCM_SAMPLE_WIDTH,
            int(sample_rate * num_channels * _PCM_SAMPLE_WIDTH * _FRAME_MS / 1000),
        )
        for index in range(0, len(pcm), bytes_per_frame):
            chunk = pcm[index : index + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                break
            samples_per_channel = len(chunk) // (num_channels * _PCM_SAMPLE_WIDTH)
            frame = rtc.AudioFrame(chunk, sample_rate, num_channels, samples_per_channel)
            await self._audio_source.capture_frame(frame)

    async def _capture_pcm(self, pcm: bytes) -> None:
        sample_rate, num_channels = self._audio_format
        if self._rtc_factory is not None:
            await _await_result(
                self._audio_track.capture_frame(
                    {
                        "pcm": pcm,
                        "sample_rate": sample_rate,
                        "num_channels": num_channels,
                    }
                )
            )
            return
        await self._capture_real_pcm(pcm)

    async def publish_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate: int = _DEFAULT_SAMPLE_RATE,
        num_channels: int = _DEFAULT_NUM_CHANNELS,
    ) -> None:
        """Push one PCM buffer to the room track."""
        if not self._enabled:
            return
        data = _validate_pcm(pcm, sample_rate, num_channels)
        async with self._lifecycle_lock:
            self._bind_format(sample_rate, num_channels)
            await self._start_locked()
            try:
                await self._capture_pcm(data)
            except asyncio.CancelledError:
                await self._cancelled_capture()
            except BaseException:
                await self._cleanup_after_failure()
                raise
            self._frames_published += 1

    async def stop(self) -> None:
        """Disconnect the room; a disconnect failure leaves state retryable."""
        if not self._enabled:
            self._started = False
            return
        async with self._lifecycle_lock:
            if self._room_ctx is None:
                self._started = False
                return
            try:
                await _disconnect(self._room_ctx)
            except asyncio.CancelledError:
                await self._cancelled_stop()
            self._clear_resources()
        log.info(
            "livekit_publish stopped session=%s frames=%s", self.session_id, self._frames_published
        )


@dataclass
class _RegistryEntry:
    publisher: Any
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class LiveKitPublisherRegistry:
    """Own one lazily-created publisher for each active session."""

    def __init__(
        self,
        publisher_factory: Callable[[str], Any] | None = None,
        *,
        enabled_predicate: Callable[[], bool] = publish_enabled,
    ) -> None:
        self._publisher_factory = publisher_factory or AudioTrackPublisher
        self._enabled_predicate = enabled_predicate
        self._entries: dict[str, _RegistryEntry] = {}
        self._active_sessions: set[str] = set()
        self._lock = asyncio.Lock()

    @property
    def session_ids(self) -> tuple[str, ...]:
        return tuple(self._entries)

    def activate(self, session_id: str) -> None:
        self._active_sessions.add(session_id)

    async def publish(self, window: AudioWindow) -> None:
        if not window.pcm or not self._enabled_predicate():
            return
        async with self._lock:
            if window.session_id not in self._active_sessions:
                return
            entry = self._entries.get(window.session_id)
            if entry is None:
                entry = _RegistryEntry(self._publisher_factory(window.session_id))
                self._entries[window.session_id] = entry
        try:
            async with entry.lock:
                async with self._lock:
                    if (
                        window.session_id not in self._active_sessions
                        or self._entries.get(window.session_id) is not entry
                    ):
                        return
                await entry.publisher.publish_pcm(
                    window.pcm,
                    sample_rate=window.sample_rate,
                    num_channels=1,
                )
        except BaseException:
            await self._remove_after_failure(window.session_id, entry)
            raise

    async def _remove_after_failure(self, session_id: str, entry: _RegistryEntry) -> None:
        async with self._lock:
            if self._entries.get(session_id) is entry:
                self._entries.pop(session_id, None)
        try:
            async with entry.lock:
                await entry.publisher.stop()
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            log.error(
                "LiveKit publisher cleanup failed session=%s error_type=%s",
                session_id,
                type(exc).__name__,
            )

    async def stop(self, session_id: str) -> None:
        async with self._lock:
            self._active_sessions.discard(session_id)
            entry = self._entries.pop(session_id, None)
        if entry is None:
            return
        async with entry.lock:
            await entry.publisher.stop()

    async def stop_all(self) -> None:
        async with self._lock:
            self._active_sessions.clear()
            entries = list(self._entries.items())
            self._entries.clear()
        for session_id, entry in entries:
            try:
                async with entry.lock:
                    await entry.publisher.stop()
            except asyncio.CancelledError:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                log.error(
                    "LiveKit publisher cleanup failed session=%s error_type=%s",
                    session_id,
                    type(exc).__name__,
                )


class _RealRoomCtx:
    """Async context manager wrapper for a real livekit-rtc Room."""

    def __init__(self, room: Any, url: str, token: str) -> None:
        self._room = room
        self._url = url
        self._token = token

    async def __aenter__(self) -> Any:
        await self._room.connect(self._url, self._token)
        return self._room

    async def __aexit__(self, *exc: object) -> bool:
        await self._room.disconnect()
        return False
