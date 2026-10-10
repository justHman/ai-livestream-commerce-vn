"""LemonSlice documented-contract alignment (P0-FB-010b; DOC-via-assistant, not verified)."""

from __future__ import annotations

import logging
import time

import pytest

from backend.application.clients.avatar.lemonslice import (
    LemonSliceError,
    LemonSliceRenderBackend,
    SessionNearCap,
)
from backend.application.render.engines_base import StartOptions

from .lemonslice_double import FakeLemonSlice, FakeRoom, FakeStatusError, fake_plugin_modules
from .test_lemonslice_backend import LS_KEY, make, settings, win
from .test_lemonslice_races import until

pytestmark = pytest.mark.timeout(30)

BASE = "https://lemonslice.com/api/liveai"


def test_start_passes_the_room_sid_as_livekit_session_id_like_the_plugin():
    backend, room, rest = make()
    backend.start(StartOptions())
    assert rest.starts[0]["livekit_session_id"] == "RM_fake"
    backend.stop_all()


def test_room_name_is_the_session_id_fallback_when_the_room_has_no_sid():
    backend, room, rest = make()
    room.room_sid = ""
    res = backend.start(StartOptions())
    assert rest.starts[0]["livekit_session_id"] == res.session_id
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
        session_client_factory=rest.client_factory,
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


# ---- P0-FB-010c: session start goes through the plugin API client (library only) ------------


def test_default_client_is_the_plugin_api_client_with_retries_disabled(monkeypatch):
    from backend.application.clients.avatar.lemonslice import _load_plugin, _plugin_client_factory

    made: list = []
    fake_plugin_modules(monkeypatch, made)
    _plugin_client_factory(settings(request_timeout_s=7.0), _load_plugin())()
    kw = made[0]
    assert kw["api_url"] == f"{BASE}/sessions" and kw["api_key"] == LS_KEY
    # a retried POST could start a second billable session
    assert kw["conn_options"].max_retry == 0 and kw["conn_options"].timeout == 7.0


def test_missing_plugin_fails_fast_at_construction_with_a_typed_error(monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "livekit.plugins.lemonslice.api", None)  # ImportError
    with pytest.raises(LemonSliceError) as err:
        LemonSliceRenderBackend(settings(), room_factory=lambda: FakeRoom())
    assert err.value.code == "plugin_not_installed" and err.value.__context__ is None


def test_plugin_init_failure_fails_fast_with_a_typed_error(monkeypatch):
    import sys
    import types

    fake_plugin_modules(monkeypatch, [])  # livekit.agents importable even without the extra

    class Boom(types.ModuleType):
        def __getattr__(self, name):
            raise RuntimeError("Plugins must be registered on the main thread")

    monkeypatch.setitem(sys.modules, "livekit.plugins.lemonslice.api", Boom("x"))
    with pytest.raises(LemonSliceError) as err:
        LemonSliceRenderBackend(settings(), room_factory=lambda: FakeRoom())
    assert err.value.code == "plugin_init_failed" and "RuntimeError" in str(err.value)
    assert err.value.__context__ is None and "main thread" not in str(err.value)


@pytest.mark.parametrize(
    "exc, expect",
    [
        (FakeStatusError(429, f"body {LS_KEY}"), "status=429"),
        (ConnectionError(f"{BASE} X-API-Key={LS_KEY}"), "error_type=ConnectionError"),
        (TimeoutError(), "error_type=TimeoutError"),
    ],
)
def test_plugin_client_failure_is_redacted_typed_and_leaves_the_room(exc, expect):
    backend, room, rest = make()
    rest.start_raise = exc
    with pytest.raises(LemonSliceError) as err:
        backend.start(StartOptions())
    e = err.value
    assert e.code == "session_request_failed" and expect in str(e)
    assert LS_KEY not in str(e) and "lemonslice.com" not in str(e)
    assert e.__cause__ is None and e.__context__ is None and room.disconnected
    assert rest.controls == []  # nothing to terminate: no session id was ever returned
    backend.stop_all()


def test_slow_plugin_start_hits_the_request_deadline():
    backend, room, rest = make(request_timeout_s=0.2, ready_timeout_s=2.0)
    release = rest.hold()
    with pytest.raises(LemonSliceError) as err:
        backend.start(StartOptions())
    assert err.value.code == "session_request_failed" and "TimeoutError" in str(err.value)
    assert room.disconnected
    release.set()  # the late session lands: the reaper ends it
    assert until(lambda: [e for _, e in rest.controls] == ["terminate"], 5)
    backend.stop_all()


