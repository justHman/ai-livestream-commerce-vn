"""Codex review fixes on top of P0-FB-010b (keep-alive teardown, terminal responses, id safety,
timer validation, fail-closed clear, history-eviction ambiguity, secret repr)."""

from __future__ import annotations

import threading
import time

import pytest

from backend.application.clients.avatar.lemonslice import LemonSliceError
from backend.application.publishing.datastream import AvatarChannelBroken
from backend.application.render.engines_base import StartOptions
from backend.config import AppConfig, LLMConfig, PublishingConfig, TTSConfig

from .test_lemonslice_backend import make, settings, win
from .test_lemonslice_races import (
    bg,
    channel,
    join,
    manual_backend,
    started,
    until,
)
from .test_lemonslice_races import make as races_make

pytestmark = pytest.mark.timeout(40)


# ---- A1: a running keep-alive HTTP call never outlives teardown ------------------------------


def test_terminate_waits_for_an_in_flight_keepalive_and_nothing_follows_it():
    holder: list = []
    order: list[str] = []
    parked, release = threading.Event(), threading.Event()

    def post(url, headers, body, timeout):
        event = body.get("event")
        if event == "reset-idle-timeout":
            parked.set()
            release.wait(5)
        if event:
            order.append(event)
        return holder[0](url, headers, body, timeout)

    backend, room, rest = races_make(
        http_post=post, keepalive_s=0.03, request_timeout_s=3.0, playback_margin_s=0.3
    )
    holder.append(rest)
    backend.start(StartOptions())
    assert parked.wait(3)
    t, _ = bg(backend.stop_all)
    time.sleep(0.4)  # teardown has run: terminate must still be waiting for the parked call
    assert "terminate" not in order
    release.set()
    join(t)
    time.sleep(0.2)
    assert order == ["reset-idle-timeout", "terminate"]


# ---- A2: terminal provider answers stop the keep-alive and degrade the session ---------------


@pytest.mark.parametrize("status", [404, 410])
def test_gone_session_stops_keepalive_and_is_degraded(status, caplog):
    backend, room, rest = make(keepalive_s=0.03)
    rest.control_status = status
    sid = backend.start(StartOptions()).session_id
    assert until(lambda: backend.session_status(sid) == "degraded")
    n = len(rest.controls)
    time.sleep(0.25)
    assert len(rest.controls) == n == 1
    assert caplog.text.count("keepalive terminal") == 1
    rest.control_status = 200
    backend.stop_all()


def test_repeated_auth_failures_are_terminal_but_one_is_not():
    backend, room, rest = make(keepalive_s=0.03, keepalive_fail_log_after=3)
    rest.control_status = 401
    sid = backend.start(StartOptions()).session_id
    assert until(lambda: backend.session_status(sid) == "degraded")
    assert len(rest.controls) == 3
    rest.control_status = 200
    backend.stop_all()


def test_transient_5xx_keeps_retrying_and_the_session_stays_active():
    backend, room, rest = make(keepalive_s=0.03)
    rest.control_status = 503
    sid = backend.start(StartOptions()).session_id
    assert until(lambda: len(rest.controls) >= 6)
    assert backend.session_status(sid) == "active"
    rest.control_status = 200
    backend.stop_all()


# ---- A3: provider session id is one safe path segment ----------------------------------------


@pytest.mark.parametrize(
    "bad", ["../../../admin", "a/b", "a?x=1", "a b", "x" * 129, "..%2f", "", 123, True, None]
)
def test_unsafe_provider_session_id_refuses_start_and_terminates_nothing(bad):
    backend, room, rest = make(keepalive_s=0.03)
    rest.session_id = bad
    with pytest.raises(LemonSliceError) as err:
        backend.start(StartOptions())
    assert err.value.code == "session_id_invalid"
    time.sleep(0.15)
    assert rest.controls == [] and room.disconnected
    backend.stop_all()


def test_safe_provider_session_id_is_used_in_the_control_url():
    backend, room, rest = make(keepalive_s=0)
    rest.session_id = "Abc_12-x"
    backend.stop(backend.start(StartOptions()).session_id)
    assert rest.controls[0][0].endswith("/sessions/Abc_12-x/control")
    backend.stop_all()


# ---- A4: timer settings are finite and non-negative -------------------------------------------

