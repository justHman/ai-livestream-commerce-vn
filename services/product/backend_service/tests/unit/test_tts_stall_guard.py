"""Bounded HTTP header/body reads against a loopback socket; no TTS service required."""

from __future__ import annotations

import asyncio
import logging
import socketserver
import threading
import time
from contextlib import contextmanager

import httpx
import pytest

from backend.application.clients.tts import self_hosted as tts

SECRET_TEXT = "private scripted speech"
SECRET_TOKEN = "private-bearer-token"


@pytest.fixture(autouse=True)
def deadlines(monkeypatch):
    monkeypatch.setenv("TTS_API_STYLE", "openai_audio_speech")
    monkeypatch.delenv("TTS_TEMPO", raising=False)
    monkeypatch.setattr(tts, "FIRST_BYTE_DEADLINE_S", 0.4)
    monkeypatch.setattr(tts, "TOTAL_DEADLINE_MIN_S", 0.6)
    monkeypatch.setattr(tts, "TOTAL_DEADLINE_MAX_S", 0.6)
    monkeypatch.setattr(tts, "_RETRY_DELAYS_S", (0.02, 0.03))


@contextmanager
def stalled_endpoint(mode, *, recover=False, request_started=None):
    stop = threading.Event()
    connections = []
    finished = []
    chunk_counts = []
    lock = threading.Lock()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            self.request.settimeout(3)
            data = b""
            while b"\r\n\r\n" not in data:
                data += self.request.recv(4096)
            headers, body = data.split(b"\r\n\r\n", 1)
            length = int(
                next(
                    h.split(b":", 1)[1]
                    for h in headers.split(b"\r\n")
                    if h.lower().startswith(b"content-length:")
                )
            )
            while len(body) < length:
                body += self.request.recv(4096)
            with lock:
                index = len(connections)
                connections.append(self.request)  # Keep identity alive (no id/port reuse).
                done = threading.Event()
                finished.append(done)
                chunk_counts.append(0)
            if request_started is not None:
                request_started.set()
            try:
                if recover and index > 0:
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n\x01\x00")
                    return
                if mode == "silent-connect":
                    assert self.request.recv(1) == b""
                    return
                if mode == "header-trickle":
                    self.request.sendall(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                    while not stop.wait(0.025):
                        self.request.sendall(b"x")
                        chunk_counts[index] += 1
                    return
                self.request.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                if mode != "silent":
                    self.request.sendall(b"2\r\n\x01\x00\r\n")
                    chunk_counts[index] += 1
                if mode == "trickle":
                    # This interval models the sender's pacing, not test synchronization.
                    while not stop.wait(0.025):
                        self.request.sendall(b"2\r\n\x01\x00\r\n")
                        chunk_counts[index] += 1
                else:
                    # EOF is the handshake proving the failed attempt closed its connection.
                    assert self.request.recv(1) == b""
            except (OSError, AssertionError):
                pass
            finally:
                done.set()

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    server = Server(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02})
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", connections, finished, chunk_counts
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        worker.join(3)
        assert not worker.is_alive()
        assert all(done.wait(3) for done in finished)


@pytest.mark.parametrize(
    "mode", ["silent", "unfinished", "trickle", "header-trickle", "silent-connect"]
)
@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_stalled_request_is_bounded_and_retries_a_fresh_connection(mode, recover, owned, caplog):
    with stalled_endpoint(mode, recover=recover) as (url, connections, finished, counts):
        # Exercise an injected real pool too: retries must not reuse its failed connection.
        with httpx.Client() as http_client:
            client = tts.SelfHostedTTSClient(
                url, api_key=SECRET_TOKEN, http_client=None if owned else http_client
            )
            started = time.monotonic()
            with caplog.at_level(logging.WARNING, logger=tts.__name__):
                if recover:
                    assert client.synthesize(SECRET_TEXT).pcm16 == b"\x01\x00"
                else:
                    with pytest.raises(tts.TTSClientError):
                        client.synthesize(SECRET_TEXT)
            elapsed = time.monotonic() - started
        attempts = 2 if recover else 3
        assert len(connections) == attempts
        assert len({id(conn) for conn in connections}) == attempts
        assert all(done.wait(3) for done in finished)
        assert elapsed < 4.0  # Generous scheduling margin, much less than an unbounded read.
        if mode in ("trickle", "header-trickle"):
            assert counts[0] >= 2
            assert elapsed >= (0.6 if mode == "trickle" else 0.4)
        warnings = [
            r for r in caplog.records if r.name == tts.__name__ and r.levelno == logging.WARNING
        ]
        assert len(warnings) == attempts - 1
        for attempt, record in enumerate(warnings, 1):
            message = record.getMessage()
            assert f"attempt={attempt}" in message
            assert "elapsed_s=" in message
            assert f"input_chars={len(SECRET_TEXT)}" in message
            assert SECRET_TEXT not in message and SECRET_TOKEN not in message
            assert "Authorization" not in message and "Bearer" not in message
            assert record.exc_info is None


