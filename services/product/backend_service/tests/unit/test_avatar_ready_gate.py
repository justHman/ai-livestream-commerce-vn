"""Avatar delivery regressions, offline room and coordinator doubles."""

import asyncio
import time
import threading

import pytest

from backend.application.clients.avatar.lemonslice import LemonSliceError
from backend.application.render.engines_base import StartOptions
from .test_lemonslice_backend import make, win
from .lemonslice_double import AVATAR
from . import test_approved_speech_active as speech_tests
from .test_approved_speech_active import decision

case_factory = speech_tests.case_factory


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["interrupt", "cancel", "backend_interrupt", "stop"])
async def test_muted_wait_releases_coordinator_lock_promptly(case_factory, action):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    co._orchestrator_registry = {}
    backend, room, _ = make(speak_ready_wait_s=2, keepalive_s=0)
    started = threading.Event()
    pending = None
    try:
        real_sid = backend.start(StartOptions()).session_id
        backend._sessions[sid] = backend._sessions.pop(real_sid)
        co._backend = backend
        pub = room.remote_participants[AVATAR].track_publications["v"]
        pub.muted = True
        original = backend._prepare_ready

        async def observed(*args, **kwargs):
            started.set()
            return await original(*args, **kwargs)

        backend._prepare_ready = observed
        turn = decision(case)
        pending = asyncio.create_task(co._maybe_speak(sid, turn))
        assert await asyncio.to_thread(started.wait, 1)
        assert co._lock_registry.is_locked(sid)
        before = time.monotonic()
        if action == "interrupt":
            await co.interrupt(sid)
        elif action == "cancel":
            await co._orchestrator_registry[sid]["orchestrator"].cancel(sid)
        elif action == "backend_interrupt":
            await asyncio.to_thread(backend.interrupt, sid)
        else:
            await asyncio.to_thread(backend.stop, sid)
        await asyncio.wait_for(asyncio.shield(pending), 0.15)
        assert time.monotonic() - before < 0.15
        assert not co._lock_registry.is_locked(sid)
        assert not room.streams
        assert not any(e.get("reason") == "avatar_not_ready" for e in case.events)
        assert co._completed_speech[sid]["state"] == "cancelled_stale"
        if action != "stop":
            # Keep video muted until the cancelled worker has actually released the lock.
            assert pub.muted
            pub.muted = False
            await co._maybe_speak(sid, decision(case))
            assert room.streams
    finally:
        # Also let the unfixed implementation finish after the timing assertion fails.
        if pending is not None:
            pub.muted = False
            await asyncio.gather(pending, return_exceptions=True)
        backend.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [ConnectionError, TimeoutError])
