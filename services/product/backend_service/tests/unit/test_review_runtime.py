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
async def test_unconfirmed_playback_never_freezes_the_live(case_factory, caplog):
    from backend.application.render.orchestrator import StreamOrchestrator

    case = await case_factory()
    co, sid = case.d.coordinator, case.sid
    failed = []
    co.runtime_failures = SimpleNamespace(fail=lambda *a: failed.append(a), tick=lambda *a: None)
    case.backend.playback_info = lambda *a: {"playback_unconfirmed": True}
    before = StreamOrchestrator.playback_unconfirmed_total
    for _ in range(2):  # one and two unconfirmed utterances in a row
        turn = decision(case)
        assert await co._maybe_speak(sid, turn)
        assert co._completed_speech[sid]["state"] == "completed"
        assert not co._frozen(sid)
    assert not failed
    assert StreamOrchestrator.playback_unconfirmed_total == before + 2
    assert "playback unconfirmed" in caplog.text


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
async def test_poison_comment_is_bisected_and_quarantined_while_others_proceed():
    reducer = FastReducer(embedder=Embedder(), now_fn=lambda: 1001.0)
    for cid in ("ok1", "broken", "ok2", "ok3"):
        reducer.notify_new_events("s", comment(cid))
    state = reducer._sessions["s"]
    for _ in range(30):
        if not state.pending:
            break
        try:
            await reducer.run_once("s", 1001.0)
        except RuntimeError:
            reducer._note_failure(state, "s", 1001.0)
    assert not state.pending and state.quarantined == 1
    done = {d["comment_id"] for d in state.demand}
    assert done == {"ok1", "ok2", "ok3"}
    assert "broken" not in state.provenance and not state.item_failures


@pytest.mark.asyncio
async def test_inflight_batch_keeps_provenance_when_pending_overflows():
    holder = {}

    class Overflow:
        def encode(self, texts):
            for i in range(30):  # arrivals while encode runs push q1 out of pending
                holder["r"].notify_new_events("s", comment(f"n{i}"))
            return [[1.0, 0.0] for _ in texts]

    reducer = holder["r"] = FastReducer(
        embedder=Overflow(), config=FastReducerConfig(max_pending=5), now_fn=lambda: 1001.0
    )
    reducer.notify_new_events("s", comment("q1"))
    await reducer.run_once("s", 1001.0)
    assert reducer.comment_provenance("s", "q1") is not None


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


class _Server:
    """Tiny TCP HTTP server: handler(conn, n) is called once per request on a connection."""

    def __init__(self, handler):
        import socket
        import threading

        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.handler, self.stop = handler, threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.sock.getsockname()[1]}"

    def _run(self):
        import threading

        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            n = 0
            while self.handler(conn, n):
                n += 1
        except OSError:
            pass
        finally:
            try:  # drain the unread request body so close is a FIN, not an RST
                conn.shutdown(1)
                conn.settimeout(0.2)
                while conn.recv(65536):
                    pass
            except OSError:
                pass
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()


_SSE = b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n"
_TOKEN = b'data: {"choices":[{"delta":{"content":"x"}}]}\n\n'
_BAD = b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"


def _stream(server, total, first, timeout=30, usage=False):
    from backend.application.clients.llm import OpenAICompatibleClient, ChatRequest

    llm = OpenAICompatibleClient(base_url=server.url, timeout=timeout)
    llm._total_timeout, llm._first_content_timeout = total, first
    start = time.monotonic()
    try:
        return list(
            llm.chat_stream(ChatRequest(messages=[], include_usage=usage))
        ), time.monotonic() - start
    finally:
        llm.close()


