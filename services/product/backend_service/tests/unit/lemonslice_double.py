"""LemonSlice protocol double for P0-FB-010 (test double, never a real provider).

A fake ``rtc.Room`` plus a fake avatar participant. It records every data-stream
write, RPC and track event with timestamps, and implements the avatar side of
``lk.playback_started`` / ``lk.playback_finished`` / ``lk.clear_buffer``.
``FakeLemonSlice`` is the plugin-client double (session start) and also serves the control endpoint (``controls``: terminate and
reset-idle-timeout events; ``control_status`` / ``control_raise`` inject failures).
Modes: ``normal`` | ``video_only`` (the avatar publishes no audio) | ``never``
(the avatar never joins) | ``silent`` (never sends playback_finished).

Deterministic races: ``room.hold(name)`` blocks the next ``open`` / ``write`` /
``close`` / ``rpc`` / ``disconnect`` operation at its gate until ``room.release(name)``;
``room.wait_hit(name)`` returns once an operation is parked there. ``room.auto = False``
stops the avatar from answering by itself so a test injects playback events with
``room.emit``. ``FakeLemonSlice.hold()`` parks the REST call the same way.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import jwt

AVATAR = "lemonslice-avatar-agent"
KIND_VIDEO, KIND_AUDIO = 2, 1


@dataclass
class Event:
    kind: str
    at: float
    detail: Any = None


@dataclass
class Stream:
    attributes: dict
    destination: list
    chunks: list = field(default_factory=list)
    closed_reason: str | None = "open"
    opened_at: float = 0.0

    @property
    def pcm(self) -> bytes:
        return b"".join(c for _, c in self.chunks)


class _Writer:
    def __init__(self, stream: Stream, room: "FakeRoom") -> None:
        self.s, self.room = stream, room

    async def write(self, data: bytes) -> None:
        await self.room.gate("write")
        if self.s.closed_reason != "open":  # like the real SDK: a closed stream rejects writes
            raise RuntimeError("write on a closed stream")
        if not self.s.chunks:
            self.room.log("first_write", self.s.attributes["livento.utterance_id"])
            self.room.avatar_started()
        self.s.chunks.append((time.monotonic(), bytes(data)))

    async def aclose(self, *, reason: str | None = None) -> None:
        await self.room.gate("close")
        self.s.closed_reason = reason
        self.room.log("stream_closed", reason)
        if reason is None:
            self.room.avatar_plays(self.s)


class _Local:
    def __init__(self, room: "FakeRoom") -> None:
        self.room, self.rpc = room, {}

    def register_rpc_method(self, name, handler):
        self.rpc[name] = handler

    async def stream_bytes(
        self, name, *, topic="", attributes=None, destination_identities=None, **kw
    ):
        assert topic == "lk.audio_stream"
        await self.room.gate("open")
        s = Stream(
            dict(attributes or {}), list(destination_identities or []), opened_at=time.monotonic()
        )
        self.room.streams.append(s)
        return _Writer(s, self.room)

    async def perform_rpc(self, *, destination_identity, method, payload, response_timeout=None):
        await self.room.gate("rpc")
        self.room.log("rpc", (destination_identity, method))
        if method == "lk.clear_buffer" and self.room.mode != "silent":
            self.room.log("clear_buffer_done")
            self.room.fire("lk.playback_finished", '{"interrupted": true}')
        return ""

    async def publish_track(self, *a, **k):
        self.room.audio_track_published = True


class FakeRoom:
    def __init__(self, mode: str = "normal", play_delay: float = 0.05) -> None:
        self.mode, self.play_delay = mode, play_delay
        self.events: list[Event] = []
        self.streams: list[Stream] = []
        self.remote_participants: dict = {}
        self.local_participant = _Local(self)
        self.connected_token: dict | None = None
        self.disconnected = False
        self.audio_track_published = False
        self.captured: list[tuple[float, bytes]] = []
        self.auto = True
        self.emits_events = True
        self.handlers: dict[str, list] = {}
        self.blocked: set[str] = set()
        self.resistant: set[str] = set()
        self.fail: dict[str, BaseException] = {}  # raise this at the named gate, once
        self.hits: dict[str, threading.Event] = {}
        self.queue_cleared = 0
        self.room_sid = "RM_fake"

    # -- deterministic gates (thread-safe: the test thread controls the loop thread)
    def hold(self, name: str, *, resistant: bool = False) -> None:
        """Park the next ``name`` operation; ``resistant`` swallows cancellation (a hung socket)."""
        self.blocked.add(name)
        if resistant:
            self.resistant.add(name)

    def release(self, name: str) -> None:
        self.blocked.discard(name)

    def wait_hit(self, name: str, timeout: float = 3.0) -> bool:
        return self.hits.setdefault(name, threading.Event()).wait(timeout)

    async def gate(self, name: str) -> None:
        self.hits.setdefault(name, threading.Event()).set()
        while name in self.blocked:
            if name in self.resistant:
                try:
                    await asyncio.shield(asyncio.sleep(0.005))
                except asyncio.CancelledError:
                    pass  # a hung operation that ignores cancellation
            else:
                await asyncio.sleep(0.005)
        exc = self.fail.get(name)
        if isinstance(exc, list):  # several consecutive failures
            exc = exc.pop(0) if exc else None
        elif exc is not None:
            del self.fail[name]
        if exc is not None:
            raise exc

    def emit(self, loop, method: str, payload: str = "", caller: str = AVATAR) -> None:
        """Deliver one avatar playback RPC on the backend loop and wait for the handler."""
        data = SimpleNamespace(caller_identity=caller, payload=payload)
        handler = self.local_participant.rpc[method]
        asyncio.run_coroutine_threadsafe(handler(data), loop).result(3)

    def add_avatar_audio(self) -> None:
        self.remote_participants[AVATAR].track_publications["a"] = SimpleNamespace(kind=KIND_AUDIO)
        if self.emits_events:  # rtc.Room emits track_published on its own thread/loop
            for handler in self.handlers.get("track_published", []):
                self.loop.call_soon_threadsafe(
                    handler, SimpleNamespace(kind=KIND_AUDIO), self.remote_participants[AVATAR]
                )

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def log(self, kind, detail=None):
        self.events.append(Event(kind, time.monotonic(), detail))

    def times(self, kind):
        return [e.at for e in self.events if e.kind == kind]

    @property
    def sid(self):  # rtc.Room.sid is awaitable
        async def get():
            return self.room_sid

        return get()

    async def connect(self, url, token):
        self.loop = asyncio.get_running_loop()
        self.connected_token = jwt.decode(token, options={"verify_signature": False})
        self.log("connect")

    async def disconnect(self):
        await self.gate("disconnect")
        self.disconnected = True
        self.log("disconnect")

    # -- avatar side ------------------------------------------------
    def avatar_joins(self):
        if self.mode == "never":
            return
        pubs = {"v": SimpleNamespace(kind=KIND_VIDEO)}
        if self.mode != "video_only":
            pubs["a"] = SimpleNamespace(kind=KIND_AUDIO)
        self.remote_participants[AVATAR] = SimpleNamespace(track_publications=pubs)

    def fire(self, method, payload="", caller=AVATAR):
        handler = self.local_participant.rpc[method]
        data = SimpleNamespace(caller_identity=caller, payload=payload)
        asyncio.ensure_future(handler(data))
        self.log(method)

    def avatar_started(self):
        if not self.auto:
            return
        self.fire("lk.playback_started")

    def avatar_plays(self, stream: Stream):
        if self.mode == "silent" or not self.auto:
            return
        asyncio.get_event_loop().call_later(
            self.play_delay, lambda: self.fire("lk.playback_finished", '{"interrupted": false}')
        )


class FakeStatusError(Exception):
    """Like the plugin's APIStatusError: carries ``status_code`` and a provider ``body``."""

    def __init__(self, status_code: int, body: str = "") -> None:
        super().__init__(f"LemonSlice Server returned an error {body}")
        self.status_code, self.body = status_code, body