async def test_opening_partial_tts_failure_is_not_replayed(case_factory, failure):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    case.backend.speak_ready_gate_enabled = True
    turn = decision(case)
    turn.action = "autonomous_opening"
    original = case.tts.stream_audio
    calls, sent = [], []
    stream = case.backend.stream_audio

    def capture(sid, window):
        sent.append(window)
        return stream(sid, window)

    def partial(chunk, **kwargs):
        calls.append(chunk.text)
        yield from original(chunk, **kwargs)
        if len(calls) == 1:
            raise failure("after partial opening audio")

    case.backend.stream_audio = capture
    case.tts.stream_audio = partial
    await co._maybe_speak(sid, turn)
    assert sent, "reproduction must send partial audio before the transport failure"
    assert len(calls) == 1
    assert not any(e["type"] == "coordinator.speak_finished" for e in case.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_failure", [False, True])
async def test_opening_unconfirmed_retries_only_after_recovery(case_factory, transport_failure):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    case.backend.speak_ready_gate_enabled = True
    turn = decision(case)
    turn.action = "autonomous_opening"
    events = []

    def info(*args):
        events.append("unconfirmed" if not events else "confirmed")
        return {"playback_unconfirmed": len(events) == 1}

    case.backend.playback_info = info
    case.backend.require_avatar_recovery = lambda _: events.append("recovered")
    original = case.tts.stream_audio
    retried_chunks = []

    def synthesize(chunk, **kwargs):
        if "recovered" in events:
            retried_chunks.append(chunk.text)
            if transport_failure:
                yield from original(chunk, **kwargs)
                raise ConnectionError("partial replay failed")
        yield from original(chunk, **kwargs)

    case.tts.stream_audio = synthesize
    await co._maybe_speak(sid, turn)
    if transport_failure:
        assert events == ["unconfirmed", "recovered"]
        assert len(retried_chunks) == 1
        assert not any(e["type"] == "coordinator.speak_finished" for e in case.events)
    else:
        assert events == ["unconfirmed", "recovered", "confirmed"]
        assert co._completed_speech[sid]["state"] == "completed"


@pytest.mark.asyncio
async def test_cancel_during_pending_rollover_detaches_waiter_not_provider():
    backend, room, rest = make(speak_ready_wait_s=2, keepalive_s=0)
    release = None
    pending = None
    try:
        sid = backend.start(StartOptions()).session_id
        backend.require_avatar_recovery(sid)
        release = rest.hold()
        cancelled = threading.Event()
        pending = asyncio.create_task(
            asyncio.to_thread(backend.wait_speak_ready, sid, cancelled=cancelled.is_set)
        )
        async with asyncio.timeout(1):
            while len(rest.starts) < 2:
                await asyncio.sleep(0.005)
        cancelled.set()
        assert await asyncio.wait_for(asyncio.shield(pending), 0.15) is False
        assert not backend._sessions[sid].roll_task.done()
        assert not room.streams
        release.set()
        assert await asyncio.to_thread(backend.wait_speak_ready, sid)
        assert len(rest.starts) == 2
    finally:
        if release is not None:
            release.set()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        backend.stop_all()


def test_first_utterance_waits_for_unmuted_video():
    backend, room, _ = make(speak_ready_wait_s=0.3)
    try:
        sid = backend.start(StartOptions()).session_id
        pub = room.remote_participants[AVATAR].track_publications["v"]
        pub.muted = True

        async def restore():
            await asyncio.sleep(0.08)
            assert not room.streams
            pub.muted = False

        backend._loop.call_soon_threadsafe(lambda: asyncio.create_task(restore()))
        backend.stream_audio(sid, win("first", 0, final=True))
        assert len(room.streams) == 1
    finally:
        backend.stop_all()


def test_absent_avatar_has_bounded_wait_and_no_pcm():
    backend, room, _ = make(speak_ready_wait_s=0.06)
    try:
        sid = backend.start(StartOptions()).session_id
        room.remote_participants.clear()
        room.mode = "never"
        backend._sessions[sid].next_roll_at = time.monotonic() + 60
        started = time.monotonic()
        with pytest.raises(LemonSliceError, match="avatar_not_ready"):
            backend.stream_audio(sid, win("first", 0, final=True))
        assert time.monotonic() - started < 1
        assert not room.streams
    finally:
        backend.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failures, expected, outcome", [(1, 2, "completed"), (0, 1, "completed"), (9, 3, "skipped")]
)
async def test_unit_retries_before_cursor_advances(case_factory, failures, expected, outcome):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    ds = case.d.director.get_session(sid)
    product = ds.director.state.products[0]
    product.units = (case.source.version.spoken_text, "next unit")
    product.next_unit = 0
    spoken_before = product.spoken_turns
    turn = decision(case)
    turn.unit_index = 0
    attempts = []
    recoveries = []
    case.backend.speak_ready_gate_enabled = True
    case.backend.require_avatar_recovery = lambda s: recoveries.append(s)

    def info(*args):
        attempts.append(product.next_unit)
        return {"playback_unconfirmed": len(attempts) <= failures}

    case.backend.playback_info = info
    await co._maybe_speak(sid, turn)
    assert attempts == [0] * expected
    assert product.next_unit == 1
    assert product.spoken_turns == spoken_before + (outcome == "completed")
    assert len(recoveries) == min(failures, expected)
    assert co._completed_speech[sid]["state"] == outcome
    if outcome == "skipped":
        assert any(e["type"] == "coordinator.avatar_delivery_skipped" for e in case.events)
    assert not co._frozen(sid)


def test_unconfirmed_recovery_replaces_old_avatar_before_replay():
    backend, room, rest = make(mode="silent", speak_ready_wait_s=0.8, keepalive_s=0)
    try:
        sid = backend.start(StartOptions()).session_id
        old = room.remote_participants[AVATAR].sid
        backend.stream_audio(sid, win("lost", 0, final=True))
        assert backend.playback_info(sid, "lost")["playback_unconfirmed"]
        backend.require_avatar_recovery(sid)
        room.mode = "normal"
        backend.stream_audio(sid, win("replay", 0, final=True))
        assert room.remote_participants[AVATAR].sid != old
        assert rest.controls[0][1] == "terminate"
        assert len(rest.starts) == 2
        assert not backend.playback_info(sid, "replay")["playback_unconfirmed"]
    finally:
        backend.stop_all()


def test_failed_termination_never_sends_replay_or_creates_second_avatar():
    backend, room, rest = make(speak_ready_wait_s=0.1, keepalive_s=0, terminate_attempts=1)
    try:
        sid = backend.start(StartOptions()).session_id
        backend.require_avatar_recovery(sid)
        rest.control_status = 500
        with pytest.raises(LemonSliceError, match="avatar_not_ready"):
            backend.stream_audio(sid, win("replay", 0, final=True))
        assert not room.streams
        assert len(rest.starts) == 1
    finally:
        rest.control_status = 200
        backend.stop_all()


