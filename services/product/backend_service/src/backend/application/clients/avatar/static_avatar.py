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
    unconfirmed: set[str] = field(default_factory=set)  # no audio was accepted for these
    video_failed: bool = False
    closing: bool = False


@dataclass(eq=False)  # identity-hashed: kept in a set
class _Startup:
    """One in-flight start(). Every field is read and written under the backend lock."""

    abandoned: bool = False  # the caller gave up, or stop_all ran: never register a session
    sid: str | None = None  # set when the session was registered
    fut: Any = None
    task: Any = None  # the asyncio task, once its body started


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
        self._starting: set[_Startup] = set()  # in flight until the task body has finished
        self._orphans: list[Any] = []  # rooms whose cleanup failed; stop_all retries them

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
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(target=loop.run_forever, daemon=True, name="static-avatar").start()
                self._loop = loop
            return self._loop

    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result(timeout)

    # ---- RenderBackend ---------------------------------------------
    def start(self, opts: StartOptions) -> StartResult:
        st = _Startup()
        with self._lock:  # owned from here on, before the loop has seen it
            self._starting.add(st)
        try:
            loop = self._ensure_loop()
            st.fut = asyncio.run_coroutine_threadsafe(self._start(st), loop)
            return st.fut.result(self._s.connect_timeout_s + 10)
        except BaseException:
            self._abandon(st)
            raise
        finally:
            with self._lock:
                if st.task is None:  # the body never began: nothing else will discard it
                    self._starting.discard(st)

    def _abandon(self, st: _Startup) -> None:
        """The caller gave up: only ITS startup is cancelled, and a session it never saw is closed."""
        with self._lock:
            st.abandoned = True
            sid = st.sid
        if st.fut is not None:
            st.fut.cancel()
        if sid:
            try:
                self.stop(sid)
            except Exception as exc:  # still owned in _sessions: stop_all retries it
                log.error("static_avatar cleanup failed error_type=%s", type(exc).__name__)

    async def _start(self, st: _Startup) -> StartResult:
        try:
            with self._lock:
                st.task = asyncio.current_task()
                if st.abandoned:
                    raise asyncio.CancelledError
            return await self._start_inner(st)
        finally:
            with self._lock:
                self._starting.discard(st)

    async def _start_inner(self, st: _Startup) -> StartResult:
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
            async with asyncio.timeout(s.connect_timeout_s):  # connect AND both publishes
                await room.connect(s.livekit_url, token)
                frame = await asyncio.to_thread(render_frame, s)
                audio, pump = await self._media_factory(room, s, frame)
        except BaseException:
            await self._disconnect(room)
            raise
        sess = _Sess(room, audio)
        with self._lock:  # check + register together: stop_all/_abandon cannot slip in between
            abandoned = st.abandoned
            if not abandoned:
                self._sessions[room_name] = sess
                st.sid = room_name
        if abandoned:
            await self._disconnect(room)
            raise asyncio.CancelledError
        sess.video_task = asyncio.ensure_future(pump())
        sess.video_task.add_done_callback(lambda t: self._video_done(sess, t))
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

    @staticmethod
    def _video_done(sess: _Sess, task: asyncio.Future) -> None:
        if not task.cancelled() and task.exception() is not None:
            sess.video_failed = True
            log.error(
                "static_avatar video pump died error_type=%s", type(task.exception()).__name__
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
        try:
            await self._send(sess, w)
        except BaseException:
            if w.utterance_id not in sess.playback_started_at:
                sess.unconfirmed.add(w.utterance_id)
            raise

    async def _send(self, sess: _Sess, w: AudioWindow) -> None:
        rate = self._s.audio_sample_rate
        rs = sess.resamplers.get(w.utterance_id)
        if rs is None:
            rs = sess.resamplers[w.utterance_id] = UtteranceResampler(w.sample_rate, rate)
        pcm = rs.push(w.pcm)
        if w.is_final:
            pcm += rs.flush()
            sess.resamplers.pop(w.utterance_id, None)
        sess.open_utterance = w.utterance_id
        epoch, step = sess.epoch, rate * 2 * _CHUNK_MS // 1000
        for i in range(0, len(pcm), step):
            if epoch != sess.epoch or sess.closing:
                return  # interrupted: drop the rest
            await sess.audio_source.capture(pcm[i : i + step])
            if w.utterance_id not in sess.playback_started_at:  # only after audio was accepted
                sess.playback_started_at[w.utterance_id] = time.time()
                while len(sess.playback_started_at) > _HISTORY:
                    sess.playback_started_at.popitem(last=False)
        if w.is_final:
            if sess.open_utterance == w.utterance_id:  # a newer utterance may own it by now
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
        sess = self._sessions.get(session_id)
        if sess is None:
            return
        # Ownership is kept until the room is really gone, so a failed teardown can be retried.
        self._run(self._close(sess), 15)
        self._sessions.pop(session_id, None)

    async def _close(self, sess: _Sess) -> None:
        sess.closing = True
        if sess.video_task is not None:
            sess.video_task.cancel()
            await asyncio.gather(sess.video_task, return_exceptions=True)
        await asyncio.wait_for(sess.room.disconnect(), 10)

    async def _disconnect(self, room: Any) -> None:
        # Failed/abandoned startup: the room is owned (an orphan) BEFORE the attempt and released
        # only when it succeeded, so an error or a cancellation can never forget it.
        self._orphans.append(room)
        try:
            await asyncio.wait_for(room.disconnect(), 10)
        except Exception as exc:
            log.warning("static_avatar disconnect failed error_type=%s", type(exc).__name__)
        else:
            if room in self._orphans:
                self._orphans.remove(room)

    async def _join(self, tasks: tuple[Any, ...]) -> None:
        await asyncio.gather(*tasks, return_exceptions=True)

    def stop_all(self) -> None:
        with self._lock:  # snapshot + flag together with the same lock that registers sessions
            pending = tuple(self._starting)
            for st in pending:
                st.abandoned = True
            tasks = tuple(st.task for st in pending if st.task is not None)
        for st in pending:
            if st.fut is not None:
                st.fut.cancel()
        if tasks:  # let each cancelled startup finish its own cleanup first
            self._run(self._join(tasks), 20)
        failed = 0
        for room in tuple(self._orphans):
            try:
                self._run(asyncio.wait_for(room.disconnect(), 10), 15)
                if room in self._orphans:
                    self._orphans.remove(room)
            except Exception as exc:
                failed += 1
                log.error("static_avatar orphan cleanup failed error_type=%s", type(exc).__name__)
        for sid in tuple(self._sessions):
            try:
                self.stop(sid)
            except Exception as exc:
                failed += 1
                log.error(
                    "static_avatar stop failed session=%s error_type=%s", sid, type(exc).__name__
                )
        if failed:
            raise RuntimeError(f"static_avatar teardown incomplete for {failed} session(s)")

    def session_status(self, session_id: str) -> str:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        return "video_failed" if sess.video_failed else "active"

    def playback_info(self, session_id: str, utterance_id: str) -> dict[str, Any] | None:
        sess = self._sessions.get(session_id)
        if sess is None:
            return None
        return {
            "playback_started_at": sess.playback_started_at.get(utterance_id),
            "playback_unconfirmed": utterance_id in sess.unconfirmed,
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