@pytest.mark.parametrize("failure", [429, 503, "timeout", "network"])
def test_transient_failure_then_success(failure, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.extensions["timeout"] == {
            "connect": 0.6,
            "read": 0.4,
            "write": 0.6,
            "pool": 0.6,
        }
        assert request.headers["connection"] == "close"
        if len(calls) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout(SECRET_TEXT + SECRET_TOKEN)
            if failure == "network":
                raise httpx.ConnectError(SECRET_TEXT + SECRET_TOKEN)
            return httpx.Response(failure, text=SECRET_TEXT + SECRET_TOKEN)
        return httpx.Response(200, content=b"ok")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        engine = tts.SelfHostedTTSClient("http://offline", http_client=http_client)
        with caplog.at_level(logging.WARNING):
            assert engine.synthesize(SECRET_TEXT).pcm16 == b"ok"
    assert len(calls) == 2
    assert SECRET_TEXT not in caplog.text and SECRET_TOKEN not in caplog.text


def test_non_transient_status_is_not_retried():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, text=SECRET_TEXT)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        engine = tts.SelfHostedTTSClient("http://offline", http_client=http_client)
        with pytest.raises(tts.TTSClientError, match="HTTP 400"):
            engine.synthesize(SECRET_TEXT)
    assert len(calls) == 1


def test_production_deadline_formula(monkeypatch):
    monkeypatch.setattr(tts, "TOTAL_DEADLINE_MIN_S", 12.0)
    monkeypatch.setattr(tts, "TOTAL_DEADLINE_MAX_S", 45.0)
    monkeypatch.setattr(tts, "_RETRY_DELAYS_S", (0.4, 1.2))
    assert tts.request_deadline_s(1) == 12.0
    assert tts.request_deadline_s(100) == 35.0
    assert tts.request_deadline_s(579) == 45.0
    assert tts.phrase_wait_budget_s(579) == 161.6


@pytest.mark.parametrize("send_first_chunk", [False, True])
def test_watchdog_interrupts_a_read_before_the_next_chunk(send_first_chunk, caplog):
    sockets = []
    streams = []

    class FakeSocket:
        def __init__(self):
            self.interrupted = threading.Event()
            sockets.append(self)

        def shutdown(self, _how):
            self.interrupted.set()

    class Stream(httpx.SyncByteStream):
        def __init__(self):
            self.sock = FakeSocket()
            self.closed = False
            streams.append(self)

        def get_extra_info(self, key):
            assert key == "socket"
            return self.sock

        def __iter__(self):
            if send_first_chunk:
                yield b"first"
            # Deliberately ignores httpx's read timeout. Only socket shutdown can wake this read.
            assert self.sock.interrupted.wait(3), "watchdog never interrupted the read"
            raise httpx.ReadError("interrupted")

        def close(self):
            self.closed = True

    def handler(request):
        stream = Stream()
        return httpx.Response(200, stream=stream, extensions={"network_stream": stream})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http_client:
        client = tts.SelfHostedTTSClient("http://offline", http_client=http_client)
        started = time.monotonic()
        with pytest.raises(tts.TTSClientError):
            client.synthesize(SECRET_TEXT)
        elapsed = time.monotonic() - started
    assert elapsed < 3
    assert elapsed >= (1.8 if send_first_chunk else 1.2)
    assert len(sockets) == 3
    assert all(sock.interrupted.is_set() for sock in sockets)
    assert all(stream.closed for stream in streams)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["header-trickle", "silent"])
@pytest.mark.parametrize("abandon", [False, True])
async def test_stalled_synthesis_worker_exits_and_starts_no_request_after_failure(
    mode, abandon, monkeypatch
):
    from backend.application.clients.composition import RemoteHttpTTSEngine
    from .test_verbatim_phrasing import LONG, _no_prefetch_threads, _orchestrator, _Playback

    if abandon:
        # Force the safety net during the first attempt. The worker must finish
        # that request by its deadline and suppress the two remaining retries.
        monkeypatch.setattr(tts, "FIRST_BYTE_DEADLINE_S", 0.6)
        monkeypatch.setattr(
            "backend.application.render.orchestrator.phrase_wait_budget_s", lambda _: 0.3
        )
    request_started = threading.Event()
    with stalled_endpoint(mode, request_started=request_started) as (url, connections, done, _):
        client = tts.SelfHostedTTSClient(url)  # Owned client: real transport and engine seam.
        backend = _Playback()
        orch = _orchestrator(RemoteHttpTTSEngine(client), backend)
        started = time.monotonic()
        task = asyncio.create_task(orch.speak_verbatim("stalled", LONG))
        try:
            assert await asyncio.to_thread(request_started.wait, 3)
            with pytest.raises(tts.TTSClientError):
                await asyncio.wait_for(asyncio.shield(task), 4)
            attempts = len(tts._RETRY_DELAYS_S) + 1
            count_at_failure = len(connections)
            if abandon:
                assert not done[0].is_set()  # The worker is genuinely still in the request.
                with pytest.raises(tts.TTSClientError, match="Previous TTS synthesis"):
                    await asyncio.wait_for(orch.speak_verbatim("next", LONG), 3)
            assert await _no_prefetch_threads(3)
            assert time.monotonic() - started < (
                tts.request_deadline_s(len(LONG)) * attempts + sum(tts._RETRY_DELAYS_S) + 2
            )
            assert len(connections) == count_at_failure == (1 if abandon else attempts)
            assert all(event.wait(3) for event in done)
            assert backend.windows == []  # No later phrase, fabricated final, or script success.
        finally:
            await asyncio.gather(task, return_exceptions=True)
            assert await _no_prefetch_threads(3)
            client.close()