@pytest.mark.asyncio
async def test_reconnecting_is_recoverable_live_follow_position():
    from .test_live_follow import _recorder, _ds, _script

    recorder, sent = _recorder()
    ds, product = _ds()
    turn = _script(0)
    recorder.turn_selected("s", turn, ds)
    recorder.avatar_reconnecting("s", turn, ds)
    position = recorder.snapshot("s", ds)["position"]
    assert position["phase"] == "avatar_reconnecting"
    assert position["script_cursor"]["next_unit_index"] == 0
    recorder.speak_started("s", turn, ds)
    assert recorder.snapshot("s", ds)["position"]["phase"] == "speaking"
    recorder.retire("s")
    await asyncio.sleep(0)


def test_gate_can_be_disabled_for_rollback():
    backend, room, _ = make(speak_ready_gate_enabled=False, keepalive_s=0)
    try:
        sid = backend.start(StartOptions()).session_id
        room.remote_participants[AVATAR].track_publications["v"].muted = True
        backend.stream_audio(sid, win("legacy", 0, final=True))
        assert len(room.streams) == 1
    finally:
        backend.stop_all()


def test_absent_first_avatar_returns_before_any_pcm():
    backend, room, _ = make(speak_ready_wait_s=0.3, keepalive_s=0)
    try:
        sid = backend.start(StartOptions()).session_id
        avatar = room.remote_participants.pop(AVATAR)
        backend._sessions[sid].next_roll_at = time.monotonic() + 60

        async def restore():
            await asyncio.sleep(0.08)
            assert not room.streams
            room.remote_participants[AVATAR] = avatar

        backend._loop.call_soon_threadsafe(lambda: asyncio.create_task(restore()))
        backend.stream_audio(sid, win("first", 0, final=True))
        assert len(room.streams) == 1
    finally:
        backend.stop_all()


def test_ready_deadline_includes_rollover_and_serializes_late_creation():
    backend, room, rest = make(speak_ready_wait_s=0.06, keepalive_s=0)
    release = None
    try:
        sid = backend.start(StartOptions()).session_id
        backend.require_avatar_recovery(sid)
        release = rest.hold()
        for uid in ("retry1", "retry2"):
            started = time.monotonic()
            with pytest.raises(LemonSliceError, match="avatar_not_ready"):
                backend.stream_audio(sid, win(uid, 0, final=True))
            assert time.monotonic() - started < 0.5
        assert not room.streams
        assert len(rest.starts) == 2  # one original, one pending replacement
    finally:
        if release is not None:
            release.set()
        backend.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("qa, attempts", [(False, 3), (True, 0)])
async def test_never_ready_skips_without_terminal_failure(case_factory, qa, attempts):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    turn = decision(case, generated=qa)
    calls = []
    case.backend.speak_ready_gate_enabled = True
    case.backend.avatar_ready = lambda _: False

    def wait(_, **kwargs):
        calls.append(1)
        raise LemonSliceError("avatar_not_ready")

    case.backend.wait_speak_ready = wait
    await asyncio.wait_for(co._maybe_speak(sid, turn), 3)
    assert len(calls) == attempts
    assert co._completed_speech[sid]["state"] == "skipped"
    assert not any(e["type"] == "coordinator.terminal_failure" for e in case.events)


def test_interrupt_while_waiting_does_not_send_late_pcm():
    backend, room, _ = make(speak_ready_wait_s=0.3, keepalive_s=0)
    try:
        sid = backend.start(StartOptions()).session_id
        pub = room.remote_participants[AVATAR].track_publications["v"]
        pub.muted = True

        async def interrupt_while_muted():
            await asyncio.sleep(0.05)
            await backend._interrupt(backend._sessions[sid])

        backend._loop.call_soon_threadsafe(lambda: asyncio.create_task(interrupt_while_muted()))
        started = time.monotonic()
        backend.stream_audio(sid, win("cancelled", 0, final=True))
        assert time.monotonic() - started < 0.15
        assert pub.muted
        assert not room.streams
    finally:
        backend.stop_all()


def test_env_controls_ready_gate(monkeypatch):
    from backend.config import AppConfig

    monkeypatch.setenv("LEMONSLICE_SPEAK_READY_GATE_ENABLED", "false")
    monkeypatch.setenv("LEMONSLICE_SPEAK_READY_WAIT_S", "0.25")
    cfg = AppConfig.from_env()
    assert cfg.lemonslice_speak_ready_gate_enabled is False
    assert cfg.lemonslice_speak_ready_wait_s == 0.25
