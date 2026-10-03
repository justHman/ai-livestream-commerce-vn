"""LemonSlice documented-contract alignment (P0-FB-010b; DOC-via-assistant, not verified)."""

from __future__ import annotations

import logging
import time

import pytest

from backend.application.clients.avatar.lemonslice import (
    LemonSliceRenderBackend,
    SessionNearCap,
)
from backend.application.render.engines_base import StartOptions

from .lemonslice_double import FakeLemonSlice, FakeRoom
from .test_lemonslice_backend import LS_KEY, make, settings, win
from .test_lemonslice_races import until

pytestmark = pytest.mark.timeout(30)

BASE = "https://lemonslice.com/api/liveai"


def test_livekit_session_id_absent_by_default():
    backend, room, rest = make()
    backend.start(StartOptions())
    assert "livekit_session_id" not in rest.calls[0][2]["properties"]
    backend.stop_all()


def test_livekit_session_id_is_opt_in():
    backend, room, rest = make(send_livekit_session_id=True)
    res = backend.start(StartOptions())
    assert rest.calls[0][2]["properties"]["livekit_session_id"] == res.session_id
    backend.stop_all()


def test_default_terminate_is_control_event_on_the_api_base():
    backend, room, rest = make(keepalive_s=0)
    backend.stop(backend.start(StartOptions()).session_id)
    assert rest.controls == [(f"{BASE}/sessions/ls-1/control", "terminate")]
    assert rest.calls[-1][1] == {"X-API-Key": LS_KEY}
    backend.stop_all()


def test_terminate_path_override_still_sends_the_terminate_event():
    backend, room, rest = make(keepalive_s=0, terminate_path="/v2/{session_id}/end")
    backend.stop(backend.start(StartOptions()).session_id)
    assert rest.controls == [(f"{BASE}/v2/ls-1/end", "terminate")]
    backend.stop_all()


def test_keepalive_resets_idle_timeout_while_silent_and_stops_on_stop():
    backend, room, rest = make(keepalive_s=0.05)
    sid = backend.start(StartOptions()).session_id
    assert until(lambda: sum(e == "reset-idle-timeout" for _, e in rest.controls) >= 2)
    backend.stop(sid)
    n = len(rest.controls)
    time.sleep(0.3)
    assert len(rest.controls) == n and rest.controls[-1][1] == "terminate"
    backend.stop_all()


def test_keepalive_disabled_sends_nothing():
    backend, room, rest = make(keepalive_s=0)
    backend.start(StartOptions())
    time.sleep(0.2)
    assert rest.controls == []
    backend.stop_all()


def test_keepalive_failures_are_logged_by_class_and_never_break_audio(caplog):
    backend, room, rest = make(keepalive_s=0.03)
    rest.control_raise = ConnectionError(f"{BASE} X-API-Key={LS_KEY}")
    sid = backend.start(StartOptions()).session_id
    with caplog.at_level(logging.WARNING):
        assert until(lambda: "keepalive failing" in caplog.text)
    assert "ConnectionError" in caplog.text and LS_KEY not in caplog.text
    backend.stream_audio(sid, win("u1", 0, final=True))
    assert backend.session_status(sid) == "active"
    rest.control_raise = None
    backend.stop_all()


def test_keepalive_skips_while_an_utterance_is_open():
    backend, room, rest = make(keepalive_s=0.03)
    room.auto = False  # the utterance stays open: no playback_finished
    sid = backend.start(StartOptions()).session_id
    time.sleep(0.15)  # silent: keepalives flow
    backend.stream_audio(sid, win("u1", 0, final=False))
    time.sleep(0.1)  # let an in-flight tick land
    n = sum(e == "reset-idle-timeout" for _, e in rest.controls)
    time.sleep(0.3)
    assert sum(e == "reset-idle-timeout" for _, e in rest.controls) == n
    backend.stop_all()


def _aged(max_session_s=100.0):
    now = [1000.0]
    room = FakeRoom()
    rest = FakeLemonSlice(room)
    backend = LemonSliceRenderBackend(
        settings(max_session_s=max_session_s, keepalive_s=0),
        room_factory=lambda: room,
        http_post=rest,
        monotonic=lambda: now[0],
    )
    return backend, now


def test_age_guard_warns_once_at_80_percent_then_refuses_new_utterances(caplog):
    backend, now = _aged()
    sid = backend.start(StartOptions()).session_id
    now[0] += 79
    assert backend.session_status(sid) == "active"
    now[0] += 2
    with caplog.at_level(logging.WARNING):
        assert backend.session_status(sid) == "active"
        backend.session_status(sid)
    assert caplog.text.count("80%") == 1
    now[0] += 30
    assert backend.session_status(sid) == "near_cap"
    with pytest.raises(SessionNearCap) as err:
        backend.stream_audio(sid, win("u1", 0, final=True))
    assert err.value.code == "session_near_cap"
    backend.stop_all()


def test_age_guard_disabled_with_zero():
    backend, now = _aged(max_session_s=0)
    sid = backend.start(StartOptions()).session_id
    now[0] += 10**6
    assert backend.session_status(sid) == "active"
    backend.stop_all()