TIMERS = [
    "idle_timeout_s",
    "ready_timeout_s",
    "request_timeout_s",
    "keepalive_s",
    "max_session_s",
    "io_timeout_s",
    "clear_budget_s",
    "audio_recheck_s",
]


@pytest.mark.parametrize("name", TIMERS)
@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf")])
def test_settings_reject_unsafe_timers_by_name(name, bad):
    with pytest.raises(ValueError, match=name):
        settings(**{name: bad})


def test_zero_still_disables_keepalive_and_cap():
    s = settings(keepalive_s=0, max_session_s=0)
    assert s.keepalive_s == 0 and s.max_session_s == 0


@pytest.mark.parametrize(
    "var",
    [
        "LEMONSLICE_MAX_SESSION_S",
        "LEMONSLICE_KEEPALIVE_S",
        "LEMONSLICE_READY_TIMEOUT_S",
        "LEMONSLICE_IDLE_TIMEOUT_S",
    ],
)
@pytest.mark.parametrize("bad", ["-1", "nan", "inf", "abc"])
def test_env_rejects_unsafe_timers_naming_the_variable(monkeypatch, var, bad):
    monkeypatch.setenv(var, bad)
    with pytest.raises(ValueError, match=var):
        AppConfig.from_env()


def test_env_zero_is_an_explicit_disable(monkeypatch):
    monkeypatch.setenv("LEMONSLICE_MAX_SESSION_S", "0")
    monkeypatch.setenv("LEMONSLICE_KEEPALIVE_S", "0")
    cfg = AppConfig.from_env()
    assert cfg.lemonslice_max_session_s == 0 and cfg.lemonslice_keepalive_s == 0


# ---- B1: a failed clear fails the channel closed ----------------------------------------------


def test_failed_clear_rpc_breaks_the_channel_so_new_speech_is_refused():
    backend, room, _ = races_make(playback_margin_s=0.3)
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("u1", 0))  # speech is buffered at the avatar
        room.fail["rpc"] = RuntimeError("rpc blew up")
        backend.interrupt(sid)  # never raises into the coordinator
        assert channel(backend, sid).broken
        with pytest.raises(AvatarChannelBroken):
            backend.stream_audio(sid, win("u2", 0))
        assert len(room.streams) == 1
    finally:
        backend.stop_all()


def test_failed_clear_with_nothing_dispatched_is_benign():
    backend, room, _ = races_make(playback_margin_s=0.3)
    sid = backend.start(StartOptions()).session_id
    try:
        room.fail["rpc"] = RuntimeError("rpc blew up")
        backend.interrupt(sid)
        assert channel(backend, sid).broken is None
    finally:
        backend.stop_all()


# ---- B2: evicting an unresolved record never lets a later one take its event -----------------


def test_id_less_event_after_eviction_of_an_unresolved_record_is_not_credited():
    backend, room, sid = manual_backend(history=2, playback_margin_s=0.02)
    try:
        for i in range(3):  # u0 is evicted while still unresolved
            backend.stream_audio(sid, win(f"u{i}", 0, final=True))
        started(room, backend)  # u0's delayed id-less started
        assert backend.playback_info(sid, "u1")["playback_started_at"] is None
        assert backend.playback_info(sid, "u2")["playback_started_at"] is None
        backend.interrupt(sid)  # the clear ack resolves the ambiguity
        t, _ = bg(backend.stream_audio, sid, win("u3", 0))
        assert until(lambda: len(room.streams) == 4)
        started(room, backend)
        join(t)
        assert backend.playback_info(sid, "u3")["playback_started_at"] is not None
    finally:
        backend.stop_all()


# ---- B3: no credential in any config repr -----------------------------------------------------


def test_config_reprs_contain_no_secret():
    secrets = ["K-1", "S-2", "L-3", "B-4", "A-5", "P-6", "R-7", "T-8", "M-9"]
    cfg = AppConfig(
        livekit_api_key="K-1",
        livekit_api_secret="S-2",
        lemonslice_api_key="L-3",
        backend_api_token="B-4",
        admin_api_token="A-5",
        database_url="postgresql://u:P-6@h/db",
        redis_url="redis://:R-7@h/0",
        tts=TTSConfig(extra={"api_key": "T-8"}),
        llm=LLMConfig(extra={"api_key": "M-9"}),
    )
    pub = PublishingConfig(livekit_url="wss://x", livekit_api_key="K-1", livekit_api_secret="S-2")
    text = repr(cfg) + repr(pub)
    assert not [x for x in secrets if x in text]


