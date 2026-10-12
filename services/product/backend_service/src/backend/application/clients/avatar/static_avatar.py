"""``static_avatar`` render backend: a free stand-in for a paid avatar provider.

It joins the LiveKit room as the avatar participant (same identity the API's media join expects)
and publishes ONE video track (a still image, repeated) and ONE audio track (the host's TTS).
No provider, no credit, no lip-sync: it exists to exercise the whole live flow (prepare, media
join, egress to Facebook, Q&A, auto end) without spending anything. Never a production avatar.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from backend.application.publishing.datastream import UtteranceResampler
from backend.application.publishing.livekit import mint_room_token
from backend.application.render.engines_base import (
    StartOptions,
    StartResult,
    StreamingAvatarBackend,
)
from backend.application.render.windows import AudioWindow

log = logging.getLogger(__name__)

_CHUNK_MS = 100
_HISTORY = 256


@dataclass
class StaticAvatarSettings:
    livekit_url: str
    livekit_api_key: str = field(repr=False)
    livekit_api_secret: str = field(repr=False)
    avatar_identity: str = "lemonslice-avatar-agent"
    image_path: str = ""  # empty = a generated placeholder card
    width: int = 1280
    height: int = 720
    fps: int = 10
    audio_sample_rate: int = 24000
    connect_timeout_s: float = 20.0
    token_ttl_s: int = 3600


@dataclass
class _Sess:
    room: Any
    audio_source: Any
    video_task: asyncio.Future | None = None
    epoch: int = 0
    open_utterance: str = ""
    cleared: set[str] = field(default_factory=set)
    resamplers: dict[str, UtteranceResampler] = field(default_factory=dict)
    playback_started_at: OrderedDict[str, float] = field(default_factory=OrderedDict)
    closing: bool = False


def render_frame(settings: StaticAvatarSettings) -> bytes:
    """RGBA bytes of the still image, letterboxed to width x height."""
    from PIL import Image, ImageDraw

    w, h = settings.width, settings.height
    canvas = Image.new("RGBA", (w, h), (18, 18, 24, 255))
    if settings.image_path:
        img = Image.open(settings.image_path).convert("RGBA")
        img.thumbnail((w, h))
        canvas.paste(img, ((w - img.width) // 2, (h - img.height) // 2))
    else:
        ImageDraw.Draw(canvas).text((w // 2 - 60, h // 2), "Livento (static avatar)", fill="white")
    return canvas.tobytes()


class StaticAvatarRenderBackend(StreamingAvatarBackend):
    name = "static_avatar"

    def __init__(self, settings: StaticAvatarSettings, *, room_factory=None, media_factory=None):
        self._s = settings
        self._room_factory = room_factory or self._default_room
        self._media_factory = media_factory or self._default_media
        self._sessions: dict[str, _Sess] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    # ---- livekit seams (replaced in tests) --------------------------
    @staticmethod
    def _default_room() -> Any:
        from livekit import rtc  # type: ignore

        return rtc.Room()

    @staticmethod
    async def _default_media(room: Any, s: StaticAvatarSettings, frame: bytes):
        """Publish the video + audio tracks; return (audio_source, video_pump coroutine)."""
        from livekit import rtc  # type: ignore

        video = rtc.VideoSource(s.width, s.height)
        audio = rtc.AudioSource(s.audio_sample_rate, 1)
        lp = room.local_participant
        await lp.publish_track(
            rtc.LocalVideoTrack.create_video_track("livento-video", video),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA),
        )
        await lp.publish_track(
            rtc.LocalAudioTrack.create_audio_track("livento-audio", audio),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        vf = rtc.VideoFrame(s.width, s.height, rtc.VideoBufferType.RGBA, frame)

        async def pump() -> None:
            while True:
                video.capture_frame(vf)
                await asyncio.sleep(1 / s.fps)

        async def capture(pcm: bytes) -> None:
            await audio.capture_frame(rtc.AudioFrame(pcm, s.audio_sample_rate, 1, len(pcm) // 2))

        return _AudioIO(audio, capture), pump

    # ---- loop thread ------------------------------------------------
    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, daemon=True, name="static-avatar").start()
                self._loop = loop
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    # ---- RenderBackend ---------------------------------------------
    def start(self, opts: StartOptions) -> StartResult:
        return self._run(self._start(), self._s.connect_timeout_s + 10)

    async def _start(self) -> StartResult:
        s = self._s
        room_name = f"rt_{uuid.uuid4().hex}"
        room = self._room_factory()
        token = mint_room_token(
            api_key=s.livekit_api_key,
            api_secret=s.livekit_api_secret,
            room=room_name,
            identity=s.avatar_identity,
            name=s.avatar_identity,
            ttl_sec=s.token_ttl_s,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            kind="agent",
        )
        try:
            await asyncio.wait_for(room.connect(s.livekit_url, token), s.connect_timeout_s)
            audio, pump = await self._media_factory(
                room, s, await asyncio.to_thread(render_frame, s)
            )
        except BaseException:
            await self._disconnect(room)
            raise
        sess = _Sess(room, audio)
        sess.video_task = asyncio.ensure_future(pump())
        self._sessions[room_name] = sess
        viewer = mint_room_token(
            api_key=s.livekit_api_key,
            api_secret=s.livekit_api_secret,
            room=room_name,
            identity=f"viewer-{uuid.uuid4().hex[:12]}",
            ttl_sec=s.token_ttl_s,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=False,
        )
        log.warning("static_avatar started session=%s (test avatar, no provider)", room_name)
        return StartResult(
            session_id=room_name,
            livekit_url=s.livekit_url,
            livekit_client_token=viewer,
            mode="STATIC",
        )

    def stream_audio(self, session_id: str, audio_window: AudioWindow):
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        if audio_window.pcm is None:
            raise ValueError("static_avatar requires in-memory PCM windows")
        self._run(self._stream(sess, audio_window))
        return iter(())

    async def _stream(self, sess: _Sess, w: AudioWindow) -> None:
        if sess.closing or w.utterance_id in sess.cleared:
            return
        rate = self._s.audio_sample_rate
        rs = sess.resamplers.setdefault(w.utterance_id, UtteranceResampler(w.sample_rate, rate))
        pcm = rs.push(w.pcm)
        if w.is_final:
            pcm += rs.flush()
            sess.resamplers.pop(w.utterance_id, None)
        sess.open_utterance = w.utterance_id
        sess.playback_started_at.setdefault(w.utterance_id, time.time())
        while len(sess.playback_started_at) > _HISTORY:
            sess.playback_started_at.popitem(last=False)
        epoch, step = sess.epoch, rate * 2 * _CHUNK_MS // 1000
        for i in range(0, len(pcm), step):
            if epoch != sess.epoch or sess.closing:
                return  # interrupted: drop the rest
            await sess.audio_source.capture(pcm[i : i + step])
        if w.is_final:
            sess.open_utterance = ""
            if epoch == sess.epoch:
                await asyncio.wait_for(sess.audio_source.wait_for_playout(), 30)

    def interrupt(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        self._run(self._interrupt(sess))

    async def _interrupt(self, sess: _Sess) -> None:
        sess.epoch += 1
        if sess.open_utterance:
            sess.cleared.add(sess.open_utterance)
            sess.resamplers.pop(sess.open_utterance, None)
            sess.open_utterance = ""
        sess.audio_source.clear_queue()

    def stop(self, session_id: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if sess is not None:
            self._run(self._close(sess), 15)

    async def _close(self, sess: _Sess) -> None:
        sess.closing = True
        if sess.video_task is not None:
            sess.video_task.cancel()
        await self._disconnect(sess.room)

    @staticmethod
    async def _disconnect(room: Any) -> None:
        try:
            await asyncio.wait_for(room.disconnect(), 10)
        except Exception as exc:  # teardown must not mask the real failure
            log.warning("static_avatar disconnect failed error_type=%s", type(exc).__name__)

    def stop_all(self) -> None:
        for sid in tuple(self._sessions):
            self.stop(sid)

    def session_status(self, session_id: str) -> str:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        return "active"

    def playback_info(self, session_id: str, utterance_id: str) -> dict[str, Any] | None:
        sess = self._sessions.get(session_id)
        if sess is None:
            return None
        return {
            "playback_started_at": sess.playback_started_at.get(utterance_id),
            "playback_unconfirmed": False,
        }


class _AudioIO:
    """Thin wrapper so tests can stub capture / playout / clear without livekit."""

    def __init__(self, source: Any, capture: Any) -> None:
        self.capture = capture
        self._source = source

    def clear_queue(self) -> None:
        self._source.clear_queue()

    async def wait_for_playout(self) -> None:
        await self._source.wait_for_playout()
