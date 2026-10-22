"""Offline regressions for the independent runtime review (R1-R10)."""

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from backend.application.reducer import AcceptedComment, FastReducer, FastReducerConfig
from backend.application.clients.tts.self_hosted import SelfHostedTTSClient, TTSClientError
from . import test_approved_speech_active as speech_tests
from .test_approved_speech_active import decision, prepare

case_factory = speech_tests.case_factory


@pytest.mark.asyncio
async def test_qa_provider_outage_deflects_then_script_continues(case_factory):
    case = await case_factory()

    def unavailable(*args, **kwargs):
        raise ConnectionError("provider unavailable")

    case.llm.stream_chunks = unavailable
    turn = decision(case, generated=True)
    await prepare(case, turn)
    assert "chưa có thông tin" in turn.prepared_script
    assert await case.d.coordinator._maybe_speak(case.sid, turn)
    script = decision(case)
    assert await case.d.coordinator._maybe_speak(case.sid, script)
    assert case.tts.calls


@pytest.mark.asyncio
async def test_stop_releases_speech_gate_and_fences_old_speech(case_factory):
    from backend.application.script_authoring.approved_speech import SpeechRejected

    case = await case_factory()
    gate, sid = case.d.approved_speech, case.sid
    speech = await gate.prepare(sid, case.source.version.spoken_text, route="director")
    case.d.coordinator.stop(sid)
    assert sid not in gate._pinned
    assert sid not in gate._epochs
    with pytest.raises(SpeechRejected):
        gate.check_live(speech, lambda: True)
    # Reusing an id must never make an old worker valid again.
    await gate.resolve(sid)
    with pytest.raises(SpeechRejected):
        gate.check_live(speech, lambda: True)


@pytest.mark.asyncio
async def test_resume_uses_reducer_original_comment_times(case_factory):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    ds = case.d.director.get_session(sid)
    turn = decision(case, generated=True)
    co.set_reducer_mode(sid, True)
    turn.occurred_at_bounds = (time.time() - 2, time.time() - 1)
    assert not ds.director.state.rolling_comments
    assert not co._hard_expired(sid, turn)
    turn.occurred_at_bounds = (time.time() - 200, time.time() - 150)
    assert co._hard_expired(sid, turn)


@pytest.mark.asyncio
async def test_no_tts_windows_does_not_complete_turn(case_factory):
    case = await case_factory()
    case.tts.stream_audio = lambda *a, **kw: iter(())
    turn = decision(case)
    await case.d.coordinator._maybe_speak(case.sid, turn)
    assert case.d.coordinator._completed_speech[case.sid]["state"] == "failed"
    assert not turn.completed_at


@pytest.mark.asyncio
async def test_unconfirmed_playback_recovers_after_bounded_wait_without_replay(case_factory):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    co.UNCONFIRMED_GRACE_S = 0.05
    interrupts = []
    case.backend.interrupt = lambda s: interrupts.append(s)
    case.backend.playback_info = lambda *a: {"playback_unconfirmed": True}
    turn = decision(case)
    await co._maybe_speak(sid, turn)
    assert co._completed_speech[sid]["state"] == "playback_unconfirmed"
    assert not turn.completed_at
    assert co._frozen(sid)
    await co._recovery_tasks[sid]
    assert interrupts == [sid] and not co._frozen(sid)  # buffer drained, then unlocked
    case.backend.playback_info = lambda *a: {"playback_unconfirmed": False}
    count = len(case.tts.calls)
    assert await co._maybe_speak(sid, decision(case))
    assert len(case.tts.calls) > count and co._unconfirmed_streak.get(sid) is None


@pytest.mark.asyncio
async def test_late_completion_unlocks_and_resets_streak(case_factory):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    co.UNCONFIRMED_GRACE_S = 1.0
    state = {"unconfirmed": True}
    case.backend.playback_info = lambda *a: {"playback_unconfirmed": state["unconfirmed"]}
    interrupts = []
    case.backend.interrupt = lambda s: interrupts.append(s)
    await co._maybe_speak(sid, decision(case))
    assert co._frozen(sid) and co._unconfirmed_streak[sid] == 1
    state["unconfirmed"] = False  # completion arrives late
    await co._recovery_tasks[sid]
    assert not co._frozen(sid) and sid not in co._unconfirmed_streak and not interrupts


