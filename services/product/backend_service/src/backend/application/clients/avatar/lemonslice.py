"""``cloud_lemonslice`` render backend (P0-FB-010 / 010c, DR-MEDIA-001 Option B2).

The LemonSlice avatar joins a Livento-owned LiveKit room (room name = runtime
session id). The Runtime stays the single speaker: guarded TTS PCM is
resampled and sent over the LiveKit avatar data-stream protocol to the avatar
participant only. LemonSlice's own LLM/TTS is never enabled.

Session creation goes through LemonSlice's LiveKit Agents plugin API client
(``livekit.plugins.lemonslice.api.LemonSliceAPI.start_agent_session``), imported as a
library only: no ``AgentSession``, no Agents worker, no LemonSlice LLM/TTS (support
answer, Report 87: the raw ``POST /liveai/sessions`` is plugin-only). The plugin client has
NO control methods, so terminate and keep-alive stay on the documented, separate
control-session endpoint (``POST .../sessions/{id}/control``).

The seam is sync; ``livekit-rtc`` runs on one backend-owned event-loop thread.
``stream_audio`` yields no VideoWindow (the provider renders in the room).

UNVERIFIED ASSUMPTIONS (gate external proof 023, not local implementation).
Items tagged DOC-via-assistant come from LemonSlice's own docs assistant: documented but
NOT verified by a human or against the real service. Behaviour stays configurable and
fails safe.
  LS1  Session start = plugin ``LemonSliceAPI(api_url={api_base}/sessions).start_agent_session``
       (``livekit_url``, agent token, room sid, ``agent_id``, ``idle_timeout``; response
       ``session_id``). UNVERIFIED: that library-only use without ``AgentSession`` is accepted
       by LemonSlice (Report 87 action 2). The plugin's own retries are disabled (a retried
       POST could start a second billable session).
  LS1t Terminate (not in the plugin client). Documented control endpoint: ``POST {api_base}/sessions/{id}/control`` with
       ``{"event":"terminate"}``; default of ``LEMONSLICE_TERMINATE_PATH`` (``{session_id}``
       placeholder, relative to the API base).
  LS2  the avatar publishes video (and audio); ``lk.audio_stream`` accepts 16 kHz
       (DOC-via-assistant: 16 kHz is the processing rate, other rates are resampled).
       If it publishes video only, ``AVATAR_AUDIO_FALLBACK_PUBLISH`` makes the
       Runtime publish the same PCM as one delayed audio track.
  LS3  Idle timeout. DOC-via-assistant: default 60 s, resets while the avatar talks;
       ``{"event":"reset-idle-timeout"}`` on the control endpoint resets it. A keep-alive
       loop (``LEMONSLICE_KEEPALIVE_S``, 0 = off) sends it while the avatar is silent.
  LS4  Session cap. DOC-via-assistant: hard cap is the LemonSlice GPU timeout (default 30 min).
       ``LEMONSLICE_MAX_SESSION_S`` (default 1500, 0 = off) refuses new utterances with
       ``SessionNearCap`` and ``session_status()`` reports ``near_cap``. There is NO automatic
       rollover: whether a new session may start right after the cap is unknown (LS4/LS7).
  LK   JWT claim names for ``kind=agent`` / ``lk.publish_on_behalf``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import logging
import math
import re
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote

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


_CONTROL_PATH = "sessions/{session_id}/control"  # DOC-via-assistant, relative to api_base
_CAP_WARN_FRACTION = 0.8
# A provider session id becomes ONE URL path segment carrying the API key header.
_SAFE_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TERMINAL_STATUS = frozenset({404, 410})  # the provider session is gone
_AUTH_STATUS = frozenset({401, 403})  # terminal only when repeated


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
    # TOTAL deadline of one control call (keep-alive / terminate); None = request_timeout_s.
    # httpx timeouts are per connect/read chunk, not total, so this is enforced by cancellation.
    control_deadline_s: float | None = None
    avatar_identity: str = "lemonslice-avatar-agent"
    runtime_identity: str = "livento-runtime"
    fallback_publish: bool = False
    render_offset_ms: int = 0
    terminate_path: str = _CONTROL_PATH
    keepalive_s: float = 20.0  # reset-idle-timeout period while the avatar is silent; 0 = off
    max_session_s: float = 1500.0  # age guard below the documented 30 min cap; 0 = off
    keepalive_fail_log_after: int = 3
    playback_margin_s: float = 5.0
    avatar_token_ttl_s: int = 300
    client_token_ttl_s: int = 3600
    io_timeout_s: float = 2.0  # stream open/write/close and room disconnect deadline
    clear_budget_s: float = 5.0  # TOTAL worst case of one interrupt (lock, abort, RPC, ack)
    audio_recheck_s: float = 1.0  # idle backstop for 'avatar started publishing audio'
    audio_probe_s: float = 1.0  # how long to look for an avatar audio track before fallback
    fallback_max_queue: int = 512  # bounded fallback buffer (chunks)
    history: int = 256  # bounded playback-history collections
    terminate_attempts: int = 2

    def __post_init__(self) -> None:
        # Negative/NaN/inf timers would silently disable a safeguard: refuse them (0 = off where
        # documented: keepalive_s, max_session_s). Messages name the field, never the value.
        for name in (
            "ready_timeout_s",
            "request_timeout_s",
            "keepalive_s",
            "max_session_s",
            "playback_margin_s",
            "io_timeout_s",
            "clear_budget_s",
            "audio_recheck_s",
            "audio_probe_s",
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ValueError(f"{name} must be a finite, non-negative number")
            if not math.isfinite(v) or v < 0:
                raise ValueError(f"{name} must be a finite, non-negative number")
        cd = self.control_deadline_s
        if cd is not None and (
            isinstance(cd, bool)
            or not isinstance(cd, int | float)
            or not math.isfinite(cd)
            or cd <= 0
        ):
            raise ValueError("control_deadline_s must be a finite, positive number")
        # counts and sizes: real integers only (no float, NaN, bool), with a lower bound
        for name, low in (
            ("idle_timeout_s", 0),
            ("avatar_token_ttl_s", 1),
            ("client_token_ttl_s", 1),
            ("render_offset_ms", 0),
            ("terminate_attempts", 1),
            ("keepalive_fail_log_after", 1),
            ("audio_sample_rate", 1),
            ("history", 1),
            ("fallback_max_queue", 1),
        ):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, int) or v < low:
                raise ValueError(f"{name} must be an integer >= {low}")

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


class SessionNearCap(LemonSliceError):
    """The session is older than ``max_session_s``: the provider's hard cap is close."""

    def __init__(self) -> None:
        super().__init__(
            "session_near_cap", "LemonSlice session reached its age guard; start a new session"
        )