@pytest.mark.parametrize("mode", ["heartbeat", "endless", "token_then_silence"])
def test_llm_stream_deadlines_without_watcher_threads(mode):
    from backend.application.clients.llm import LLMClientError

    def handler(conn, n):
        conn.recv(65536)
        conn.sendall(_SSE)
        if mode == "token_then_silence":
            conn.sendall(_TOKEN)
            time.sleep(2)
            return False
        for _ in range(100):
            conn.sendall(b": hb\n\n" if mode == "heartbeat" else _TOKEN)
            time.sleep(0.05)
        return False

    server = _Server(handler)
    try:
        start = time.monotonic()
        with pytest.raises(LLMClientError):
            _stream(server, total=0.6, first=0.3)
        # heartbeat: first-content check (~0.3s); endless: total check (~0.6s);
        # silence after a token: bounded by the per-request read timeout.
        assert time.monotonic() - start < 1.5
    finally:
        server.close()


def test_llm_retry_without_stream_options_gets_a_fresh_deadline():
    calls = []

    def handler(conn, n):
        conn.recv(65536)
        time.sleep(0.2)
        calls.append(1)
        if len(calls) == 1:
            conn.sendall(_BAD)
            return False
        conn.sendall(_SSE + _TOKEN + b"data: [DONE]\n\n")
        return False

    server = _Server(handler)
    try:
        # two 0.2s waits exceed one shared 0.3s first-content budget, not a per-attempt one
        out, _ = _stream(server, total=5, first=0.3, usage=True)
        assert out == ["x"]
    finally:
        server.close()


@pytest.mark.asyncio
async def test_run_loop_backoff_doubles_and_bisects_from_second_failure():
    now = [1001.0]

    class Broken:
        def encode(self, texts):
            raise RuntimeError("down")

    reducer = FastReducer(embedder=Broken(), now_fn=lambda: now[0])
    for cid in ("a", "b", "c", "d"):
        reducer.notify_new_events("s", comment(cid))
    state = reducer._sessions["s"]
    task = asyncio.create_task(reducer.run_loop())
    seen = []
    try:
        for attempt in range(1, 4):
            for _ in range(200):
                if state.failures == attempt:
                    break
                await asyncio.sleep(0.005)
            assert state.failures == attempt
            seen.append((state.retry_at - now[0], state.batch_limit))
            now[0] += 100
            reducer.notify_new_events("s")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert seen == [(1.0, None), (2.0, 2), (4.0, 1)]


@pytest.mark.parametrize("status", [401, 429, 500])
def test_llm_http_error_is_an_llm_client_error_with_body(status):
    from backend.application.clients.llm import LLMClientError

    def handler(conn, n):
        conn.recv(65536)
        body = b"nope"
        conn.sendall(
            b"HTTP/1.1 %d X\r\nContent-Length: 4\r\nConnection: close\r\n\r\n" % status + body
        )
        return False

    server = _Server(handler)
    try:
        with pytest.raises(LLMClientError, match=f"HTTP {status} nope"):
            _stream(server, total=5, first=5)
    finally:
        server.close()


@pytest.mark.asyncio
async def test_sweeper_does_not_fail_an_end_that_is_waiting_for_provider_stop():
    import threading
    from backend.api.v1.sessions import stop_session_internal
    from backend.application.avatar_lifecycle import AvatarLifecycle
    from backend.application.execution_contract import ExecutionState
    from .test_avatar_lifecycle import container

    entered, release, calls = threading.Event(), threading.Event(), []

    def stop(sid):
        calls.append(sid)
        entered.set()
        release.wait(5)

    d = container(
        SimpleNamespace(
            stop=stop,
            session_ids=lambda: (),
            set_session_phase=lambda *a: None,
            lifecycle_status=lambda *a: None,
        )
    )
    raw = ExecutionState(
        tenant_id="t",
        business_session_id="b",
        runtime_session_id="s",
        generation="g1",
        phase="ready",
        runtime_ready=True,
    )
    await d.store.set("s", {"execution_contract": raw.model_dump(mode="json")})
    end = asyncio.create_task(stop_session_internal(d, "s"))
    await asyncio.to_thread(entered.wait, 5)
    service = AvatarLifecycle(d)
    await service.check("s")  # sweeper runs while the End waits for stop
    meta = await d.store.get("s")
    assert meta["execution_contract"]["phase"] == "ready"
    assert "avatar_stop_reason" not in meta and "s" not in service.pending
    release.set()
    await end
    assert calls == ["s"] and "s" not in d.media_cleanup_pending
