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
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
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

    def __repr__(self) -> str:  # never print secrets
        return "LemonSliceSettings(<redacted>)"


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
    """Runtime-owned audio track for the video-only avatar case (delayed)."""

    def __init__(self, capture: Callable[[bytes], Any], offset_ms: int) -> None:
        self._capture = capture
        self._offset = offset_ms / 1000
        self._q: asyncio.Queue = asyncio.Queue()
        self._task = asyncio.ensure_future(self._run())

    def enqueue(self, pcm: bytes) -> None:
        self._q.put_nowait((time.monotonic() + self._offset, pcm))

    def drain(self) -> None:
        while not self._q.empty():
            self._q.get_nowait()

    async def _run(self) -> None:
        while True:
            due, pcm = await self._q.get()
            await asyncio.sleep(max(0.0, due - time.monotonic()))
            res = self._capture(pcm)
            if asyncio.iscoroutine(res):
                await res

    def close(self) -> None:
        self._task.cancel()


@dataclass
class _Sess:
    room: Any
    channel: AvatarAudioChannel
    provider_session_id: str = ""
    epoch: int = 0
    cleared: set[str] = field(default_factory=set)
    unconfirmed: set[str] = field(default_factory=set)
    fallback: _FallbackAudioTrack | None = None


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
        self._loop_lock = threading.Lock()

    # ---- loop thread ------------------------------------------------
    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._loop_lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                threading.Thread(
                    target=loop.run_forever, name="lemonslice-loop", daemon=True
                ).start()
                self._loop = loop
            return self._loop

    def _run(self, coro: Any, timeout: float | None = None) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self._ensure_loop()).result(timeout)

    @staticmethod
    def _default_room() -> Any:
        from livekit import rtc  # type: ignore

        return rtc.Room()

    @staticmethod
    def _default_track(room: Any, sample_rate: int) -> Any:
        from livekit import rtc  # type: ignore

        source = rtc.AudioSource(sample_rate, 1)
        track = rtc.LocalAudioTrack.create_audio_track("livento-audio", source)

        async def publish() -> None:
            await room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            )

        async def capture(pcm: bytes) -> None:
            n = len(pcm) // 2
            await source.capture_frame(rtc.AudioFrame(pcm, sample_rate, 1, n))

        return publish, capture

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
                raise RuntimeError("avatar_id is not in the LEMONSLICE avatar allowlist")
            return avatar_id
        if not self._s.agent_id:
            raise RuntimeError("LEMONSLICE_AGENT_ID is not configured")
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
        try:
            await room.connect(s.livekit_url, runtime_token)
            sess = _Sess(
                room,
                AvatarAudioChannel(
                    room, s.avatar_identity, sample_rate=s.audio_sample_rate, clock=self._clock
                ),
            )
            if s.fallback_publish:
                publish, capture = self._track_factory(room, s.audio_sample_rate)
                await publish()
                sess.fallback = _FallbackAudioTrack(capture, s.render_offset_ms)
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
            status, data = await asyncio.to_thread(
                self._post,
                f"{s.api_base.rstrip('/')}/sessions",
                {"X-API-Key": s.lemonslice_api_key},
                body,
                s.request_timeout_s,
            )
            if status >= 300:
                raise RuntimeError(f"LemonSlice session request failed status={status}")
            sess.provider_session_id = str(data.get("session_id") or "")
            await self._wait_avatar_video(room)
        except BaseException as exc:
            await self._teardown(room_name, sess, room)
            if isinstance(exc, RuntimeError):
                raise
            # never chain the original: it may carry request URLs/headers
            raise RuntimeError(f"LemonSlice start failed error_type={type(exc).__name__}") from None
        self._sessions[room_name] = sess
        client_token = mint_room_token(
            api_key=s.livekit_api_key,
            api_secret=s.livekit_api_secret,
            room=room_name,
            identity=f"viewer-{uuid.uuid4().hex[:12]}",
            ttl_sec=s.client_token_ttl_s,
            can_publish=False,
            can_subscribe=True,
        )
        return StartResult(
            session_id=room_name,
            livekit_url=s.livekit_url,
            livekit_client_token=client_token,
            mode="LEMONSLICE",
        )

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
        raise RuntimeError("LemonSlice avatar did not publish video before the ready timeout")

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
        out = await sess.channel.send(
            w.pcm,
            src_rate=w.sample_rate,
            utterance_id=w.utterance_id,
            epoch=sess.epoch,
            final=w.is_final,
        )
        if sess.fallback is not None and out:
            sess.fallback.enqueue(out)
        if w.is_final:
            timeout = sess.channel.sent_ms / 1000 + self._s.playback_margin_s
            ok = await sess.channel.wait_finished(w.utterance_id, timeout)
            if w.utterance_id in sess.cleared:
                return
            if not ok:
                sess.unconfirmed.add(w.utterance_id)
                log.warning("avatar playback_unconfirmed utterance=%s", w.utterance_id)

    def interrupt(self, session_id: str) -> None:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        self._run(self._interrupt(sess))

    async def _interrupt(self, sess: _Sess) -> None:
        open_id = sess.channel.open_utterance
        if open_id:
            sess.cleared.add(open_id)
        sess.epoch += 1
        if sess.fallback is not None:
            sess.fallback.drain()
        await sess.channel.clear_buffer()

    def stop(self, session_id: str) -> None:
        sess = self._sessions.pop(session_id, None)
        if sess is None:
            raise KeyError(session_id)
        self._run(self._teardown(session_id, sess, sess.room))

    def stop_all(self) -> None:
        for sid in list(self._sessions):
            try:
                self.stop(sid)
            except Exception as exc:
                log.warning("lemonslice stop_all error_type=%s", type(exc).__name__)

    async def _teardown(self, room_name: str, sess: _Sess | None, room: Any) -> None:
        if sess is not None:
            try:
                await self._interrupt(sess)
            except Exception:
                pass
            if sess.fallback is not None:
                sess.fallback.close()
            if self._s.terminate_path and sess.provider_session_id:
                path = self._s.terminate_path.format(session_id=sess.provider_session_id)
                try:
                    await asyncio.to_thread(
                        self._post,
                        f"{self._s.api_base.rstrip('/')}/{path.lstrip('/')}",
                        {"X-API-Key": self._s.lemonslice_api_key},
                        {},
                        self._s.request_timeout_s,
                    )
                except Exception as exc:
                    log.warning("lemonslice terminate failed error_type=%s", type(exc).__name__)
        try:
            await room.disconnect()
        except Exception as exc:
            log.warning("lemonslice room leave failed error_type=%s", type(exc).__name__)

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