class _SessionClosing(Exception):
    """A control call was refused because its session is closing or closed."""


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


AsyncHttpPost = Callable[
    [str, dict[str, str], dict[str, Any], float], Awaitable[tuple[int, dict[str, Any]]]
]


SessionClientFactory = Callable[[], Any]  # -> async context manager with start_agent_session


def _plugin_client_factory(s: LemonSliceSettings) -> SessionClientFactory:
    """Real client: the LemonSlice LiveKit Agents plugin API client, used as a library only."""

    def factory() -> Any:
        found: tuple[Any, Any] | None = None
        try:
            from livekit.agents import APIConnectOptions  # type: ignore
            from livekit.plugins.lemonslice.api import LemonSliceAPI  # type: ignore

            found = (APIConnectOptions, LemonSliceAPI)
        except ImportError:
            pass
        if found is None:  # raised outside the except block: no __context__
            raise LemonSliceError(
                "plugin_not_installed",
                "livekit-plugins-lemonslice is not installed (install the 'lemonslice' extra)",
            )
        connect_options, api_cls = found
        return api_cls(
            api_key=s.lemonslice_api_key,
            api_url=f"{s.api_base.rstrip('/')}/sessions",
            # max_retry=0: a retried POST could create a second billable provider session
            conn_options=connect_options(max_retry=0, timeout=s.request_timeout_s),
        )

    return factory


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
    watcher: Any = None  # idle backstop task for 'avatar started publishing audio'
    keepalive: Any = None  # reset-idle-timeout loop task
    started_at: float = 0.0  # monotonic
    cap_warned: bool = False
    closing: bool = False  # read from the HTTP worker thread: set before any teardown step
    # Serializes control HTTP calls of one session: terminate waits (bounded) for a running
    # keep-alive, and a late keep-alive worker re-checks ``closing`` while holding it.
    ctl_lock: threading.Lock = field(default_factory=threading.Lock)
    degraded: str = ""  # terminal keep-alive outcome (status class); "" = healthy

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
        session_client_factory: SessionClientFactory | None = None,
        http_post: HttpPost | None = None,
        async_http_post: AsyncHttpPost | None = None,
        audio_track_factory: Callable[[Any, int], Any] | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mono = monotonic
        self._s = settings
        self._room_factory = room_factory or self._default_room
        self._session_client = session_client_factory or _plugin_client_factory(settings)
        self._post = http_post or _httpx_post  # control endpoint only (terminate, keep-alive)
        # Control calls run cancellably on the loop (real httpx.AsyncClient) unless a sync fake
        # http_post is injected (then they run in a worker thread, guarded by the control lock).
        self._async_post = async_http_post or (
            self._httpx_async_post if http_post is None else None
        )
        self._client: Any = None
        self._track_factory = audio_track_factory or self._default_track
        self._clock = clock
        self._sessions: dict[str, _Sess] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._startups: set[asyncio.Task] = set()  # start() in flight: cancelled by stop_all
        self._cleanups: set[asyncio.Task] = set()  # stop()/teardown in flight: NEVER cancelled
        self._closing: dict[str, _Sess] = {}  # popped for teardown, not yet fully cleaned up
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

    async def _tracked(self, coro: Any, pool: set[asyncio.Task] | None) -> Any:
        """Loop-thread wrapper so stop_all() can find and await every in-flight operation."""
        task = asyncio.current_task()
        assert task is not None
        if pool is not None:
            pool.add(task)
        try:
            return await coro
        finally:
            if pool is not None:
                pool.discard(task)

    def _run(self, coro: Any, timeout: float | None = None, pool: str | None = None) -> Any:
        target = {"startup": self._startups, "cleanup": self._cleanups}.get(pool or "")
        fut = asyncio.run_coroutine_threadsafe(self._tracked(coro, target), self._ensure_loop())
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError:
            pass
        fut.cancel()  # never leave the coroutine running after the caller gave up
        raise LemonSliceError("timeout", "LemonSlice backend call timed out")

    async def _drain(self, pool: str) -> None:
        """startups: cancel + await (their cleanup runs inside). cleanups/reapers: await only."""
        me = asyncio.current_task()
        tasks = {"startups": self._startups, "cleanups": self._cleanups, "reapers": self._reapers}[
            pool
        ]
        pending = [t for t in tasks if t is not me and not t.done()]
        if pool == "startups":
            for t in pending:
                t.cancel()
        if pending:
            await asyncio.wait(pending, timeout=self._s.request_timeout_s * 3 + 5)
        for t in pending:
            if not t.done():
                log.error("lemonslice %s task did not finish within its bound", pool)
                t.cancel()

    async def _httpx_async_post(
        self, url: str, headers: dict, body: dict, timeout: float
    ) -> tuple[int, dict]:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=timeout)
        r = await self._client.post(url, headers=headers, json=body, timeout=timeout)
        try:
            data = r.json()
        except ValueError:
            data = {}
        return r.status_code, data if isinstance(data, dict) else {}

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.aclose(), self._s.io_timeout_s)
            except Exception as exc:
                log.warning("lemonslice http client close error_type=%s", type(exc).__name__)

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
            self._start(agent_id),
            self._s.ready_timeout_s + self._s.request_timeout_s + 10,
            pool="startup",
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
                    clear_budget_s=s.clear_budget_s,
                ),
            )
            sess.started_at = self._mono()
            sess.cleared = _BoundedSet(s.history)
            sess.unconfirmed = _BoundedSet(s.history)
            # Single-speaker: no agent_prompt / LLM / TTS fields are ever sent.
            post_task = asyncio.ensure_future(
                self._request_session(agent_id, avatar_token, await self._room_sid(room, room_name))
            )
            sid = await asyncio.shield(post_task)
            post_task = None
            if not isinstance(sid, str) or not _SAFE_SESSION_ID.fullmatch(sid):
                # never interpolate a foreign value into a URL carrying the API key
                raise LemonSliceError(
                    "session_id_invalid", "LemonSlice returned an unusable session id"
                )
            sess.provider_session_id = sid
            if s.keepalive_s > 0 and sess.provider_session_id:
                sess.keepalive = asyncio.ensure_future(self._keepalive(sess))
            await self._wait_avatar_video(room)
            if s.fallback_publish:
                # Look BEFORE publishing: an avatar with its own audio plus ours = two speakers.
                if await self._avatar_has_audio(room, s.audio_probe_s):
                    raise AvatarAudioFallbackRefused()
                publish, capture, *extra = self._track_factory(room, s.audio_sample_rate)
                await asyncio.wait_for(publish(), s.io_timeout_s)
                sess.fallback = self._new_fallback(sess, capture, extra)
                self._watch_avatar_audio(sess)
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

    @staticmethod
    async def _room_sid(room: Any, room_name: str) -> str:
        sid = getattr(room, "sid", None)  # rtc.Room.sid is awaitable
        if inspect.isawaitable(sid):
            sid = await sid
        return sid if isinstance(sid, str) and sid else room_name

    async def _request_session(self, agent_id: str, avatar_token: str, room_sid: str) -> Any:
        """Start the provider session through the plugin client; errors carry no body/URL/key."""
        s = self._s
        failure: Exception | None = None
        try:
            async with self._session_client() as client:
                return await asyncio.wait_for(
                    client.start_agent_session(
                        livekit_url=s.livekit_url,
                        livekit_token=avatar_token,
                        livekit_session_id=room_sid,
                        agent_id=agent_id,
                        idle_timeout=s.idle_timeout_s,
                    ),
                    s.request_timeout_s,
                )
        except (asyncio.CancelledError, LemonSliceError):
            raise
        except Exception as exc:
            failure = exc
        # raised outside the except block: no __context__/__cause__, never the original message
        status = getattr(failure, "status_code", None)
        detail = (
            f"status={status}"
            if isinstance(status, int)
            else f"error_type={type(failure).__name__}"
        )
        raise LemonSliceError(
            "session_request_failed", f"LemonSlice session request failed {detail}"
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

    def _watch_avatar_audio(self, sess: _Sess) -> None:
        """Detect avatar audio while idle: room publication events plus a periodic backstop."""

        def on_event(*_: Any) -> None:
            if not sess.refused and self._avatar_audio_now(sess.room):
                sess.refused = True  # synchronously: no push/send may slip in before the trip task
                self._tasks_add(asyncio.ensure_future(self._trip_fallback(sess)))

        for event in ("track_published", "track_subscribed"):
            on = getattr(sess.room, "on", None)
            if callable(on):
                try:
                    on(event, on_event)
                except Exception as exc:
                    log.warning("room event subscribe failed error_type=%s", type(exc).__name__)

        async def poll() -> None:
            while not sess.refused:
                await asyncio.sleep(self._s.audio_recheck_s)
                on_event()

        sess.watcher = asyncio.ensure_future(poll())

    def _tasks_add(self, task: asyncio.Task) -> None:
        self._reapers.add(task)
        task.add_done_callback(self._reapers.discard)

    async def _trip_fallback(self, sess: _Sess) -> None:
        """Avatar audio detected while our fallback runs: unpublish ours, drain, fail closed."""
        sess.refused = True
        if sess.watcher is not None:
            sess.watcher.cancel()
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
            sid = await asyncio.wait_for(asyncio.shield(post_task), self._s.request_timeout_s * 3)
        except BaseException as exc:
            log.warning("lemonslice late session unknown error_type=%s", type(exc).__name__)
            return
        if isinstance(sid, str) and _SAFE_SESSION_ID.fullmatch(sid):
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
        if self._age_state(sess) == "near_cap" and sess.channel.open_utterance != w.utterance_id:
            raise SessionNearCap()  # a started utterance may still finish
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
        self._closing[session_id] = sess  # tracked until disconnect + terminate finished
        self._run(self._close_session(session_id, sess), pool="cleanup")

    async def _close_session(self, session_id: str, sess: _Sess) -> None:
        try:
            await self._teardown(session_id, sess, sess.room)
        finally:
            self._closing.pop(session_id, None)

    def stop_all(self) -> None:
        try:
            if self._loop is not None:
                self._run(self._drain("startups"))  # cancelled startups clean up inside their task
            for sid in list(self._sessions):
                try:
                    self.stop(sid)
                except Exception as exc:
                    log.warning("lemonslice stop_all error_type=%s", type(exc).__name__)
            if self._loop is not None:
                self._run(self._drain("cleanups"))  # a stop parked in a clear is awaited
                self._run(self._drain("reapers"))  # late REST sessions are ended before exit
            if self._closing:
                log.error(
                    "lemonslice sessions left without confirmed cleanup count=%d",
                    len(self._closing),
                )
        finally:
            try:
                if self._loop is not None:
                    self._run(self._close_client())
            except Exception as exc:
                log.warning("lemonslice client close error_type=%s", type(exc).__name__)
            finally:
                self._shutdown_loop()

    async def _teardown(self, room_name: str, sess: _Sess | None, room: Any) -> None:
        if sess is not None:
            sess.closing = True  # first: no keep-alive may start from here on
            if sess.keepalive is not None:
                sess.keepalive.cancel()
                # cancellable: an async keep-alive aborts its connection; await it before terminate
                await asyncio.wait({sess.keepalive}, timeout=self._deadline() + 1)
            try:
                await self._interrupt(sess)
            except Exception:
                pass
            if sess.watcher is not None:
                sess.watcher.cancel()
            if sess.fallback is not None:
                sess.fallback.close()
            try:
                await sess.channel.shutdown()
            except Exception as exc:
                log.warning("channel shutdown error_type=%s", type(exc).__name__)
            try:
                await self._terminate_provider(sess)
            except Exception as exc:  # the room must still be left
                log.error("lemonslice terminate error_type=%s", type(exc).__name__)
        try:
            await asyncio.wait_for(room.disconnect(), self._s.io_timeout_s)
        except Exception as exc:
            log.warning("lemonslice room leave failed error_type=%s", type(exc).__name__)

    async def _control(
        self, provider_session_id: str, event: str, path: str, guard: _Sess | None = None
    ) -> int:
        """POST one control event; returns the HTTP status. May raise (caller classes it).

        With ``guard`` the call is serialized with the session's other control calls. A
        keep-alive re-checks ``guard.closing`` while holding the lock, right before the HTTP call;
        terminate waits for the lock a bounded time and then proceeds regardless.
        """
        s = self._s
        rel = path.format(session_id=quote(provider_session_id, safe="")).lstrip("/")
        url = f"{s.api_base.rstrip('/')}/{rel}"
        if self._async_post is not None:
            if guard is not None and guard.closing and event != "terminate":
                raise _SessionClosing()
            status, _ = await asyncio.wait_for(
                self._async_post(
                    url, {"X-API-Key": s.lemonslice_api_key}, {"event": event}, s.request_timeout_s
                ),
                self._deadline(),  # TOTAL deadline; cancellation aborts the connection
            )
            return status

        def send() -> tuple[int, dict]:
            return self._post(
                url, {"X-API-Key": s.lemonslice_api_key}, {"event": event}, s.request_timeout_s
            )

        def call() -> tuple[int, dict]:
            if guard is None:
                return send()
            if event == "terminate":
                got = guard.ctl_lock.acquire(timeout=s.request_timeout_s + 1)
                try:
                    if not got:
                        log.error("lemonslice control call still in flight at terminate")
                    return send()
                finally:
                    if got:
                        guard.ctl_lock.release()
            with guard.ctl_lock:
                if guard.closing:  # checked after any park, immediately before the send
                    raise _SessionClosing()
                return send()

        fut = asyncio.ensure_future(asyncio.to_thread(call))
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # never "not retrieved"
        status, _ = await asyncio.shield(fut)
        return status

    def _deadline(self) -> float:
        d = self._s.control_deadline_s
        return self._s.request_timeout_s if d is None else d

    async def _terminate_provider(self, sess: _Sess) -> None:
        s = self._s
        if not sess.provider_session_id:
            return
        if not s.terminate_path:
            log.warning(
                "lemonslice session not terminated (no terminate path); idle_timeout applies"
            )
            return
        attempts = s.terminate_attempts if isinstance(s.terminate_attempts, int) else 2
        guard = sess if isinstance(sess, _Sess) else None
        for attempt in range(max(1, attempts)):
            try:
                status = await self._control(
                    sess.provider_session_id, "terminate", s.terminate_path, guard
                )
                if status < 300:
                    return
                err = f"status={status}"
            except Exception as exc:
                err = f"error_type={type(exc).__name__}"
            log.warning("lemonslice terminate failed attempt=%d %s", attempt + 1, err)
            await asyncio.sleep(0.2)
        log.error("lemonslice session NOT terminated after retries; idle_timeout applies")

    async def _keepalive(self, sess: _Sess) -> None:
        """Send reset-idle-timeout while the avatar is silent. Never touches the speech path:
        it is its own task, each call is bounded, failures are only counted and logged."""
        s = self._s
        failures = 0
        auth_failures = 0
        while not sess.channel.broken and not sess.closing:
            await asyncio.sleep(s.keepalive_s)
            self._age_state(sess)  # also emits the one-time 80% warning
            if sess.closing:
                return
            if sess.channel.broken or sess.channel.open_utterance:
                continue  # speaking already resets the provider idle timer
            err = ""
            status = 0
            try:
                status = await asyncio.wait_for(
                    self._control(
                        sess.provider_session_id, "reset-idle-timeout", _CONTROL_PATH, sess
                    ),
                    s.request_timeout_s + 1,
                )
                if status >= 300:
                    err = f"status={status}"
            except _SessionClosing:
                return
            except Exception as exc:
                err = f"error_type={type(exc).__name__}"
            if not err:
                failures = auth_failures = 0
                continue
            auth_failures = auth_failures + 1 if status in _AUTH_STATUS else 0
            if status in _TERMINAL_STATUS or auth_failures >= max(1, s.keepalive_fail_log_after):
                sess.degraded = f"status={status}"
                log.error("lemonslice keepalive terminal %s; session degraded", sess.degraded)
                return
            failures += 1
            log.warning("lemonslice keepalive failed consecutive=%d %s", failures, err)
            if failures == s.keepalive_fail_log_after:
                log.error("lemonslice keepalive failing; idle_timeout may end the session")

    def _age_state(self, sess: _Sess) -> str:
        """'ok' | 'near_cap'; logs once at 80% of ``max_session_s``.

        TODO(livento): automatic rollover on near_cap is deliberately not implemented; whether
        LemonSlice allows a new session right after the cap is unknown (LS4/LS7).
        """
        cap = self._s.max_session_s
        if cap <= 0:
            return "ok"
        age = self._mono() - sess.started_at
        if age >= cap:
            return "near_cap"
        if age >= cap * _CAP_WARN_FRACTION and not sess.cap_warned:
            sess.cap_warned = True
            log.warning("lemonslice session age %.0fs is 80%% of the %.0fs guard", age, cap)
        return "ok"

    def session_status(self, session_id: str) -> str:
        sess = self._sessions.get(session_id)
        if sess is None:
            raise KeyError(session_id)
        if sess.channel.broken:
            return "channel_broken"
        if sess.degraded:
            return "degraded"
        if self._age_state(sess) == "near_cap":
            return "near_cap"
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