# ---- round 2 ------------------------------------------------------------------------------------


def test_late_keepalive_worker_released_after_stop_sends_nothing():
    backend, room, rest = races_make(keepalive_s=0.03, request_timeout_s=0.5)
    sid = backend.start(StartOptions()).session_id
    lock = backend._sessions[sid].ctl_lock
    lock.acquire()  # park the keep-alive worker right before its send
    try:
        time.sleep(0.2)
        backend.stop(sid)  # terminate waits its bound for the lock, then proceeds
    finally:
        lock.release()
    time.sleep(0.3)  # the late worker now runs: it must see the session closed
    assert [e for _, e in rest.controls] == ["terminate"]
    backend.stop_all()


def test_failed_clear_after_evicted_unresolved_audio_still_breaks_the_channel():
    backend, room, _ = races_make(history=1, playback_margin_s=0.02)
    room.auto = False
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("u1", 0))  # audio dispatched, never confirmed
        backend.stream_audio(sid, win("u2", 0, ms=0, final=True))  # evicts u1's record
        room.fail["rpc"] = RuntimeError("rpc blew up")
        backend.interrupt(sid)
        assert channel(backend, sid).broken
        with pytest.raises(AvatarChannelBroken):
            backend.stream_audio(sid, win("u3", 0))
    finally:
        backend.stop_all()


def test_failed_clear_after_completed_playback_is_benign():
    backend, room, _ = races_make(playback_margin_s=0.5)
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("u1", 0, final=True))  # auto avatar confirms the finish
        room.fail["rpc"] = RuntimeError("rpc blew up")
        backend.interrupt(sid)
        assert channel(backend, sid).broken is None
    finally:
        backend.stop_all()


COUNTS = [
    "history",
    "fallback_max_queue",
    "audio_sample_rate",
    "terminate_attempts",
    "idle_timeout_s",
]


@pytest.mark.parametrize("name", COUNTS)
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 1.5, -1, True, "fake-secret-xyz"])
def test_settings_reject_non_integer_counts_without_echoing_the_value(name, bad):
    with pytest.raises(ValueError, match=name) as err:
        settings(**{name: bad})
    assert "fake-secret-xyz" not in str(err.value)


@pytest.mark.parametrize("name", TIMERS)
def test_settings_reject_non_numeric_timers_without_echoing_the_value(name):
    with pytest.raises(ValueError, match=name) as err:
        settings(**{name: "fake-secret-xyz"})
    assert "fake-secret-xyz" not in str(err.value)


@pytest.mark.parametrize(
    "var", ["LEMONSLICE_AUDIO_SAMPLE_RATE", "LEMONSLICE_IDLE_TIMEOUT_S", "AVATAR_RENDER_OFFSET_MS"]
)
@pytest.mark.parametrize("bad", ["1.5", "nan", "-1", "fake-secret-xyz"])
def test_env_integers_are_strict_and_never_echo_the_value(monkeypatch, var, bad):
    monkeypatch.setenv(var, bad)
    with pytest.raises(ValueError, match=var) as err:
        AppConfig.from_env()
    assert "fake-secret-xyz" not in str(err.value) and "1.5" not in str(err.value)


@pytest.mark.parametrize("var", ["LEMONSLICE_KEEPALIVE_S", "LEMONSLICE_MAX_SESSION_S"])
def test_env_timers_never_echo_the_value(monkeypatch, var):
    monkeypatch.setenv(var, "fake-secret-xyz")
    with pytest.raises(ValueError, match=var) as err:
        AppConfig.from_env()
    assert "fake-secret-xyz" not in str(err.value)


def test_stop_still_terminates_and_disconnects_with_a_corrupt_attempt_count():
    backend, room, rest = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    object.__setattr__(backend._s, "terminate_attempts", 1.5)
    backend.stop(sid)
    assert [e for _, e in rest.controls] == ["terminate"] and room.disconnected
    backend.stop_all()


# ---- round 3: real httpx, cancellable keep-alive with a TOTAL deadline -----------------------


