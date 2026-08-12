"""Integration: speech route through the scheduler runtime (Change T 3.2/11.x).

The real provider is a system boundary; these tests build a FastAPI app whose
lifespan provider build is monkeypatched so ``app.state.runtime`` is a
``SchedulerRuntime`` over a deterministic fake provider. Following the
cluster-2 convention, app state is set INSIDE ``with TestClient(app)``
because the lifespan overwrites state set before it.

The runtime runs on a fake clock; a ticking thread advances it so HTTP
requests resolve without manual clock management, and the two tests that
need a big jump (deadline, overload) drive the clock explicitly from the
main thread while the blocking request runs in a worker thread.

Covered: runtime path audio (11.1), mixed-session isolation (11.6), chunk
identity across batches (11.7), overload 429, deadline 408, provider batch
failure 502 with continuation (11.3), fallback to legacy engine when the
runtime is not ready (11.1), and tracing headers (11.2).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np
from fastapi.testclient import TestClient

from tts import create_app
from tts.config import RuntimeConfig
from tts.engines.base import ToneEngine
from tts.providers.capabilities import ProviderCapabilities
from tts.providers.errors import ProviderInferenceError
from tts.providers.models import AudioResult, ProviderRequest, ProviderResult
from tts.scheduler.admission import AdmissionController
from tts.scheduler.fairness import FairnessSelector, PendingPopulation
from tts.scheduler.runtime import SchedulerRuntime

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self._now = NOW

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


def start_ticking(clock: FakeClock, tick: float = 0.05) -> threading.Thread:
    """Advance the fake clock every tick real-seconds, like a real clock."""

    def run() -> None:
        while True:
            time.sleep(tick)
            clock.advance(tick)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class FakeProvider:
    """Deterministic provider: waveform encodes text; records batch calls."""

    def __init__(
        self,
        *,
        fail_batch: bool = False,
        dispatch_delay: float = 0.0,
        max_batch_size: int = 8,
    ) -> None:
        self.fail_batch = fail_batch
        self.dispatch_delay = dispatch_delay
        self.max_batch_size = max_batch_size
        self.batch_calls: list[tuple[str, ...]] = []

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="fake",
            model_revision="fake-1",
            sample_rate_hz=48_000,
            supports_native_batch=True,
            max_batch_size=self.max_batch_size,
            supports_mixed_voice_batch=True,
            supported_styles=("natural",),
            supported_response_formats=("pcm", "wav"),
        )

    def batch_key(self, request: ProviderRequest):
        return "k"

    async def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        self.batch_calls.append(tuple(r.request_id for r in requests))
        if self.fail_batch:
            raise ProviderInferenceError("fake provider batch failed")
        if self.dispatch_delay:
            await asyncio_sleep(self.dispatch_delay)
        return [self._result(r) for r in requests]

    def _result(self, request: ProviderRequest) -> AudioResult:
        n = len(request.input_text) * 48 * 240
        return AudioResult(
            request_id=request.request_id,
            sample_rate=48_000,
            waveform=np.zeros(n, dtype=np.float32),
            response_format=request.response_format,
            duration_ms=240,
        )


async def asyncio_sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _make_runtime(clock: FakeClock, provider: FakeProvider, **config_overrides) -> SchedulerRuntime:
    overrides = dict(provider="fake", model_revision="fake-1", coalesce_window_ms=1)
    overrides.update(config_overrides)
    global_limit = overrides.pop("global_pending_limit", 512)
    session_limit = overrides.pop("per_session_pending_limit", 64)
    return SchedulerRuntime(
        population=PendingPopulation(),
        admission=AdmissionController(global_limit, session_limit),
        selector=FairnessSelector(),
        provider=provider,
        config=RuntimeConfig(**overrides),
        clock=clock.now,
    )


def _prepare_app(monkeypatch, *, provider=None):
    """Create the app with the lifespan provider build replaced by our fake."""
    if provider is not None:

        def build_provider(app):
            return provider

        monkeypatch.setattr("tts.bootstrap.lifespan._build_provider", build_provider)
        monkeypatch.setenv("TTS_PROVIDER", "fake")
    app = create_app()
    app.state.engine = ToneEngine.from_config({})
    app.state.engine_ready = True
    return app


def _payload(*, session_id=None, utterance_id=None, chunk_seq=0, **overrides) -> dict:
    payload = {
        "text": "Xin chào",
        "session_id": session_id,
        "utterance_id": utterance_id,
        "chunk_seq": chunk_seq,
        "response_format": "wav",
    }
    payload.update(overrides)
    return payload


# ── 11.1: runtime path serves audio from the provider result ──────────────────
def test_speech_through_runtime_returns_audio(monkeypatch) -> None:
    clock = FakeClock()
    provider = FakeProvider()
    app = _prepare_app(monkeypatch, provider=provider)
    start_ticking(clock)
    with TestClient(app) as client:
        app.state.runtime = _make_runtime(clock, provider)
        resp = client.post("/v1/speech", json=_payload())
    assert resp.status_code == 200
    assert resp.content[:4] == b"RIFF"
    assert resp.headers["x-audio-sample-rate"] == "48000"
    assert len(provider.batch_calls) == 1


# ── 11.6: mixed sessions — zero cross-route/duplicate/missing results ─────────
def test_mixed_sessions_zero_cross_route(monkeypatch) -> None:
    clock = FakeClock()
    provider = FakeProvider(dispatch_delay=0.01)
    app = _prepare_app(monkeypatch, provider=provider)
    start_ticking(clock)
    with TestClient(app) as client:
        app.state.runtime = _make_runtime(clock, provider)
        a0 = client.post(
            "/v1/audio/speech",
            json=_payload(session_id="sess-a", utterance_id="utt-a", chunk_seq=0),
        )
        b0 = client.post(
            "/v1/audio/speech",
            json=_payload(session_id="sess-b", utterance_id="utt-b", chunk_seq=0),
        )
        a1 = client.post(
            "/v1/audio/speech",
            json=_payload(session_id="sess-a", utterance_id="utt-a", chunk_seq=1),
        )
    assert a0.status_code == 200 and b0.status_code == 200 and a1.status_code == 200
    assert a0.headers["x-session-id"] == "sess-a" and a0.headers["x-chunk-seq"] == "0"
    assert b0.headers["x-session-id"] == "sess-b" and b0.headers["x-chunk-seq"] == "0"
    assert a1.headers["x-session-id"] == "sess-a" and a1.headers["x-chunk-seq"] == "1"
    # One response per request: no duplicates or missing accepted results.
    assert len(provider.batch_calls) >= 1
    dispatched = [r for call in provider.batch_calls for r in call]
    assert len(dispatched) == 3
    assert len(set(dispatched)) == 3  # request ids are unique per HTTP call


# ── 11.7: same-session concurrent chunks keep utterance/chunk identity ────────
def test_same_session_chunks_keep_identity_across_batches(monkeypatch) -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=1)
    app = _prepare_app(monkeypatch, provider=provider)
    start_ticking(clock)
    with TestClient(app) as client:
        app.state.runtime = _make_runtime(clock, provider)
        c0 = client.post(
            "/v1/audio/speech", json=_payload(session_id="s1", utterance_id="u1", chunk_seq=0)
        )
        c1 = client.post(
            "/v1/audio/speech", json=_payload(session_id="s1", utterance_id="u1", chunk_seq=1)
        )
    assert c0.status_code == 200 and c1.status_code == 200
    assert c0.headers["x-utterance-id"] == "u1" and c0.headers["x-chunk-seq"] == "0"
    assert c1.headers["x-utterance-id"] == "u1" and c1.headers["x-chunk-seq"] == "1"
    assert len(provider.batch_calls) == 2


# ── 11.1 fallback: runtime not ready -> legacy engine still serves ────────────
def test_speech_falls_back_to_engine_when_runtime_not_ready(monkeypatch) -> None:
    app = _prepare_app(monkeypatch)  # no provider, no runtime
    with TestClient(app) as client:
        resp = client.post("/v1/speech", json=_payload())
    assert resp.status_code == 200
    assert resp.headers["x-audio-engine"] == "tone"
    assert resp.headers["x-audio-sample-rate"] == "24000"


# ── 11.3: provider batch failure -> 502; later queued work still succeeds ─────
def test_provider_batch_failure_502_and_runtime_continues(monkeypatch) -> None:
    clock = FakeClock()
    provider = FakeProvider(fail_batch=True)
    app = _prepare_app(monkeypatch, provider=provider)
    start_ticking(clock)
    with TestClient(app) as client:
        app.state.runtime = _make_runtime(clock, provider)
        first = client.post("/v1/speech", json=_payload())
    assert first.status_code == 502
    assert first.json()["error"]["code"].startswith("provider_")
    provider.fail_batch = False
    with TestClient(app) as client:
        app.state.runtime = app.state.runtime or _make_runtime(clock, provider)
        second = client.post("/v1/speech", json=_payload())
    assert second.status_code == 200


# ── overload: 429 ─────────────────────────────────────────────────────────────
def test_overload_returns_429(monkeypatch) -> None:
    clock = FakeClock()
    provider = FakeProvider(dispatch_delay=1.0)  # first request stays in flight
    app = _prepare_app(monkeypatch, provider=provider)
    start_ticking(clock)
    results: list = []

    def post_first() -> None:
        results.append(client.post("/v1/speech", json=_payload(session_id="sess-ov")))

    with TestClient(app) as client:
        app.state.runtime = _make_runtime(
            clock, provider, global_pending_limit=1, per_session_pending_limit=64
        )
        worker = threading.Thread(target=post_first)
        worker.start()
        time.sleep(0.3)  # first request dispatched, holding the only slot
        resp = client.post("/v1/speech", json=_payload(session_id="sess-ov"))
        worker.join()
    assert resp.status_code == 429
    assert resp.json()["error"]["code"].startswith("provider_")
    assert results[0].status_code == 200


# ── deadline: 408 ─────────────────────────────────────────────────────────────
def test_deadline_exceeded_returns_408(monkeypatch) -> None:
    clock = FakeClock()
    provider = FakeProvider()
    app = _prepare_app(monkeypatch, provider=provider)
    results: list = []

    def post_first() -> None:
        results.append(
            client.post("/v1/speech", json=_payload(session_id="s-dl", utterance_id="u-dl"))
        )

    with TestClient(app) as client:
        app.state.runtime = _make_runtime(clock, provider, coalesce_window_ms=10_000)
        worker = threading.Thread(target=post_first)
        worker.start()
        time.sleep(0.3)  # request admitted, pending inside the window
        clock.advance(120)  # far past the 30 s request deadline
        worker.join()
    assert results[0].status_code == 408
    assert results[0].json()["error"]["code"].startswith("provider_")


# ── 11.2: tracing headers preserved on the runtime path ───────────────────────
def test_runtime_path_preserves_tracing_headers(monkeypatch) -> None:
    clock = FakeClock()
    provider = FakeProvider()
    app = _prepare_app(monkeypatch, provider=provider)
    start_ticking(clock)
    with TestClient(app) as client:
        app.state.runtime = _make_runtime(clock, provider)
        resp = client.post(
            "/v1/speech", json=_payload(session_id="sess-t", utterance_id="utt-t", chunk_seq=3)
        )
    assert resp.status_code == 200
    assert resp.headers["x-session-id"] == "sess-t"
    assert resp.headers["x-utterance-id"] == "utt-t"
    assert resp.headers["x-chunk-seq"] == "3"