class _PluginClient:
    """Double of ``LemonSliceAPI`` (async context manager + ``start_agent_session``)."""

    def __init__(self, fake: "FakeLemonSlice") -> None:
        self.fake = fake

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def start_agent_session(self, **kw) -> str:
        f = self.fake
        f.starts.append(kw)
        if f._hold is not None:
            f.parked.set()
            while not f._hold.is_set():
                await asyncio.sleep(0.005)
        if f.start_raise is not None:
            raise f.start_raise
        if f.status >= 300:
            raise FakeStatusError(f.status, "provider says no")
        f.room.avatar_joins()
        return f.session_id


class FakeLemonSlice:
    """Fake plugin client (session start) plus the control endpoint (``__call__``).

    Launching a session makes the avatar join the room.
    """

    def __init__(self, room: FakeRoom, status: int = 200) -> None:
        self.room, self.status, self.calls = room, status, []
        self.parked = threading.Event()
        self._hold: threading.Event | None = None
        self.starts: list[dict] = []  # kwargs of every plugin start_agent_session call
        self.start_raise: BaseException | None = None
        self.controls: list[tuple[str, str]] = []  # (url, event) of every control POST
        self.control_status = 200
        self.control_raise: BaseException | None = None  # raised by every control POST
        self.session_id = "ls-1"  # what the plugin client answers

    def client_factory(self) -> _PluginClient:
        return _PluginClient(self)

    def hold(self) -> threading.Event:
        """Park the next session start until the returned event is set (creation delay)."""
        self._hold = threading.Event()
        return self._hold

    def __call__(self, url, headers, body, timeout):
        """Control endpoint only: terminate | reset-idle-timeout."""
        self.calls.append((url, headers, body))
        assert "event" in body, "session start must go through the plugin client, not raw REST"
        self.controls.append((url, body["event"]))
        if self.control_raise is not None:
            raise self.control_raise
        return self.control_status, {}
