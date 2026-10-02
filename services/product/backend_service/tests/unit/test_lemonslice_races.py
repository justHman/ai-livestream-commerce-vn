"""P0-FB-010 Runtime: races the immediate protocol double cannot show.

The double parks open/write/close/disconnect/REST at gates the test controls, and
the test injects avatar playback events itself (``room.auto = False``), so every
interleaving below is deterministic. Each test names the fix it guards; the
mutation of that fix (removing the epoch re-check, the event correlation, the
audio-track check, the future cancellation, ...) makes the test fail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

import pytest

from backend.application.clients.avatar.lemonslice import (
    AvatarAudioFallbackRefused,
    LemonSliceError,
    LemonSliceRenderBackend,
    _FallbackAudioTrack,
)
from backend.application.publishing.datastream import AvatarIOError
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow

from .lemonslice_double import FakeLemonSlice, FakeRoom
from .test_lemonslice_backend import LK_SECRET, LS_KEY, settings, win

pytestmark = pytest.mark.timeout(40)


def make(mode="normal", http_post=None, **kw):
    room = FakeRoom(mode)
    rest = FakeLemonSlice(room)
    pushed: list[bytes] = []

    def track_factory(_room, _rate):
        async def publish():
            room.audio_track_published = True

        def capture(pcm):
            pushed.append(pcm)

        def clear():
            room.queue_cleared += 1

        return publish, capture, clear

    backend = LemonSliceRenderBackend(
        settings(**kw),
        room_factory=lambda: room,
        http_post=http_post or rest,
        audio_track_factory=track_factory,
    )
    room.pushed = pushed
    return backend, room, rest


def bg(fn, *args, **kwargs):
    box: dict = {}

    def run():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as exc:
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def until(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


@pytest.fixture
def be():
    backend, room, rest = make(playback_margin_s=0.3)
    res = backend.start(StartOptions())
    yield backend, room, rest, res.session_id
    for gate in ("open", "write", "close", "disconnect"):
        room.release(gate)
    backend.stop_all()


# ---- P1-1: the epoch is an enforced fence --------------------------------------------------


def test_interrupt_while_stream_open_is_pending_writes_nothing_after_clear(be):
    backend, room, _, sid = be
    room.hold("open")
    t_send, _ = bg(backend.stream_audio, sid, win("u1", 0, ms=400))
    assert room.wait_hit("open")
    t_int, _ = bg(backend.interrupt, sid)
    time.sleep(0.2)
    assert t_int.is_alive() and not room.times(
        "clear_buffer_done"
    )  # clear waits for the stale send
    room.release("open")
    t_send.join(5), t_int.join(5)
    s = room.streams[0]
    assert s.chunks == [] and s.closed_reason == "interrupted"
    assert room.times("stream_closed")[0] <= room.times("clear_buffer_done")[0]


def test_interrupt_during_write_stops_the_remaining_slices(be):
    backend, room, _, sid = be
    room.hold("write")
    t_send, _ = bg(backend.stream_audio, sid, win("u1", 0, ms=800))  # 4 slices of 200 ms
    assert room.wait_hit("write")
    t_int, _ = bg(backend.interrupt, sid)
    time.sleep(0.1)
    room.release("write")
    t_send.join(5), t_int.join(5)
    s = room.streams[0]
    assert len(s.chunks) == 1  # only the slice already in flight when the interrupt landed
    assert s.chunks[0][0] <= room.times("clear_buffer_done")[0]
    assert s.closed_reason == "interrupted"


def test_interrupt_during_final_close_does_not_mark_playback_unconfirmed(be):
    backend, room, _, sid = be
    room.hold("close")
    t_send, box = bg(backend.stream_audio, sid, win("u1", 0, final=True))
    assert room.wait_hit("close")
    t_int, _ = bg(backend.interrupt, sid)
    time.sleep(0.1)
    room.release("close")
    t_send.join(5), t_int.join(5)
    assert "error" not in box
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is False


def test_window_queued_behind_interrupt_is_dropped(be):
    backend, room, _, sid = be
    room.hold("write")
    t1, _ = bg(backend.stream_audio, sid, win("u1", 0))
    assert room.wait_hit("write")
    t2, _ = bg(backend.stream_audio, sid, win("u1", 1, final=True))  # waits for the channel lock
    time.sleep(0.1)
    t_int, _ = bg(backend.interrupt, sid)
    time.sleep(0.1)
    room.release("write")
    for t in (t1, t2, t_int):
        t.join(5)
    assert len(room.streams[0].chunks) == 1


# ---- P1-2: playback events are matched to the utterance they belong to ---------------------


@pytest.fixture
def manual():
    backend, room, rest = make(playback_margin_s=0.4)
    room.auto = False
    sid = backend.start(StartOptions()).session_id
    yield backend, room, sid
    backend.stop_all()


def finished(room, backend, **payload):
    room.emit(backend._ensure_loop(), "lk.playback_finished", json.dumps(payload))


def test_finish_with_another_utterance_id_never_satisfies_the_waiting_one(manual):
    backend, room, sid = manual
    t, _ = bg(backend.stream_audio, sid, win("new", 0, final=True))
    assert until(lambda: room.streams and room.streams[0].closed_reason is None)
    finished(room, backend, utterance_id="old", interrupted=False)
    t.join(5)
    assert backend.playback_info(sid, "new")["playback_unconfirmed"] is True


def test_finish_before_the_stream_is_closed_is_dropped(manual):
    backend, room, sid = manual
    backend.stream_audio(sid, win("u1", 0))  # open, written, not final
    finished(room, backend, interrupted=False)  # stale completion lands mid-utterance
    t, _ = bg(backend.stream_audio, sid, win("u1", 1, final=True))
    t.join(5)
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is True


def test_interrupted_finish_never_completes_a_live_utterance(manual):
    backend, room, sid = manual
    t, _ = bg(backend.stream_audio, sid, win("u1", 0, final=True))
    assert until(lambda: room.streams and room.streams[0].closed_reason is None)
    finished(room, backend, interrupted=True)
    t.join(5)
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is True


def test_late_finish_of_a_timed_out_utterance_is_not_credited_to_the_next(manual):
    backend, room, sid = manual
    backend.stream_audio(sid, win("u1", 0, final=True))  # no finish: times out
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is True
    t, box = bg(backend.stream_audio, sid, win("u2", 0, final=True))
    assert until(lambda: len(room.streams) == 2 and room.streams[1].closed_reason is None)
    finished(room, backend, interrupted=False)  # u1's late completion
    time.sleep(0.15)
    assert t.is_alive()  # u2 is still waiting for its own
    finished(room, backend, interrupted=False)
    t.join(5)
    assert backend.playback_info(sid, "u2")["playback_unconfirmed"] is False


def test_started_for_another_utterance_does_not_stamp_the_current_one(manual):
    backend, room, sid = manual
    backend.stream_audio(sid, win("u1", 0))
    room.emit(backend._ensure_loop(), "lk.playback_started", json.dumps({"utterance_id": "old"}))
    assert backend.playback_info(sid, "u1")["playback_started_at"] is None
    room.emit(backend._ensure_loop(), "lk.playback_started", json.dumps({"utterance_id": "u1"}))
    assert backend.playback_info(sid, "u1")["playback_started_at"] is not None


def test_started_before_any_audio_was_written_stamps_nothing(manual):
    backend, room, sid = manual
    room.hold("write")
    t, _ = bg(backend.stream_audio, sid, win("u1", 0))
    assert room.wait_hit("write")
    room.emit(backend._ensure_loop(), "lk.playback_started")
    room.release("write")
    t.join(5)
    assert backend.playback_info(sid, "u1")["playback_started_at"] is None


# ---- P1-3: interrupt reaches the fallback track; P1-4: never two audio publishers ----------


def fallback_backend(**kw):
    backend, room, rest = make(
        mode="video_only", fallback_publish=True, audio_probe_s=0.05, render_offset_ms=300, **kw
    )
    return backend, room, rest


def test_pcm_sleeping_for_its_render_offset_is_not_pushed_after_interrupt():
    backend, room, _ = fallback_backend()
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("u1", 0))
        backend.interrupt(sid)
        time.sleep(0.6)
        assert room.pushed == [] and room.queue_cleared >= 1
    finally:
        backend.stop_all()


def test_fallback_pcm_is_pushed_after_the_offset_when_not_interrupted():
    backend, room, _ = fallback_backend()
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("u1", 0))
        assert until(lambda: room.pushed, 2)
    finally:
        backend.stop_all()


def test_audio_plus_video_avatar_refuses_fallback_and_publishes_no_second_track():
    backend, room, _ = make(fallback_publish=True, audio_probe_s=0.05)
    with pytest.raises(AvatarAudioFallbackRefused) as err:
        backend.start(StartOptions())
    assert err.value.code == "avatar_audio_fallback_refused"
    assert room.audio_track_published is False and room.disconnected
    backend.stop_all()


def test_video_only_avatar_allows_the_fallback_publisher():
    backend, room, _ = fallback_backend()
    backend.start(StartOptions())
    assert room.audio_track_published is True
    backend.stop_all()


def test_avatar_audio_appearing_after_start_stops_the_fallback_before_any_write():
    backend, room, _ = fallback_backend()
    sid = backend.start(StartOptions()).session_id
    try:
        room.add_avatar_audio()
        with pytest.raises(AvatarAudioFallbackRefused):
            backend.stream_audio(sid, win("u1", 0))
        assert room.streams == [] and room.pushed == []
    finally:
        backend.stop_all()


def test_fallback_buffer_is_bounded():
    async def go():
        track = _FallbackAudioTrack(lambda pcm: None, 10_000, max_queue=3)
        for i in range(10):
            track.enqueue(bytes([i]))
        size = track._q.qsize()
        track.close()
        return size

    assert asyncio.run(go()) == 3


# ---- P2: lifecycle, deadlines, redaction ---------------------------------------------------


def test_start_timeout_cancels_startup_and_ends_a_session_created_late():
    backend, room, rest = make(terminate_path="/sessions/{session_id}/terminate")
    release = rest.hold()
    with pytest.raises(LemonSliceError) as err:
        backend._run(backend._start("agent-preset"), 0.3)
    assert err.value.code == "timeout"
    assert rest.parked.is_set()
    threading.Timer(0.2, release.set).start()  # the lost REST response finally lands
    assert until(lambda: rest.calls[-1][0].endswith("/sessions/ls-1/terminate"), 5)
    assert until(lambda: room.disconnected, 5)
    assert backend._sessions == {}
    backend.stop_all()


def test_stop_all_stops_the_loop_thread_and_the_backend_can_restart():
    backend, room, _ = make()
    backend.start(StartOptions())
    thread = backend._thread
    backend.stop_all()
    assert not thread.is_alive() and backend._loop is None
    backend.start(StartOptions())
    backend.stop_all()


def test_stream_write_has_an_application_deadline_and_the_channel_recovers():
    backend, room, _ = make(io_timeout_s=0.2)
    sid = backend.start(StartOptions()).session_id
    room.hold("write")
    t0 = time.monotonic()
    with pytest.raises(AvatarIOError):
        backend.stream_audio(sid, win("u1", 0))
    assert time.monotonic() - t0 < 2
    room.release("write")
    backend.stream_audio(sid, win("u2", 0))
    assert room.streams[-1].attributes["livento.utterance_id"] == "u2"
    backend.stop_all()


def test_stuck_disconnect_does_not_hang_stop():
    backend, room, _ = make(io_timeout_s=0.2)
    sid = backend.start(StartOptions()).session_id
    room.hold("disconnect")
    t0 = time.monotonic()
    backend.stop(sid)
    assert time.monotonic() - t0 < 5
    room.release("disconnect")
    backend.stop_all()


def test_terminate_failure_is_retried_and_logged_by_class_only(caplog):
    calls = []

    def post(url, headers, body, timeout):
        calls.append(url)
        if "terminate" in url:
            raise ConnectionError(f"{url} X-API-Key={LS_KEY}")
        room_ref[0].avatar_joins()
        return 200, {"session_id": "ls-9"}

    room_ref = [None]
    backend, room, _ = make(
        http_post=post, terminate_path="/sessions/{session_id}/terminate", terminate_attempts=2
    )
    room_ref[0] = room
    sid = backend.start(StartOptions()).session_id
    with caplog.at_level(logging.WARNING):
        backend.stop(sid)
    assert sum("terminate" in c for c in calls) == 2
    text = caplog.text
    assert "ConnectionError" in text and LS_KEY not in text and "lemonslice.com" not in text
    assert "NOT terminated" in text
    backend.stop_all()


def test_unexpected_start_error_is_redacted_and_not_chained(caplog):
    backend, room, _ = make()

    async def boom(url, token):
        raise RuntimeError(f"cannot reach {url} with {token} {LS_KEY} {LK_SECRET}")

    room.connect = boom
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LemonSliceError) as err:
            backend.start(StartOptions())
    shown = str(err.value) + repr(err.value) + caplog.text
    assert err.value.code == "start_failed" and err.value.__cause__ is None
    assert err.value.__suppress_context__
    assert LS_KEY not in shown and LK_SECRET not in shown and "wss://lk.example.test" not in shown
    backend.stop_all()


def test_playback_history_is_bounded():
    backend, room, _ = make(history=3)
    sid = backend.start(StartOptions()).session_id
    for i in range(8):
        backend.stream_audio(sid, win(f"u{i}", 0, final=True))
    assert len(backend._sessions[sid].channel.playback_started_at) <= 3
    backend.stop_all()


# ---- resampling edge cases -----------------------------------------------------------------


def raw(uid, seq, pcm, rate=16000, final=False):
    return AudioWindow(
        session_id="s",
        utterance_id=uid,
        seq=seq,
        sample_rate=rate,
        duration_ms=1,
        pcm=pcm,
        is_final=final,
    )


def test_odd_byte_windows_never_split_an_int16_sample(be):
    backend, room, _, sid = be
    backend.stream_audio(sid, raw("u1", 0, b"\x01" * 3201))
    backend.stream_audio(sid, raw("u1", 1, b"\x01" * 3201, final=True))
    chunks = [c for _, c in room.streams[0].chunks]
    assert all(len(c) % 2 == 0 for c in chunks) and sum(map(len, chunks)) == 6402


def test_empty_final_window_closes_the_stream_without_writing(be):
    backend, room, _, sid = be
    backend.stream_audio(sid, raw("u1", 0, b"", final=True))
    assert room.streams[0].chunks == [] and room.streams[0].closed_reason is None


@pytest.mark.parametrize("rate", [8000, 24000, 48000])
def test_rate_change_keeps_duration_and_even_byte_count(be, rate):
    backend, room, _, sid = be
    backend.stream_audio(sid, win("u1", 0, ms=500, rate=rate, final=True))
    pcm = room.streams[0].pcm
    assert len(pcm) % 2 == 0 and abs(len(pcm) / 2 / 16000 - 0.5) <= 0.02
