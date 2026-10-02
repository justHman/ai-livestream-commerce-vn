"""P0-FB-010 Runtime: races the immediate protocol double cannot show.

The double parks open/write/close/rpc/disconnect/REST at gates the test controls, and
the test injects avatar playback events itself (``room.auto = False``), so every
interleaving below is deterministic: ordering is synchronized on observable state
(``until``/``wait_hit``), not on sleeps, and every thread must terminate cleanly.
Each test names the fix it guards; removing that fix makes it fail.
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
from backend.application.publishing.datastream import (
    AvatarIOError,
    AvatarProtocolError,
    AvatarStreamError,
)
from backend.application.render.engines_base import StartOptions
from backend.application.render.windows import AudioWindow

from .lemonslice_double import FakeLemonSlice, FakeRoom
from .test_lemonslice_backend import LK_SECRET, LS_KEY, settings, win

pytestmark = pytest.mark.timeout(40)


def make(mode="normal", http_post=None, **kw):
    room = FakeRoom(mode)
    rest = FakeLemonSlice(room)
    room.pushed = []
    room.unpublished = False

    def track_factory(_room, _rate):
        async def publish():
            room.audio_track_published = True

        def capture(pcm):
            room.pushed.append(pcm)

        def clear():
            room.queue_cleared += 1

        async def unpublish():
            room.unpublished = True

        return publish, capture, clear, unpublish

    backend = LemonSliceRenderBackend(
        settings(**kw),
        room_factory=lambda: room,
        http_post=http_post or rest,
        audio_track_factory=track_factory,
    )
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


def until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def join(*threads, timeout=8.0):
    for t in threads:
        t.join(timeout)
        assert not t.is_alive(), "a background call never terminated"


def channel(backend, sid):
    return backend._sessions[sid].channel


def waiters(ch):
    return len(ch._io._waiters or ())


@pytest.fixture
def be():
    backend, room, rest = make(playback_margin_s=0.3)
    res = backend.start(StartOptions())
    yield backend, room, rest, res.session_id
    for gate in ("open", "write", "close", "rpc", "disconnect"):
        room.release(gate)
    backend.stop_all()


# ---- P1-1: the epoch is an enforced fence, and a clear is serialized with sends ----------


def test_interrupt_while_stream_open_is_pending_writes_nothing_after_clear(be):
    backend, room, _, sid = be
    room.hold("open")
    t_send, box = bg(backend.stream_audio, sid, win("u1", 0, ms=400))
    assert room.wait_hit("open")
    t_int, ibox = bg(backend.interrupt, sid)
    assert until(lambda: channel(backend, sid).epoch == 1)
    assert not room.times("clear_buffer_done")  # clear waits for the stale send
    room.release("open")
    join(t_send, t_int)
    assert "error" not in box and "error" not in ibox
    s = room.streams[0]
    assert s.chunks == [] and s.closed_reason == "interrupted"
    assert room.times("stream_closed")[0] <= room.times("clear_buffer_done")[0]


def test_interrupt_during_write_stops_the_remaining_slices(be):
    backend, room, _, sid = be
    room.hold("write")
    t_send, box = bg(backend.stream_audio, sid, win("u1", 0, ms=800))  # 4 slices of 200 ms
    assert room.wait_hit("write")
    t_int, ibox = bg(backend.interrupt, sid)
    assert until(lambda: channel(backend, sid).epoch == 1)
    room.release("write")
    join(t_send, t_int)
    assert "error" not in box and "error" not in ibox
    s = room.streams[0]
    assert len(s.chunks) == 1  # only the slice already in flight when the interrupt landed
    assert s.chunks[0][0] <= room.times("clear_buffer_done")[0]
    assert s.closed_reason == "interrupted"


def test_interrupt_during_final_close_does_not_mark_playback_unconfirmed(be):
    backend, room, _, sid = be
    room.hold("close")
    t_send, box = bg(backend.stream_audio, sid, win("u1", 0, final=True))
    assert room.wait_hit("close")
    t_int, ibox = bg(backend.interrupt, sid)
    assert until(lambda: channel(backend, sid).epoch == 1)
    room.release("close")
    join(t_send, t_int)
    assert "error" not in box and "error" not in ibox
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is False


def test_window_queued_behind_interrupt_is_dropped(be):
    backend, room, _, sid = be
    room.hold("write")
    t1, b1 = bg(backend.stream_audio, sid, win("u1", 0))
    assert room.wait_hit("write")
    t2, b2 = bg(backend.stream_audio, sid, win("u1", 1, final=True))  # waits for the lock
    assert until(lambda: waiters(channel(backend, sid)) == 1)
    t_int, ib = bg(backend.interrupt, sid)
    assert until(lambda: channel(backend, sid).epoch == 1)
    room.release("write")
    join(t1, t2, t_int)
    assert not any("error" in b for b in (b1, b2, ib))
    assert len(room.streams[0].chunks) == 1


def test_new_send_waits_for_the_whole_clear_rpc_and_opens_exactly_one_stream(be):
    backend, room, _, sid = be
    backend.stream_audio(sid, win("u1", 0))  # u1 open
    room.hold("rpc")
    t_int, ib = bg(backend.interrupt, sid)
    assert room.wait_hit("rpc")  # the clear RPC is parked
    t_new, nb = bg(backend.stream_audio, sid, win("u2", 0))  # new epoch
    assert until(lambda: waiters(channel(backend, sid)) == 1)
    assert len(room.streams) == 1  # nothing may open while the avatar buffer is being cleared
    room.release("rpc")
    join(t_int, t_new)
    assert "error" not in ib and "error" not in nb
    assert [s.attributes["livento.utterance_id"] for s in room.streams] == ["u1", "u2"]
    assert channel(backend, sid).open_utterance == "u2"


def test_hung_write_that_ignores_cancellation_cannot_block_interrupt_or_corrupt_next_stream():
    backend, room, _ = make(io_timeout_s=0.2)
    sid = backend.start(StartOptions()).session_id
    try:
        room.hold("write", resistant=True)
        t_send, box = bg(backend.stream_audio, sid, win("u1", 0, ms=400))
        assert room.wait_hit("write")
        t_int, ib = bg(backend.interrupt, sid)
        join(t_int)  # hard deadline: returns although the write ignores cancellation
        assert "error" not in ib
        join(t_send)
        assert isinstance(box["error"], AvatarIOError)
        room.blocked.discard("write")
        backend.stream_audio(sid, win("u2", 0))
        assert [s.attributes["livento.utterance_id"] for s in room.streams] == ["u1", "u2"]
        assert room.streams[1].closed_reason == "open" and len(room.streams[1].chunks) == 1
    finally:
        room.release("write")
        backend.stop_all()


# ---- P1-2: playback events are matched to the utterance they belong to -------------------


def manual_backend(**kw):
    backend, room, rest = make(**{"playback_margin_s": 0.4, **kw})
    room.auto = False
    sid = backend.start(StartOptions()).session_id
    return backend, room, sid


@pytest.fixture
def manual():
    backend, room, sid = manual_backend()
    yield backend, room, sid
    backend.stop_all()


def finished(room, backend, **payload):
    room.emit(backend._ensure_loop(), "lk.playback_finished", json.dumps(payload))


def started(room, backend, **payload):
    room.emit(backend._ensure_loop(), "lk.playback_started", json.dumps(payload))


def test_finish_with_another_utterance_id_never_satisfies_the_waiting_one(manual):
    backend, room, sid = manual
    t, box = bg(backend.stream_audio, sid, win("new", 0, final=True))
    assert until(lambda: room.streams and room.streams[0].closed_reason is None)
    finished(room, backend, utterance_id="old", interrupted=False)
    join(t)
    assert "error" not in box
    assert backend.playback_info(sid, "new")["playback_unconfirmed"] is True


def test_finish_before_the_stream_is_closed_is_dropped(manual):
    backend, room, sid = manual
    backend.stream_audio(sid, win("u1", 0))  # open, written, not final
    finished(room, backend, interrupted=False)  # stale completion lands mid-utterance
    t, box = bg(backend.stream_audio, sid, win("u1", 1, final=True))
    join(t)
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is True


def test_interrupted_finish_never_completes_a_live_utterance(manual):
    backend, room, sid = manual
    t, box = bg(backend.stream_audio, sid, win("u1", 0, final=True))
    assert until(lambda: room.streams and room.streams[0].closed_reason is None)
    finished(room, backend, interrupted=True)
    join(t)
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is True


def test_late_finish_of_a_timed_out_utterance_is_not_credited_to_the_next(manual):
    backend, room, sid = manual
    backend.stream_audio(sid, win("u1", 0, final=True))  # no finish: times out
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is True
    t, box = bg(backend.stream_audio, sid, win("u2", 0, final=True))
    assert until(lambda: len(room.streams) == 2 and room.streams[1].closed_reason is None)
    finished(room, backend, interrupted=False)  # u1's late completion
    assert not channel(backend, sid)._by_id["u2"].done.is_set()  # u2 still waits for its own
    finished(room, backend, interrupted=False)
    join(t)
    assert backend.playback_info(sid, "u2")["playback_unconfirmed"] is False


def test_started_for_another_utterance_does_not_stamp_the_current_one(manual):
    backend, room, sid = manual
    backend.stream_audio(sid, win("u1", 0))
    started(room, backend, utterance_id="old")
    assert backend.playback_info(sid, "u1")["playback_started_at"] is None
    started(room, backend, utterance_id="u1")
    assert backend.playback_info(sid, "u1")["playback_started_at"] is not None


def test_started_before_the_stream_is_opened_stamps_nothing(manual):
    backend, room, sid = manual
    room.hold("open")
    t, box = bg(backend.stream_audio, sid, win("u1", 0))
    assert room.wait_hit("open")
    started(room, backend)
    room.release("open")
    join(t)
    assert backend.playback_info(sid, "u1")["playback_started_at"] is None


def test_late_idless_started_of_an_interrupted_utterance_does_not_stamp_the_next(manual):
    backend, room, sid = manual
    backend.stream_audio(sid, win("u1", 0))  # written, avatar never reported started
    backend.interrupt(sid)
    backend.stream_audio(sid, win("u2", 0))
    started(room, backend)  # u1's delayed event, no id on the wire
    assert backend.playback_info(sid, "u2")["playback_started_at"] is None  # absorbed, not credited
    started(room, backend)  # one event per interrupted utterance; this one is u2's own
    assert backend.playback_info(sid, "u2")["playback_started_at"] is not None


def test_late_idless_finish_of_an_interrupted_utterance_is_not_credited_to_the_next(manual):
    backend, room, sid = manual
    t1, b1 = bg(backend.stream_audio, sid, win("u1", 0, final=True))
    assert until(lambda: room.streams and room.streams[0].closed_reason is None)
    backend.interrupt(sid)
    join(t1)
    t2, b2 = bg(backend.stream_audio, sid, win("u2", 0, final=True))
    assert until(lambda: len(room.streams) == 2 and room.streams[1].closed_reason is None)
    finished(room, backend, interrupted=False)  # u1's late completion
    assert not channel(backend, sid)._by_id["u2"].done.is_set()
    finished(room, backend, interrupted=False)
    join(t2)
    assert backend.playback_info(sid, "u2")["playback_unconfirmed"] is False


def test_unknown_explicit_id_never_confirms_a_clear_buffer(caplog):
    backend, room, _ = make(mode="silent")
    sid = backend.start(StartOptions()).session_id
    loop = backend._ensure_loop()
    ch = channel(backend, sid)
    try:
        with caplog.at_level(logging.WARNING):
            fut = asyncio.run_coroutine_threadsafe(ch.clear_buffer(timeout_s=0.4), loop)
            assert until(lambda: ch._clearing is not None)
            finished(room, backend, interrupted=True, utterance_id="somebody-else")
            fut.result(5)
        assert "clear_buffer not confirmed" in caplog.text
    finally:
        backend.stop_all()


def test_finish_arriving_during_aclose_is_not_lost(manual):
    backend, room, sid = manual
    room.hold("close")
    t, box = bg(backend.stream_audio, sid, win("u1", 0, final=True))
    assert room.wait_hit("close")
    finished(room, backend, interrupted=False)  # lands while aclose() is still pending
    room.release("close")
    join(t)
    assert "error" not in box
    assert backend.playback_info(sid, "u1")["playback_unconfirmed"] is False


def test_records_are_capped_on_insert_not_only_during_clear():
    backend, room, sid = manual_backend(history=3, playback_margin_s=0.02)
    try:
        for i in range(8):  # eight utterances whose finish never arrives
            backend.stream_audio(sid, win(f"u{i}", 0, final=True))
        ch = channel(backend, sid)
        assert len(ch._plays) <= 3 and len(ch._by_id) <= 3
    finally:
        backend.stop_all()


# ---- P1-3 / P1-4: interrupt reaches the fallback track; never two audio publishers -------


def fallback_backend(**kw):
    return make(
        mode="video_only", fallback_publish=True, audio_probe_s=0.05, render_offset_ms=300, **kw
    )


def test_pcm_sleeping_for_its_render_offset_is_not_pushed_after_interrupt():
    backend, room, _ = fallback_backend()
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("u1", 0))
        backend.interrupt(sid)
        time.sleep(0.5)  # longer than the 300 ms offset: the negative must outlive the sleep
        assert room.pushed == [] and room.queue_cleared >= 1
    finally:
        backend.stop_all()


def test_fallback_pcm_is_pushed_after_the_offset_when_not_interrupted():
    backend, room, _ = fallback_backend()
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("u1", 0))
        assert until(lambda: room.pushed, 3)
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
        assert room.streams == [] and room.pushed == [] and room.unpublished
    finally:
        backend.stop_all()


def test_avatar_audio_published_during_a_gated_write_pushes_nothing_and_fails_closed():
    backend, room, _ = fallback_backend()
    sid = backend.start(StartOptions()).session_id
    try:
        room.hold("write")
        t, box = bg(backend.stream_audio, sid, win("u1", 0))
        assert room.wait_hit("write")
        room.add_avatar_audio()  # the avatar starts publishing audio while the write is pending
        room.release("write")
        join(t)
        assert isinstance(box["error"], AvatarAudioFallbackRefused)
        assert until(lambda: room.unpublished)
        with pytest.raises(AvatarAudioFallbackRefused):  # the session fails closed
            backend.stream_audio(sid, win("u2", 0))
        assert room.pushed == [] and room.streams[-1].attributes["livento.utterance_id"] == "u1"
    finally:
        room.release("write")
        backend.stop_all()


def test_queued_fallback_pcm_is_vetoed_when_avatar_audio_appears_and_track_is_unpublished():
    backend, room, _ = fallback_backend()
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("u1", 0))  # queued, sleeping for its offset
        room.add_avatar_audio()
        assert until(lambda: room.unpublished, 5)
        assert room.pushed == []
        with pytest.raises(AvatarAudioFallbackRefused):
            backend.stream_audio(sid, win("u2", 0))
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


# ---- P2: lifecycle, deadlines, redaction -------------------------------------------------


def test_start_timeout_cancels_startup_and_ends_a_session_created_late():
    backend, room, rest = make(terminate_path="/sessions/{session_id}/terminate")
    release = rest.hold()
    with pytest.raises(LemonSliceError) as err:
        backend._run(backend._start("agent-preset"), 0.3)
    assert err.value.code == "timeout" and err.value.__context__ is None
    assert rest.parked.is_set()
    threading.Timer(0.1, release.set).start()  # the lost REST response finally lands
    assert until(lambda: rest.calls[-1][0].endswith("/sessions/ls-1/terminate"), 8)
    assert until(lambda: room.disconnected, 5)
    assert backend._sessions == {}
    backend.stop_all()


def test_stop_all_during_a_pending_start_terminates_the_late_session_before_closing_the_loop():
    backend, room, rest = make(terminate_path="/sessions/{session_id}/terminate")
    release = rest.hold()
    t_start, box = bg(backend.start, StartOptions())
    assert rest.parked.wait(5)
    thread = backend._thread
    threading.Timer(0.2, release.set).start()
    backend.stop_all()  # must wait for the late session, then stop the loop
    join(t_start)
    assert rest.calls[-1][0].endswith("/sessions/ls-1/terminate")
    assert room.disconnected and not thread.is_alive() and backend._loop is None
    assert "error" in box and backend._sessions == {}


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
    room_ref = [None]

    def post(url, headers, body, timeout):
        calls.append(url)
        if "terminate" in url:
            raise ConnectionError(f"{url} X-API-Key={LS_KEY}")
        room_ref[0].avatar_joins()
        return 200, {"session_id": "ls-9"}

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


def chain(exc):
    seen = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        exc = exc.__cause__ or exc.__context__
    return seen


def test_unexpected_start_error_is_redacted_and_has_no_cause_or_context(caplog):
    backend, room, _ = make()

    async def boom(url, token):
        raise RuntimeError(f"cannot reach {url} with {token} {LS_KEY} {LK_SECRET}")

    room.connect = boom
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LemonSliceError) as err:
            backend.start(StartOptions())
    shown = str(err.value) + repr(err.value) + caplog.text
    assert err.value.code == "start_failed"
    assert err.value.__cause__ is None and err.value.__context__ is None
    assert LS_KEY not in shown and LK_SECRET not in shown and "wss://lk.example.test" not in shown
    backend.stop_all()


def test_unexpected_stream_error_aborts_the_stream_and_is_sanitized_without_context(be):
    backend, room, _, sid = be
    room.fail["write"] = RuntimeError(f"socket to wss://lk.example.test failed {LS_KEY}")
    with pytest.raises(AvatarStreamError) as err:
        backend.stream_audio(sid, win("u1", 0))
    assert err.value.__cause__ is None and err.value.__context__ is None
    assert LS_KEY not in str(err.value) and "RuntimeError" in str(err.value)
    assert room.streams[0].closed_reason == "interrupted"
    backend.stream_audio(sid, win("u2", 0))  # the channel recovered
    assert room.streams[-1].attributes["livento.utterance_id"] == "u2"


def test_cancelled_send_closes_the_open_stream(be):
    backend, room, _, sid = be
    room.hold("write")
    with pytest.raises(LemonSliceError):
        backend._run(backend._stream(backend._sessions[sid], win("u1", 0)), 0.3)  # caller gives up
    assert until(lambda: room.streams[0].closed_reason == "interrupted")


def test_sample_rate_change_inside_one_utterance_is_rejected_not_silently_ignored(be):
    backend, room, _, sid = be
    backend.stream_audio(sid, win("u1", 0, ms=100, rate=16000))
    with pytest.raises(AvatarProtocolError):
        backend.stream_audio(sid, win("u1", 1, ms=100, rate=8000))
    assert room.streams[0].closed_reason == "interrupted"
    backend.stream_audio(sid, win("u2", 0, ms=100, rate=8000, final=True))  # next utterance is free
    assert abs(len(room.streams[1].pcm) / 2 / 16000 - 0.1) <= 0.02


def test_playback_history_is_bounded():
    backend, room, _ = make(history=3)
    sid = backend.start(StartOptions()).session_id
    for i in range(8):
        backend.stream_audio(sid, win(f"u{i}", 0, final=True))
    assert len(channel(backend, sid).playback_started_at) <= 3
    backend.stop_all()


# ---- resampling edge cases ---------------------------------------------------------------


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