class _DripServer:
    """Loopback server: /sessions answers at once, keep-alives drip bytes past any timeout."""

    def __init__(self) -> None:
        import json as _json
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.log: list[tuple[float, str]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def do_POST(self):
                body = _json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                event = body.get("event")
                if event is None:
                    data = _json.dumps({"session_id": "ls-1"}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                outer.log.append((time.monotonic(), event))
                if event == "terminate":
                    self.send_response(200)
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"{}")
                    return
                self.send_response(200)
                self.send_header("Content-Length", "1000")
                self.end_headers()
                try:
                    for _ in range(60):  # one byte per 0.1 s: never trips a per-chunk read timeout
                        self.wfile.write(b" ")
                        self.wfile.flush()
                        time.sleep(0.1)
                    outer.log.append((time.monotonic(), "reset-complete"))
                except OSError:
                    outer.log.append((time.monotonic(), "reset-closed"))  # client aborted

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_real_httpx_keepalive_is_cancelled_before_terminate_and_nothing_follows(
    monkeypatch, caplog
):
    import gc

    from backend.application.clients.avatar.lemonslice import LemonSliceRenderBackend

    from .lemonslice_double import FakeLemonSlice, FakeRoom

    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    srv = _DripServer()
    room = FakeRoom()
    room.avatar_joins()
    backend = LemonSliceRenderBackend(
        settings(api_base=srv.url, keepalive_s=0.03, request_timeout_s=0.3),
        room_factory=lambda: room,
        session_client_factory=FakeLemonSlice(room).client_factory,
    )
    try:
        backend.start(StartOptions())
        assert until(lambda: any(e == "reset-idle-timeout" for _, e in srv.log))
        t0 = time.monotonic()
        backend.stop_all()
        elapsed = time.monotonic() - t0
        time.sleep(0.5)  # a drip server notices an aborted client on its next write
        events = [e for _, e in srv.log]
        resets = events.count("reset-idle-timeout")
        assert elapsed < 2.5 and events.count("terminate") == 1
        assert "reset-complete" not in events  # none survived to completion
        assert events.count("reset-closed") == resets  # every request was aborted by the client
        assert [e for e in events if e == "reset-idle-timeout"] and events.index(
            "terminate"
        ) > events.index("reset-idle-timeout")
        assert events[events.index("terminate") :].count("reset-idle-timeout") == 0
        gc.collect()
        assert "pending" not in caplog.text.lower()
    finally:
        srv.close()


def test_control_deadline_setting_is_validated_and_defaults_to_request_timeout():
    for bad in (0, -1, float("nan"), float("inf"), True, "x"):
        with pytest.raises(ValueError, match="control_deadline_s"):
            settings(control_deadline_s=bad)
    assert settings().control_deadline_s is None


@pytest.mark.parametrize("bad", ["0", "-1", "nan", "inf", "fake-secret-xyz"])
def test_env_control_deadline_must_be_positive(monkeypatch, bad):
    monkeypatch.setenv("LEMONSLICE_CONTROL_DEADLINE_S", bad)
    with pytest.raises(ValueError, match="LEMONSLICE_CONTROL_DEADLINE_S") as err:
        AppConfig.from_env()
    assert "fake-secret-xyz" not in str(err.value)


def test_terminate_waits_for_the_cancelled_keepalive_to_finish_aborting():
    import asyncio

    from backend.application.clients.avatar.lemonslice import LemonSliceRenderBackend

    from .lemonslice_double import FakeLemonSlice, FakeRoom

    order: list[str] = []

    async def post(url, headers, body, timeout):
        event = body.get("event")
        if event == "terminate":
            order.append("terminate")
            return 200, {}
        try:
            await asyncio.sleep(30)  # a request that never completes
        finally:
            await asyncio.sleep(0.3)  # slow to abort
            order.append("keepalive-aborted")
        return 200, {}

    room = FakeRoom()
    room.avatar_joins()
    backend = LemonSliceRenderBackend(
        settings(keepalive_s=0.03, control_deadline_s=5.0),
        room_factory=lambda: room,
        session_client_factory=FakeLemonSlice(room).client_factory,
        async_http_post=post,
        http_post=lambda *a: (200, {}),
    )
    backend.start(StartOptions())
    time.sleep(0.2)
    backend.stop_all()
    assert order == ["keepalive-aborted", "terminate"]
