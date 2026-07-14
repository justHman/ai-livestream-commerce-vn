"""LiveKit audio publish (backend -> SFU).

No-op unless LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET are set AND
LIVEKIT_PUBLISH=1. When enabled, connects a real livekit-rtc Room, publishes
a local audio track, and pushes PCM frames converted to 20ms AudioFrames.

``livekit-rtc`` is an OPTIONAL dependency: it is imported lazily inside
``start()`` only on the enabled path, so the offline/Colab image (which does
not install it) imports this module and runs the disabled path with zero
cost. Tests inject an ``rtc_factory`` seam instead of the real SDK.

Usage (production):
    pub = AudioTrackPublisher(session_id=sid, env=os.environ)
    await pub.start()
    await pub.publish_pcm(pcm_bytes, sample_rate=24000)
    await pub.stop()
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

# 20ms frame at 24kHz mono 16-bit = 480 samples = 960 bytes.
_FRAME_MS = 20


def publish_enabled(env: Optional[dict[str, str]] = None) -> bool:
    """True only when publish flag + LiveKit credentials are all present."""
    source = env if env is not None else os.environ
    flag = str(source.get("LIVEKIT_PUBLISH", "0")).lower() in (
        "1",
        "true",
        "on",
        "yes",
    )
    if not flag:
        return False
    url = (source.get("LIVEKIT_URL") or "").strip()
    key = (source.get("LIVEKIT_API_KEY") or "").strip()
    secret = (source.get("LIVEKIT_API_SECRET") or "").strip()
    return bool(url and key and secret)


class AudioTrackPublisher:
    """Publish PCM audio windows to a LiveKit room.

    When disabled (the default), every method is an async no-op so the stream
    path can call them unconditionally. When enabled, ``start()`` connects a
    livekit-rtc Room (or a test-injected fake) and publishes a local audio
    track; ``publish_pcm`` converts PCM to 20ms AudioFrames and captures them.
    """

    def __init__(
        self,
        session_id: str,
        *,
        room: Optional[str] = None,
        identity: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        rtc_factory: Optional[Any] = None,
    ) -> None:
        self.session_id = session_id
        self.room_name = room or session_id
        self.identity = identity or f"publisher-{session_id}"
        self._env = env
        self._rtc_factory = rtc_factory
        self._started = False
        self._enabled = publish_enabled(env)
        self._frames_published = 0
        self._room_ctx = None  # async context manager (real or fake)
        self._audio_track = None  # track exposing capture_frame
        self._audio_source = None  # rtc.AudioSource (real SDK frame sink)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def started(self) -> bool:
        return self._started

    @property
    def frames_published(self) -> int:
        return self._frames_published

    async def start(self) -> None:
        """Connect, create + publish a local audio track (no-op when disabled)."""
        if not self._enabled:
            log.debug("livekit_publish disabled (LIVEKIT_PUBLISH=1 + LIVEKIT_* creds)")
            return
        env = self._env if self._env is not None else os.environ
        url = (env.get("LIVEKIT_URL") or "").strip()
        key = (env.get("LIVEKIT_API_KEY") or "").strip()
        secret = (env.get("LIVEKIT_API_SECRET") or "").strip()

        token = self._mint_publish_token(key, secret)
        self._room_ctx = self._connect(url, token)
        room = await self._enter_room(self._room_ctx)
        self._audio_track = self._build_audio_track(room)
        await self._publish_track(room, self._audio_track)
        self._started = True
        log.info("livekit_publish started session=%s room=%s", self.session_id, self.room_name)

    def _mint_publish_token(self, key: str, secret: str) -> str:
        from .livekit_tokens import mint_room_token

        return mint_room_token(
            api_key=key,
            api_secret=secret,
            room=self.room_name,
            identity=self.identity,
            can_publish=True,
            can_subscribe=False,
        )

    def _connect(self, url: str, token: str):
        """Return a Room object/context-manager for the given url+token."""
        if self._rtc_factory is not None:
            return self._rtc_factory(url, token)
        try:
            from livekit import rtc  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "livekit-rtc is required when LIVEKIT_PUBLISH=1; "
                "pip install livekit-rtc (uv add livekit-rtc)"
            ) from exc
        room = rtc.Room()
        # ponytail: real connect is awaitable; wrap in a tiny async CM so the
        # __aenter__/__aexit__ seam below works uniformly. Ceiling: when the
        # real SDK's Room is itself an async CM, drop this wrapper.
        return _RealRoomCtx(room, url, token)

    @staticmethod
    async def _enter_room(room_ctx):
        if hasattr(room_ctx, "__aenter__"):
            return await room_ctx.__aenter__()
        return room_ctx

    def _build_audio_track(self, room):
        if self._rtc_factory is not None:
            return getattr(room, "_audio_track", None) or room.track
        from livekit import rtc  # type: ignore

        # Real SDK: AudioSource is the frame sink (has capture_frame), wrapped
        # in a LocalAudioTrack for publishing.
        self._audio_source = rtc.AudioSource(24000, 1)
        track = rtc.LocalAudioTrack.create_audio_track(self._audio_source)
        return track

    @staticmethod
    async def _publish_track(room, track):
        if hasattr(room, "local_participant"):
            await room.local_participant.publish_track(track)

    async def publish_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate: int = 24_000,
        num_channels: int = 1,
    ) -> None:
        """Push one PCM buffer to the room track (no-op when disabled)."""
        if not self._enabled:
            return
        if not self._started:
            await self.start()
        await self._capture_pcm(self._audio_track, pcm, sample_rate, num_channels)
        self._frames_published += 1

    async def _capture_pcm(self, track, pcm, sample_rate, num_channels):
        if self._rtc_factory is not None:
            track.capture_frame({"pcm": pcm, "sample_rate": sample_rate})
            return
        from livekit import rtc  # type: ignore

        # Real SDK: push 20ms AudioFrames to the AudioSource sink.
        sink = self._audio_source
        bytes_per_frame = int(sample_rate * num_channels * 2 * _FRAME_MS / 1000)
        for i in range(0, len(pcm), bytes_per_frame):
            chunk = pcm[i : i + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                break
            samples_per_channel = len(chunk) // (num_channels * 2)
            frame = rtc.AudioFrame(chunk, sample_rate, num_channels, samples_per_channel)
            await sink.capture_frame(frame)

    async def stop(self) -> None:
        """Unpublish and disconnect (no-op when disabled)."""
        if not self._enabled:
            self._started = False
            return
        if self._room_ctx is not None and hasattr(self._room_ctx, "__aexit__"):
            await self._room_ctx.__aexit__(None, None, None)
        self._room_ctx = None
        self._audio_track = None
        self._audio_source = None
        log.info(
            "livekit_publish stopped session=%s frames=%s", self.session_id, self._frames_published
        )
        self._started = False


class _RealRoomCtx:
    """Async CM wrapper over a real livekit-rtc Room so the seam is uniform.

    ponytail: ceiling — when livekit-rtc Room itself implements __aenter__,
    delete this class and return room from _connect directly.
    """

    def __init__(self, room, url: str, token: str) -> None:
        self._room = room
        self._url = url
        self._token = token

    async def __aenter__(self):
        await self._room.connect(self._url, self._token)
        return self._room

    async def __aexit__(self, *exc):
        try:
            await self._room.disconnect()
        except Exception:
            log.debug("livekit disconnect failed", exc_info=True)
        return False