@pytest.mark.asyncio
async def test_repeated_unconfirmed_is_severe_and_resume_unlocks(case_factory):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    co.UNCONFIRMED_GRACE_S = 0.02
    failed = []
    co.runtime_failures = SimpleNamespace(fail=lambda *a: failed.append(a), tick=lambda *a: None)
    case.backend.interrupt = lambda s: None
    case.backend.playback_info = lambda *a: {"playback_unconfirmed": True}
    for n in range(1, 4):
        co._uncertain_playback.pop(sid, None)
        await co._maybe_speak(sid, decision(case))
        await co._recovery_tasks[sid]
        assert bool(failed) == (n == 3)
    assert co._frozen(sid)
    co.resume(sid)
    assert not co._frozen(sid) and sid not in co._unconfirmed_streak


@pytest.mark.asyncio
async def test_stop_calls_provider_even_when_store_lock_fails(monkeypatch):
    from backend.api.v1 import execution
    from backend.api.v1.sessions import stop_session_internal
    from .test_avatar_lifecycle import container

    calls = []
    d = container(SimpleNamespace(stop=lambda sid: calls.append(sid)))

    def broken(*a, **kw):
        raise ConnectionError("Redis down")

    monkeypatch.setattr(execution, "_locked", broken)
    with pytest.raises(ConnectionError):
        await stop_session_internal(d, "s")
    assert calls == ["s"]


@pytest.mark.asyncio
async def test_prepare_rolls_back_after_metadata_write_failure(monkeypatch):
    from backend.api.v1 import sessions
    from backend.application.render.engines_base import StartResult
    from .test_avatar_lifecycle import container

    calls = []
    d = container(
        SimpleNamespace(
            start=lambda _: StartResult("s", "", ""), stop=lambda sid: calls.append(sid)
        )
    )
    d.config = SimpleNamespace()

    async def broken(*a, **kw):
        raise ConnectionError("Redis down")

    d.store.set = broken
    monkeypatch.setattr(sessions, "_container", lambda _: d)
    with pytest.raises(ConnectionError):
        await sessions.sessions_start(sessions.router.StartReq(), SimpleNamespace())
    assert calls == ["s"]


class Embedder:
    def encode(self, texts):
        if "broken" in texts:
            raise RuntimeError("embedding unavailable")
        return [[1.0, 0.0] for _ in texts]


def comment(cid, ts=1000.0):
    return AcceptedComment(cid, cid, cid, ts)


@pytest.mark.asyncio
async def test_embedding_failure_keeps_batch_for_retry():
    reducer = FastReducer(embedder=Embedder(), now_fn=lambda: 1001)
    reducer.notify_new_events("bad", comment("broken"))
    with pytest.raises(RuntimeError):
        await reducer.run_once("bad", 1001)
    assert reducer.pending_count("bad") == 1


@pytest.mark.asyncio
async def test_reducer_isolates_failed_session_and_bounds_retries():
    reducer = FastReducer(embedder=Embedder(), now_fn=lambda: 1001)
    reducer.notify_new_events("bad", comment("broken"))
    reducer.notify_new_events("good", comment("good"))
    task = asyncio.create_task(reducer.run_loop())
    try:
        for _ in range(100):
            if reducer.demand_snapshot("good", 1001):
                break
            await asyncio.sleep(0.01)
        assert not task.done()
        assert reducer.demand_snapshot("good", 1001)
        assert reducer.pending_count("bad") == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_embedding_cache_pruned_to_horizon_and_hard_cap():
    reducer = FastReducer(FastReducerConfig(rolling_horizon_sec=2, max_pending=3), Embedder())
    for i in range(10):
        reducer.notify_new_events("s", comment(str(i), 1000 + i))
        await reducer.run_once("s", 1000 + i)
    assert set(reducer._sessions["s"].cache) == {"7", "8", "9"}
    # A flood inside one horizon is bounded too, not just expired comments.
    for i in range(20):
        reducer.notify_new_events("s", comment(f"flood-{i}", 1010))
        await reducer.run_once("s", 1010)
    assert len(reducer._sessions["s"].cache) <= 3


