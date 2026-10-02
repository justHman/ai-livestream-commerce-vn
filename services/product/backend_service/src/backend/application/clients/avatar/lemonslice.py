"""``cloud_lemonslice`` render backend (P0-FB-010, DR-MEDIA-001 Option B1).

The LemonSlice avatar joins a Livento-owned LiveKit room (room name = runtime
session id). The Runtime stays the single speaker: guarded TTS PCM is
resampled and sent over the LiveKit avatar data-stream protocol to the avatar
participant only. LemonSlice's own LLM/TTS is never enabled.

The seam is sync; ``livekit-rtc`` runs on one backend-owned event-loop thread.
``stream_audio`` yields no VideoWindow (the provider renders in the room).

UNVERIFIED ASSUMPTIONS (gate external proof 023, not local implementation):
  LS1  ``POST {api_base}/sessions`` body/headers and the response ``session_id``;
       the terminate endpoint is unknown, so it is configuration
       (``LEMONSLICE_TERMINATE_PATH``, empty = leave the room and rely on
       ``idle_timeout``).
  LS2  the avatar publishes video (and audio); ``lk.audio_stream`` accepts 16 kHz.
       If it publishes video only, ``AVATAR_AUDIO_FALLBACK_PUBLISH`` makes the
       Runtime publish the same PCM as one delayed audio track.
  LS3  whether silence counts toward ``idle_timeout``; no keep-alive is sent.
  LK   JWT claim names for ``kind=agent`` / ``lk.publish_on_behalf``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from backend.application.publishing.datastream import AvatarAudioChannel
from backend.application.publishing.livekit import mint_room_token
from backend.application.render.engines_base import (
    StartOptions,
    StartResult,
    StreamingAvatarBackend,
)
from backend.application.render.windows import AudioWindow

log = logging.getLogger(__name__)

_KIND_VIDEO = 2  # livekit TrackKind.KIND_VIDEO; used when livekit-rtc is not importable
_KIND_AUDIO = 1


@dataclass(frozen=True)
class LemonSliceSettings:
    livekit_url: str
    livekit_api_key: str
    livekit_api_secret: str
    lemonslice_api_key: str
    api_base: str = "https://lemonslice.com/api/liveai"
    agent_id: str = ""
    avatar_allowlist: tuple[str, ...] = ()
    audio_sample_rate: int = 16000
    idle_timeout_s: int = 60
    ready_timeout_s: float = 30.0
    request_timeout_s: float = 15.0
    avatar_identity: str = "lemonslice-avatar-agent"
    runtime_identity: str = "livento-runtime"
    fallback_publish: bool = False
    render_offset_ms: int = 0
    terminate_path: str = ""
    playback_margin_s: float = 5.0
    avatar_token_ttl_s: int = 300
    client_token_ttl_s: int = 3600
    io_timeout_s: float = 5.0  # stream open/write/close and room disconnect deadline
    audio_probe_s: float = 1.0  # how long to look for an avatar audio track before fallback
    fallback_max_queue: int = 512  # bounded fallback buffer (chunks)
    history: int = 256  # bounded playback-history collections
    terminate_attempts: int = 2
    tombstone_ttl_s: float = 2.0  # how long an interrupted utterance absorbs its late events

    def __repr__(self) -> str:  # never print secrets
        return "LemonSliceSettings(<redacted>)"


class LemonSliceError(RuntimeError):
    """Typed, redacted backend error: ``code`` and a fixed message only (no URL, key or cause)."""

    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


class AvatarAudioFallbackRefused(LemonSliceError):
    """The avatar publishes its own audio, so a Runtime fallback track would double the audio."""

    def __init__(self) -> None:
        super().__init__(
            "avatar_audio_fallback_refused",
            "AVATAR_AUDIO_FALLBACK_PUBLISH refused: the avatar already publishes audio",
        )


class _BoundedSet:
    def __init__(self, cap: int) -> None:
        self._d: OrderedDict[str, None] = OrderedDict()
        self._cap = cap

    def add(self, k: str) -> None:
        self._d[k] = None
        while len(self._d) > self._cap:
            self._d.popitem(last=False)

    def __contains__(self, k: object) -> bool:
        return k in self._d


HttpPost = Callable[[str, dict[str, str], dict[str, Any], float], tuple[int, dict[str, Any]]]


def _httpx_post(url: str, headers: dict, body: dict, timeout: float) -> tuple[int, dict]:
    import httpx

    r = httpx.post(url, headers=headers, json=body, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {}
    return r.status_code, data if isinstance(data, dict) else {}


class _FallbackAudioTrack:
    """Runtime-owned audio track for the video-only avatar case (delayed, bounded).

    ``veto`` is asked right before every push and after it: when the avatar turns out to
    publish its own audio the track stops pushing and ``on_veto`` fails the session closed.
    """

    def __init__(
        self,
        capture: Callable[[bytes], Any],
        offset_ms: int,
        clear: Callable[[], Any] | None = None,
        max_queue: int = 512,
        veto: Callable[[], bool] | None = None,
        on_veto: Callable[[], None] | None = None,
        unpublish: Callable[[], Any] | None = None,
    ) -> None:
        self._capture = capture
        self._clear = clear
        self._veto = veto
        self._on_veto = on_veto
        self.unpublish = unpublish
        self._offset = offset_ms / 1000
        self._max = max_queue
        self._q: asyncio.Queue = asyncio.Queue()
        self._gen = 0
        self._stopped = False
        self._task = asyncio.ensure_future(self._run())

    def enqueue(self, pcm: bytes) -> None:
        if self._stopped:
            return
        if self._q.qsize() >= self._max:
            self._q.get_nowait()  # bounded: drop the oldest chunk
        self._q.put_nowait((self._gen, time.monotonic() + self._offset, pcm))

    def drain(self) -> None:
        """Interrupt: drop queued PCM, cancel the sleeping/pushing task, clear the real buffer."""
        self._gen += 1
        while not self._q.empty():
            self._q.get_nowait()
        self._task.cancel()
        if self._clear is not None:
            try:
                self._clear()  # e.g. rtc.AudioSource.clear_queue
            except Exception as exc:
                log.warning("fallback buffer clear failed error_type=%s", type(exc).__name__)
        if not self._stopped:
            self._task = asyncio.ensure_future(self._run())

    def _vetoed(self) -> bool:
        if self._veto is None or not self._veto():
            return False
        if not self._stopped and self._on_veto is not None:
            self._stopped = True
            self._on_veto()
        return True

    async def _run(self) -> None:
        gen = self._gen
        while not self._stopped:
            g, due, pcm = await self._q.get()
            await asyncio.sleep(max(0.0, due - time.monotonic()))
            if g != self._gen or gen != self._gen:  # interrupted while it slept
                continue
            if self._vetoed():
                return
            res = self._capture(pcm)
            if asyncio.iscoroutine(res):
                await res
            if self._vetoed():
                return

    def close(self) -> None:
        self._stopped = True
        self._task.cancel()


@dataclass
class _Sess:
    room: Any
    channel: AvatarAudioChannel
    provider_session_id: str = ""
    cleared: _BoundedSet = field(default_factory=lambda: _BoundedSet(256))
    unconfirmed: _BoundedSet = field(default_factory=lambda: _BoundedSet(256))
    fallback: _FallbackAudioTrack | None = None
    refused: bool = False  # avatar audio was detected while the fallback ran: fail closed

    @property
    def epoch(self) -> int:
        return self.channel.epoch


class LemonSliceRenderBackend(StreamingAvatarBackend):
    name = "cloud_lemonslice"

    def __init__(
        self,
        settings: LemonSliceSettings,
        *,
        room_factory: Callable[[], Any] | None = None,
        http_post: HttpPost | None = None,
        audio_track_factory: Callable[[Any, int], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._s = settings
        self._room_factory = room_factory or self._default_room
        self._post = http_post or _httpx_post
        self._track_factory = audio_track_factory or self._default_track
        self._clock = clock
        self._sessions: dict[str, _Sess] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._tasks: set[asyncio.Task] = set()  # startups/cleanups in flight (loop thread)
        self._reapers: set[asyncio.Task] = set()  # late-REST-session cleanups (loop thread)
        self._loop_lock = threading.Lock()

    # ---- loop thread ------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                self._thread = threading.Thread(
                    target=loop.run_forever, name="lemonslice-loop", daemon=True
                )
                self._thread.start()
                self._loop = loop
            return self._loop

    async def _tracked(self, coro: Any) -> Any:
        """Loop-thread wrapper so stop_all() can find and await every in-flight operation."""
        task = asyncio.current_task()
        assert task is not None
        self._tasks.add(task)
        try:
            return await coro
        finally:
            self._tasks.discard(task)

    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        fut = asyncio.run_coroutine_threadsafe(self._tracked(coro), self._ensure_loop())
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            pass
        fut.cancel()  # never leave the coroutine running after the caller gave up
        raise LemonSliceError("timeout", "LemonSlice backend call timed out")

    async def _drain(self, *, startups: bool) -> None:
        """Cancel/await startup tasks (cleanup runs inside them) or, later, late-session reapers."""
        me = asyncio.current_task()
        pool = self._tasks if startups else self._reapers
        pending = [t for t in pool if t is not me and not t.done()]
        if startups:
            for t in pending:
                t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=self._s.request_timeout_s * 3 + 5)
        for t in pending:
            if not t.done():
                t.cancel()

    def _shutdown_loop(self) -> None:
        with self._loop_lock:
            loop, thread, self._loop, self._thread = self._loop, self._thread, None, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
            if thread is not None:
                thread.join(timeout=5)
            if not loop.is_running():
                loop.close()

    @staticmethod
    def _default_room() -> Any:
        from livekit import rtc  # type: ignore

        return rtc.Room()

    @staticmethod
    def _default_track(room: Any, sample_rate: int) -> Any:
        from livekit import rtc  # type: ignore

        source = rtc.AudioSource(sample_rate, 1)
        track = rtc.LocalAudioTrack.create_audio_track("livento-audio", source)

        publication: list[Any] = []

        async def publish() -> None:
            publication.append(
                await room.local_participant.publish_track(
                    track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
                )
            )

        async def unpublish() -> None:
            if publication:
                await room.local_participant.unpublish_track(publication.pop().sid)

        async def capture(pcm: bytes) -> None:
            n = len(pcm) // 2
            await source.capture_frame(rtc.AudioFrame(pcm, sample_rate, 1, n))

        return publish, capture, source.clear_queue, unpublish

    # ---- RenderBackend ---------------------------------------------
    def start(self, opts: StartOptions) -> StartResult:
        agent_id = self._resolve_agent(opts.avatar_id)
        return self._run(
            self._start(agent_id), self._s.ready_timeout_s + self._s.request_timeout_s + 10
        )

    def _resolve_agent(self, avatar_id: str | None) -> str:
        # DR-AVATAR-001: preset avatars only. A caller-supplied id must be allowlisted.
        if avatar_id and avatar_id != self._s.agent_id:
            if avatar_id not in self._s.avatar_allowlist:
                raise LemonSliceError(
                    "avatar_not_allowed", "avatar_id is not in the LEMONSLICE avatar allowlist"
                )
            return avatar_id
        if not self._s.agent_id:
            raise LemonSliceError("agent_not_configured", "LEMONSLICE_AGENT_ID is not configured")
        return self._s.agent_id

    async def _start(self, agent_id: str) -> StartResult:
        s = self._s
        room_name = f"rt_{uuid.uuid4().hex}"
        room = self._room_factory()
        # Runtime participant: data-only (no media) unless the flagged audio fallback is on.
        runtime_token = mint_room_token(
            api_key=s.livekit_api_key,
            api_secret=s.livekit_api_secret,
            room=room_name,
            identity=s.runtime_identity,
            ttl_sec=s.client_token_ttl_s,
            can_publish=s.fallback_publish,
            can_subscribe=True,
            can_publish_data=True,
        )
        avatar_token = mint_room_token(
            api_key=s.livekit_api_key,
            api_secret=s.livekit_api_secret,
            room=room_name,
            identity=s.avatar_identity,
            name=s.avatar_identity,
            ttl_sec=s.avatar_token_ttl_s,
            can_publish=True,
            can_subscribe=True,
            can_publish_data=True,
            kind="agent",
            attributes={"lk.publish_on_behalf": s.runtime_identity},
        )
        sess: _Sess | None = None
        post_task: asyncio.Future | None = None
        failure: BaseException | None = None
        try:
            await asyncio.wait_for(room.connect(s.livekit_url, runtime_token), s.ready_timeout_s)
            sess = _Sess(
                room,
                AvatarAudioChannel(
                    room,
                    s.avatar_identity,
                    sample_rate=s.audio_sample_rate,
                    clock=self._clock,
                    io_timeout_s=s.io_timeout_s,
                    history=s.history,
                    tombstone_ttl_s=s.tombstone_ttl_s,
                ),
            )
            sess.cleared = _BoundedSet(s.history)
            sess.unconfirmed = _BoundedSet(s.history)
            # Single-speaker: no agent_prompt / LLM / TTS fields are ever sent.
            body = {
                "transport_type": "livekit",
                "agent_id": agent_id,
                "idle_timeout": s.idle_timeout_s,
                "properties": {
                    "livekit_url": s.livekit_url,
                    "livekit_token": avatar_token,
                    "livekit_session_id": room_name,
                },
            }
            post_task = asyncio.ensure_future(
                asyncio.to_thread(
                    self._post,
                    f"{s.api_base.rstrip('/')}/sessions",
                    {"X-API-Key": s.lemonslice_api_key},
                    body,
                    s.request_timeout_s,
                )
            )
            status, data = await asyncio.shield(post_task)
            post_task = None
            if status >= 300:
                raise LemonSliceError(
                    "session_request_failed", f"LemonSlice session request failed status={status}"
                )
            sess.provider_session_id = str(data.get("session_id") or "")
            await self._wait_avatar_video(room)
            if s.fallback_publish:
                # Look BEFORE publishing: an avatar with its own audio plus ours = two speakers.
                if await self._avatar_has_audio(room, s.audio_probe_s):
                    raise AvatarAudioFallbackRefused()
                publish, capture, *extra = self._track_factory(room, s.audio_sample_rate)
                await asyncio.wait_for(publish(), s.io_timeout_s)
                sess.fallback = self._new_fallback(sess, capture, extra)
        except BaseException as exc:
            failure = exc
        if failure is not None:
            if post_task is not None:
                # The caller gave up while the REST call was in flight: a reaper owned by the
                # backend ends the session if that call lands (stop_all awaits it).
                self._spawn_reaper(post_task)
            await self._teardown(room_name, sess, room)
            if isinstance(failure, asyncio.CancelledError | LemonSliceError):
                raise failure
            # raised outside the except block: no __context__, and never the original message
            raise LemonSliceError(
                "start_failed", f"LemonSlice start failed error_type={type(failure).__name__}"
            )
        self._sessions[room_name] = sess
        client_token = mint_room_token(
            api_key=s.livekit_api_key,
            api_secret=s.livekit_api_secret,
            room=room_name,
            identity=f"viewer-{uuid.uuid4().hex[:12]}",
            ttl_sec=s.client_token_ttl_s,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=False,  # a browser holder must never reach lk.audio_stream / RPC
        )
        return StartResult(
            session_id=room_name,
            livekit_url=s.livekit_url,
            livekit_client_token=client_token,
            mode="LEMONSLICE",
        )

    def _new_fallback(self, sess: _Sess, capture: Any, extra: list[Any]) -> _FallbackAudioTrack:
        clear = extra[0] if len(extra) > 0 else None
        unpublish = extra[1] if len(extra) > 1 else None
        return _FallbackAudioTrack(
            capture,
            self._s.render_offset_ms,
            clear,
            self._s.fallback_max_queue,
            veto=lambda: self._avatar_audio_now(sess.room),
            on_veto=lambda: self._tasks_add(asyncio.ensure_future(self._trip_fallback(sess))),
            unpublish=unpublish,
        )

    def _tasks_add(self, task: asyncio.Task) -> None:
        self._reapers.add(task)
        task.add_done_callback(self._reapers.discard)

    async def _trip_fallback(self, sess: _Sess) -> None:
        """Avatar audio detected while our fallback runs: unpublish ours, drain, fail closed."""
        sess.refused = True
        fb = sess.fallback
        if fb is None:
            return
        fb.drain()
        fb.close()
        if fb.unpublish is not None:
            try:
                await asyncio.wait_for(fb.unpublish(), self._s.io_timeout_s)
            except Exception as exc:
                log.warning("fallback unpublish failed error_type=%s", type(exc).__name__)
        log.error("avatar publishes audio: fallback track stopped, session fails closed")

    def _spawn_reaper(self, post_task: asyncio.Future) -> None:
        self._tasks_add(asyncio.ensure_future(self._reap_late_session(post_task)))

    async def _reap_late_session(self, post_task: asyncio.Future) -> None:
        """End a provider session whose creation outlived its caller."""
        try:
            status, data = await asyncio.wait_for(
                asyncio.shield(post_task), self._s.request_timeout_s * 3
            )
        except BaseException as exc:
            log.warning("lemonslice late session unknown error_type=%s", type(exc).__name__)
            return
        sid = str(data.get("session_id") or "") if status < 300 else ""
        if sid:
            await self._terminate_provider(SimpleNamespace(provider_session_id=sid))

    async def _wait_avatar_video(self, room: Any) -> None:
        try:
            from livekit import rtc  # type: ignore

            kind_video = rtc.TrackKind.KIND_VIDEO
        except ImportError:
            kind_video = _KIND_VIDEO
        deadline = time.monotonic() + self._s.ready_timeout_s
        while time.monotonic() < deadline:
            p = room.remote_participants.get(self._s.avatar_identity)
            if p is not None and any(
                getattr(pub, "kind", None) == kind_video for pub in p.track_publications.values()
            ):
                return
            await asyncio.sleep(0.05)
        raise LemonSliceError(
            "avatar_video_timeout",
            "LemonSlice avatar did not publish video before the ready timeout",
        )

    def _avatar_audio_now(self, room: Any) -> bool:
        try:
            from livekit import rtc  # type: ignore

            kind_audio = rtc.TrackKind.KIND_AUDIO
        except ImportError:
            kind_audio = _KIND_AUDIO
        p = room.remote_participants.get(self._s.avatar_identity)
        return p is not None and any(
            getattr(pub, "kind", None) == kind_audio for pub in p.track_publications.values()
        )

    async def _avatar_has_audio(self, room: Any, grace_s: float) -> bool:
        deadline = time.monotonic() + grace_s
        while True:
            if self._avatar_audio_now(room):
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)

    def stream_audio(self, session_id: str, audio_window: AudioWindow):
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        if audio_window.pcm is None:
            raise ValueError("cloud_lemonslice requires in-memory PCM windows")
        self._run(self._stream(sess, audio_window))
        return iter(())

    async def _stream(self, sess: _Sess, w: AudioWindow) -> None:
        if w.utterance_id in sess.cleared:  # late window of an interrupted utterance
            return
        assert w.pcm is not None  # checked in stream_audio
        epoch = sess.epoch
        await self._guard_fallback(sess)  # avatar audio appeared after start: never two speakers
        out = await sess.channel.send(
            w.pcm,
            src_rate=w.sample_rate,
            utterance_id=w.utterance_id,
            epoch=epoch,
            final=w.is_final,
        )
        await self._guard_fallback(sess)  # re-check after the await
        if out is None:  # fenced by an interrupt while suspended: stale audio was dropped
            return
        if sess.fallback is not None and out and epoch == sess.epoch:
            sess.fallback.enqueue(out)
        if w.is_final:
            timeout = sess.channel.sent_ms / 1000 + self._s.playback_margin_s
            ok = await sess.channel.wait_finished(w.utterance_id, timeout)
            if w.utterance_id in sess.cleared or epoch != sess.epoch:
                return
            if not ok:
                sess.unconfirmed.add(w.utterance_id)
                log.warning("avatar playback_unconfirmed utterance=%s", w.utterance_id)

    async def _guard_fallback(self, sess: _Sess) -> None:
        if sess.fallback is None and not sess.refused:
            return
        if not sess.refused and self._avatar_audio_now(sess.room):
            await self._trip_fallback(sess)
        if sess.refused:
            raise AvatarAudioFallbackRefused()

    def interrupt(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        self._run(self._interrupt(sess))

    async def _interrupt(self, sess: _Sess) -> None:
        open_id = sess.channel.open_utterance
        if open_id:
            sess.cleared.add(open_id)
        if sess.fallback is not None:
            sess.fallback.drain()
        await sess.channel.clear_buffer()  # bumps the epoch fence first

    def stop(self, session_id: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if sess is None:  # idempotent: a repeated stop is a no-op
            return
        self._run(self._teardown(session_id, sess, sess.room))

    def stop_all(self) -> None:
        if self._loop is not None:
            self._run(self._drain(startups=True))  # cancelled startups clean up inside their task
        for sid in list(self._sessions):
            try:
                self.stop(sid)
            except Exception as exc:
                log.warning("lemonslice stop_all error_type=%s", type(exc).__name__)
        if self._loop is not None:
            self._run(self._drain(startups=False))  # late REST sessions are ended before exit
        self._shutdown_loop()

    async def _teardown(self, room_name: str, sess: _Sess | None, room: Any) -> None:
        if sess is not None:
            try:
                await self._interrupt(sess)
            except Exception:
                pass
            if sess.fallback is not None:
                sess.fallback.close()
            await self._terminate_provider(sess)
        try:
            await asyncio.wait_for(room.disconnect(), self._s.io_timeout_s)
        except Exception as exc:
            log.warning("lemonslice room leave failed error_type=%s", type(exc).__name__)

    async def _terminate_provider(self, sess: _Sess) -> None:
        s = self._s
        if not sess.provider_session_id:
            return
        if not s.terminate_path:
            log.warning(
                "lemonslice session not terminated (no terminate path); idle_timeout applies"
            )
            return
        path = s.terminate_path.format(session_id=sess.provider_session_id)
        for attempt in range(max(1, s.terminate_attempts)):
            try:
                status, _ = await asyncio.to_thread(
                    self._post,
                    f"{s.api_base.rstrip('/')}/{path.lstrip('/')}",
                    {"X-API-Key": s.lemonslice_api_key},
                    {},
                    s.request_timeout_s,
                )
                if status < 300:
                    return
                err = f"status={status}"
            except Exception as exc:
                err = f"error_type={type(exc).__name__}"
            log.warning("lemonslice terminate failed attempt=%d %s", attempt + 1, err)
            await asyncio.sleep(0.2)
        log.error("lemonslice session NOT terminated after retries; idle_timeout applies")

    def session_status(self, session_id: str) -> str:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        p = sess.room.remote_participants.get(self._s.avatar_identity)
        return "active" if p is not None else "avatar_absent"

    def playback_info(self, session_id: str, utterance_id: str) -> dict[str, Any] | None:
        """D4 helper: wall-clock playback_started_at for one utterance."""
        sess = self._sessions.get(session_id)
        if sess is None:
            return None
        return {
            "playback_started_at": sess.channel.playback_started_at.get(utterance_id),
            "playback_unconfirmed": utterance_id in sess.unconfirmed,
        }
