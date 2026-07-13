"""LiveKit audio publish stub (backend → SFU).

No-op unless LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET are set AND
LIVEKIT_PUBLISH=1. Real SDK integration (livekit package / rtc) is Wave media.

Usage (future):
    pub = AudioTrackPublisher(session_id=sid, room=sid)
    await pub.start()
    await pub.publish_pcm(pcm_bytes, sample_rate=24000)
    await pub.stop()

Real integration outline:
  1. pip install livekit livekit-api
  2. mint can_publish token via core.livekit_tokens.mint_room_token
  3. rtc.Room.connect(LIVEKIT_URL, token)
  4. LocalAudioTrack from PCM frames → room.local_participant.publish_track
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)


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
    """Publish PCM audio windows to a LiveKit room (stub).

    Methods are async-compatible no-ops when publish is disabled so the
    stream path can call them unconditionally.
    """

    def __init__(
        self,
        session_id: str,
        *,
        room: Optional[str] = None,
        identity: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.session_id = session_id
        self.room = room or session_id
        self.identity = identity or f"publisher-{session_id}"
        self._env = env
        self._started = False
        self._enabled = publish_enabled(env)
        self._frames_published = 0

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
        """Connect and create a local audio track (no-op when disabled)."""
        if not self._enabled:
            log.debug(
                "livekit_publish disabled (set LIVEKIT_PUBLISH=1 + LIVEKIT_* creds)"
            )
            return
        # Real path: connect rtc.Room + publish LocalAudioTrack.
        # Left as stub until livekit SDK is a hard dep for the backend image.
        log.info(
            "livekit_publish start stub session=%s room=%s (SDK not wired)",
            self.session_id,
            self.room,
        )
        self._started = True

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
        # Real path: convert PCM → AudioFrame → track.capture_frame
        _ = (pcm, sample_rate, num_channels)
        self._frames_published += 1

    async def stop(self) -> None:
        """Unpublish and disconnect (no-op when disabled)."""
        if not self._enabled:
            self._started = False
            return
        log.info(
            "livekit_publish stop stub session=%s frames=%s",
            self.session_id,
            self._frames_published,
        )
        self._started = False