@pytest.mark.asyncio
async def test_reducer_backs_off_to_a_slow_retry_and_retains_failed_batch():
    now = [1001.0]
    calls = []

    class Broken:
        def encode(self, texts):
            calls.append(texts)
            raise RuntimeError("down")

    reducer = FastReducer(embedder=Broken(), now_fn=lambda: now[0])
    reducer.notify_new_events("s", comment("broken"))
    task = asyncio.create_task(reducer.run_loop())
    try:
        for attempt in range(3):
            for _ in range(100):
                if reducer._sessions["s"].failures == attempt + 1:
                    break
                await asyncio.sleep(0.005)
            assert reducer._sessions["s"].failures == attempt + 1
            now[0] += 10
            reducer.notify_new_events("s")
        await asyncio.sleep(0.03)
        assert len(calls) == 3  # third failure: 60s backoff, no hot loop
        assert reducer.pending_count("s") == 1
        now[0] += 61  # no new comment needed: the time-based retry fires
        reducer.notify_new_events("s")
        for _ in range(100):
            if len(calls) == 4:
                break
            await asyncio.sleep(0.005)
        assert len(calls) == 4 and reducer.pending_count("s") == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_reducer_supervisor_restarts_failed_worker():
    from backend.bootstrap.lifespan import _start_reducer_loop, _stop_reducer_loop

    calls = []
    restarted = asyncio.Event()

    async def worker():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("unexpected worker failure")
        restarted.set()
        await asyncio.Event().wait()

    container = SimpleNamespace(reducer=SimpleNamespace(run_loop=worker))
    _start_reducer_loop(container)
    try:
        await asyncio.wait_for(restarted.wait(), 2)
        assert len(calls) == 2
    finally:
        await _stop_reducer_loop(container)


@pytest.mark.parametrize("pcm", [b"", b"x"])
def test_tts_rejects_empty_or_unaligned_pcm(pcm):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=pcm)))
    tts = SelfHostedTTSClient(base_url="http://tts", http_client=client)
    with pytest.raises(TTSClientError):
        tts.synthesize("Xin chào")
    client.close()


@pytest.mark.parametrize("mode", ["heartbeat", "content", "blocked"])
def test_llm_heartbeat_or_endless_content_has_absolute_deadline(mode):
    import threading
    from backend.application.clients.llm import OpenAICompatibleClient, ChatRequest, LLMClientError

    closed = threading.Event()

    class Endless(httpx.SyncByteStream):
        def __iter__(self):
            until = time.monotonic() + 0.5
            if mode == "blocked":
                closed.wait(0.5)
            while time.monotonic() < until and not closed.wait(0.005):
                yield (
                    b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
                    if mode == "content"
                    else b": heartbeat\n\n"
                )

        def close(self):
            closed.set()

    client = httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=Endless()))
    )
    llm = OpenAICompatibleClient(base_url="http://llm", http_client=client)
    llm._total_timeout, llm._first_content_timeout = 0.12, 0.04
    start = time.monotonic()
    with pytest.raises(LLMClientError, match="deadline"):
        list(llm.chat_stream(ChatRequest(messages=[])))
    assert closed.is_set()
    assert time.monotonic() - start < 1
    client.close()


@pytest.mark.asyncio
async def test_lifecycle_disconnects_without_redis_and_keeps_retry(monkeypatch):
    from backend.api.v1 import execution
    from backend.application.avatar_lifecycle import AvatarLifecycle
    from backend.application.render.engines_base import StartOptions
    from .test_avatar_lifecycle import container
    from .test_lemonslice_backend import make

    backend, room, _ = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    d = container(backend)
    await d.store.set(sid, {"status": "active"})
    backend._sessions[sid].created_at -= 2000
    service = AvatarLifecycle(d)
    original = execution._locked

    def broken(*a, **kw):
        raise ConnectionError("Redis down")

    monkeypatch.setattr(execution, "_locked", broken)
    try:
        with pytest.raises(ConnectionError):
            await service.check(sid)
        assert room.disconnected
        assert sid in service.pending and sid in d.media_cleanup_pending
        monkeypatch.setattr(execution, "_locked", original)
        await service.check(sid)
        assert not service.pending and not d.media_cleanup_pending
        assert await d.store.get(sid) is None
    finally:
        backend.stop_all()