def test_backend_never_uses_agent_session_or_worker_apis():
    """Single speaker (010c): the plugin is a library for its API client only."""
    import ast
    import inspect

    import backend.application.clients.avatar.lemonslice as mod

    tree = ast.parse(inspect.getsource(mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    forbidden = {"AgentSession", "Agent", "AgentServer", "AvatarSession", "cli", "WorkerOptions"}
    assert not names & forbidden
    assert not any(n.startswith("livekit.agents.voice") for n in names)
    assert {"livekit.agents", "livekit.plugins.lemonslice.api"} >= {
        n for n in names if n.startswith("livekit.agents") or n.startswith("livekit.plugins")
    }


def _rolling(rollover_after_s=60.0, max_session_s=100.0):
    now = [1000.0]
    room = FakeRoom()
    rest = FakeLemonSlice(room)
    backend = LemonSliceRenderBackend(
        settings(max_session_s=max_session_s, rollover_after_s=rollover_after_s, keepalive_s=0),
        room_factory=lambda: room,
        session_client_factory=rest.client_factory,
        http_post=rest,
        monotonic=lambda: now[0],
    )
    return backend, rest, now


def test_rollover_replaces_the_provider_session_before_the_cap():
    backend, rest, now = _rolling()
    sid = backend.start(StartOptions()).session_id
    now[0] += 61
    rest.session_id = "ls-2"
    backend.stream_audio(sid, win("u1", 0, final=True))
    assert len(rest.starts) == 2 and rest.starts[1]["livekit_session_id"] == "RM_fake"
    assert rest.controls == [(f"{BASE}/sessions/ls-1/control", "terminate")]
    now[0] += 59  # 120 s after the start, but only 59 s after the rollover
    assert backend.session_status(sid) == "active"
    rest.session_id = "ls-3"
    backend.stream_audio(sid, win("u2", 0, final=True))  # the age guard does not refuse it
    assert len(rest.starts) == 2  # and 59 s old is not due for another rollover
    backend.stop(sid)
    assert rest.controls[-1] == (f"{BASE}/sessions/ls-2/control", "terminate")
    backend.stop_all()


def test_rollover_is_off_by_default():
    backend, rest, now = _rolling(rollover_after_s=0.0)
    sid = backend.start(StartOptions()).session_id
    now[0] += 90
    backend.stream_audio(sid, win("u1", 0, final=True))
    assert len(rest.starts) == 1 and rest.controls == []
    backend.stop_all()


def test_rollover_waits_for_the_next_utterance_boundary():
    backend, rest, now = _rolling()
    sid = backend.start(StartOptions()).session_id
    backend.stream_audio(sid, win("u1", 0, final=False))  # utterance u1 is open
    now[0] += 61
    backend.stream_audio(sid, win("u1", 1, final=True))  # the same utterance continues
    assert len(rest.starts) == 1
    rest.session_id = "ls-2"
    backend.stream_audio(sid, win("u2", 0, final=True))  # a new utterance: roll over now
    assert len(rest.starts) == 2
    backend.stop_all()


def test_failed_rollover_keeps_the_session_retries_later_and_the_cap_still_applies():
    backend, rest, now = _rolling()
    sid = backend.start(StartOptions()).session_id
    now[0] += 61
    rest.start_raise = ConnectionError("provider down")
    backend.stream_audio(sid, win("u1", 0, final=True))  # keeps speaking on the old session
    assert len(rest.starts) == 2 and rest.controls == []
    now[0] += 10
    backend.stream_audio(sid, win("u2", 0, final=True))
    assert len(rest.starts) == 2  # not retried inside the pause
    now[0] += 30  # now 101 s old: past the retry pause and past the cap
    with pytest.raises(SessionNearCap):
        backend.stream_audio(sid, win("u3", 0, final=True))
    assert len(rest.starts) == 3  # one more attempt was made before the guard refused
    backend.stop_all()


def test_rollover_must_start_before_the_age_guard():
    with pytest.raises(ValueError):
        settings(rollover_after_s=100.0, max_session_s=100.0)


def test_stop_during_a_rollover_ends_the_replacement_too():
    import threading

    backend, rest, now = _rolling()
    sid = backend.start(StartOptions()).session_id
    now[0] += 61
    rest.session_id = "ls-2"
    release = rest.hold()  # the replacement request is parked
    worker = threading.Thread(
        target=lambda: _ignoring(lambda: backend.stream_audio(sid, win("u1", 0, final=True)))
    )
    worker.start()
    assert rest.parked.wait(3)
    stopper = threading.Thread(target=lambda: _ignoring(lambda: backend.stop(sid)))
    stopper.start()
    time.sleep(0.2)
    release.set()
    worker.join(10)
    stopper.join(10)
    backend.stop_all()
    ended = {url for url, event in rest.controls if event == "terminate"}
    assert f"{BASE}/sessions/ls-1/control" in ended
    assert f"{BASE}/sessions/ls-2/control" in ended  # no billable session is left behind


def _ignoring(call):
    try:
        call()
    except Exception:  # the stopped session may refuse the late utterance
        pass


def test_rollover_is_skipped_when_there_is_no_avatar_to_replace():
    backend, rest, now = _rolling()
    sid = backend.start(StartOptions()).session_id
    backend._sessions[sid].room.remote_participants.clear()  # the avatar left the room
    now[0] += 61
    _ignoring(lambda: backend.stream_audio(sid, win("u1", 0, final=True)))
    assert len(rest.starts) == 1 and rest.controls == []
    backend.stop_all()


def test_a_replacement_that_lands_late_and_displaces_the_avatar_is_adopted():
    now = [1000.0]
    room = FakeRoom()
    rest = FakeLemonSlice(room)
    backend = LemonSliceRenderBackend(
        settings(
            max_session_s=100.0,
            rollover_after_s=60.0,
            keepalive_s=0,
            request_timeout_s=0.2,
            ready_timeout_s=0.3,
        ),
        room_factory=lambda: room,
        session_client_factory=rest.client_factory,
        http_post=rest,
        monotonic=lambda: now[0],
    )
    sid = backend.start(StartOptions()).session_id
    now[0] += 61
    rest.session_id = "ls-2"
    release = rest.hold()  # creation is slower than the rollover waits
    backend.stream_audio(sid, win("u1", 0, final=True))  # rollover times out: old session speaks on
    assert backend._sessions[sid].provider_session_id == "ls-1"
    release.set()  # the replacement lands late, joins and displaces the old avatar
    assert until(lambda: backend._sessions[sid].provider_session_id == "ls-2")
    assert until(lambda: (f"{BASE}/sessions/ls-1/control", "terminate") in rest.controls)
    backend.stop_all()