@pytest.mark.asyncio
async def test_media_cleanup_obligation_survives_terminal_record_completion():
    from backend.api.v1.sessions import stop_session_internal
    from backend.application.avatar_lifecycle import AvatarLifecycle
    from backend.application.terminal_outcomes import TerminalOutcomes
    from .test_avatar_lifecycle import container
    from .test_terminal_outcomes import NOW, FakePg, hot

    calls, fail = [], [True]

    def stop(sid):
        calls.append(sid)
        if fail[0]:
            raise RuntimeError("provider down")

    d = container(
        SimpleNamespace(stop=stop, session_ids=lambda: (), set_session_phase=lambda *a: None)
    )
    d.terminal_outcomes = TerminalOutcomes(FakePg(), clock=lambda: NOW)
    await d.store.set("s", hot("ending"))
    for _ in range(5):  # past the 3-attempt terminal budget
        try:
            await stop_session_internal(d, "s")
        except Exception:
            pass
    assert d.media_cleanup_pending["s"]["stopped"] is False
    fail[0] = False
    service = AvatarLifecycle(d)
    await service.check("s")
    assert "s" not in d.media_cleanup_pending and "s" not in service.pending
    assert calls[-1] == "s" and len(calls) >= 6


def test_released_deadline_never_shuts_down_a_pooled_connection():
    from backend.application.clients.llm.stream_deadline import StreamDeadline

    shut = []
    sock = SimpleNamespace(shutdown=lambda how: shut.append(how))
    deadline = StreamDeadline(0.01, 0.01)
    deadline.sock = sock
    deadline.release()  # connection about to return to the pool
    time.sleep(0.03)
    deadline._interrupt()
    assert not shut and deadline.sock is None


def test_first_content_deadline_bounds_headers_wait_on_reused_connection():
    import socket
    import threading
    from backend.application.clients.llm import OpenAICompatibleClient, ChatRequest, LLMClientError

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    release = threading.Event()

    def serve():
        conn, _ = srv.accept()
        body = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n'
        for n in range(2):
            conn.recv(65536)
            if n == 1:
                release.wait(10)  # headers never come on the reused connection
                break
            conn.sendall(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                + b"Content-Length: %d\r\n\r\n" % len(body)
                + body
            )
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    client = httpx.Client()
    llm = OpenAICompatibleClient(
        base_url=f"http://127.0.0.1:{srv.getsockname()[1]}", http_client=client, timeout=30
    )
    llm._total_timeout, llm._first_content_timeout = 1.0, 0.3
    assert list(llm.chat_stream(ChatRequest(messages=[]))) == ["hi"]
    start = time.monotonic()
    with pytest.raises(LLMClientError):
        list(llm.chat_stream(ChatRequest(messages=[])))
    assert time.monotonic() - start < 2
    release.set()
    client.close()
    srv.close()


@pytest.mark.asyncio
async def test_provenance_survives_prune_for_live_cluster_and_is_bounded_in_outage():
    reducer = FastReducer(embedder=Embedder(), now_fn=lambda: 1000.0)
    reducer.notify_new_events("s", comment("q1"))
    await reducer.run_once("s", 1001.0)
    for i in range(200):  # many demand rows after q1: demand keeps only the newest max_pending
        reducer.notify_new_events("s", comment(f"x{i}"))
    state = reducer._sessions["s"]
    state.demand = []  # q1 left the demand window but stays a live cluster member
    reducer._prune(state, "s")
    assert reducer.comment_provenance("s", "q1") is not None
    broken = FastReducer(embedder=Embedder(), now_fn=lambda: 1000.0)
    cap = broken._config.max_pending
    for i in range(cap * 6):  # embedding outage: nothing is ever processed
        broken.notify_new_events("o", comment(f"id{i}"))
    assert len(broken._sessions["o"].provenance) <= 4 * cap + 1


@pytest.mark.asyncio
async def test_empty_audio_is_retried_before_ending_the_session(case_factory):
    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    failed = []
    co.runtime_failures = SimpleNamespace(fail=lambda *a: failed.append(a), tick=lambda *a: None)
    case.tts.stream_audio = lambda *a, **kw: iter(())
    for n in range(1, 4):
        turn = decision(case)
        await co._maybe_speak(sid, turn)
        assert not turn.completed_at
        assert bool(failed) == (n == 3)
