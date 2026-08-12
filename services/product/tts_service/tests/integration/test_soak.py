"""Soak regression smoke: multi-session churn over the scheduler runtime
(Change T tasks 16.1/16.2).

Not a production soak. The production soak runs many minutes on real
hardware/GPUs; this is a CI-safe regression smoke that drives the same
SchedulerRuntime shape — concurrent sessions submitting continuously,
interleaved cancellations, bounded queues — over a short deterministic
period (default 2 s, override with TTS_SOAK_SECONDS).

The runtime dispatches on the injected clock (window expiry, deadline
sweep), so a ticking fake clock advances the whole scheduler deterministically
while wall time stays bounded.

Cancellations are deterministic: a submit task cancelled synchronously right
after create_task is cancelled before the dispatcher ever sees it (the
caller-disconnect-before-dispatch path); in-flight cancellation is covered by
the unit suite (test_cancelled_in_flight_result_discarded_sibling_resolves).

Assertions (16.2): every dispatched request resolves exactly once to its own
request id (zero routing/fairness failures), the queue drains back to
baseline (pending depth 0, no active sessions, admission counters zero), and
the runtime stays healthy for a follow-up session. Memory growth is
deliberately NOT asserted here — tracemalloc over a synthetic waveform loop
measures the test harness, not the runtime; real memory soak belongs to the
GPU soak run.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import numpy as np

from tts.config import RuntimeConfig
from tts.providers.capabilities import ProviderCapabilities
from tts.providers.models import (
    AudioResult,
    Priority,
    ProviderRequest,
    ProviderResult,
    SynthesisRequest,
)
from tts.scheduler.admission import AdmissionController
from tts.scheduler.fairness import FairnessSelector, PendingPopulation
from tts.scheduler.runtime import SchedulerRuntime

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
DEFAULT_SOAK_SECONDS = 2.0
SESSION_COUNT = 4
CHUNKS_PER_SESSION = 20
CANCEL_EVERY = 5  # cancel every 5th chunk before it dispatches


class FakeClock:
    """Deterministic clock; a background thread ticks it like real time."""

    def __init__(self) -> None:
        self._now = NOW
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += timedelta(seconds=seconds)


def start_ticking(clock: FakeClock, tick: float = 0.01) -> threading.Thread:
    thread = threading.Thread(target=_tick_loop, args=(clock, tick), daemon=True)
    thread.start()
    return thread


def _tick_loop(clock: FakeClock, tick: float) -> None:
    while True:
        time.sleep(tick)
        clock.advance(tick)


class SoakProvider:
    """Deterministic provider: records dispatched request ids per batch."""

    def __init__(self, max_batch_size: int = 4) -> None:
        self.max_batch_size = max_batch_size
        self.dispatched: list[tuple[str, ...]] = []

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
        return request.voice_profile_id

    async def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        self.dispatched.append(tuple(r.request_id for r in requests))
        await asyncio.sleep(0)
        return [self._result(r) for r in requests]

    def _result(self, request: ProviderRequest) -> AudioResult:
        return AudioResult(
            request_id=request.request_id,
            sample_rate=48_000,
            waveform=np.zeros(max(1, len(request.input_text)) * 48 * 240, dtype=np.float32),
            response_format="wav",
            duration_ms=240,
        )


def _make_runtime(clock: FakeClock, provider: SoakProvider) -> SchedulerRuntime:
    return SchedulerRuntime(
        population=PendingPopulation(),
        admission=AdmissionController(global_pending_limit=512, per_session_pending_limit=64),
        selector=FairnessSelector(),
        provider=provider,
        config=RuntimeConfig(
            provider="fake",
            model_revision="fake-1",
            global_pending_limit=512,
            per_session_pending_limit=64,
            request_deadline_ms=30_000,
            max_batch_size=4,
            coalesce_window_ms=1,
            aging_threshold_ms=5_000,
        ),
        clock=clock.now,
    )


def _chunk(session_id: str, seq: int) -> SynthesisRequest:
    return SynthesisRequest(
        request_id=f"{session_id}-{seq}",
        session_id=session_id,
        utterance_id=f"utt-{seq % 3}",
        chunk_seq=seq,
        input_text="xin chào quý khách hôm nay chúng ta có sản phẩm mới",
        voice_profile_id="profile-a",
        priority=Priority.NORMAL,
        response_format="wav",
        submitted_at=NOW,
    )


def _soak_seconds() -> float:
    return max(0.5, float(os.environ.get("TTS_SOAK_SECONDS", DEFAULT_SOAK_SECONDS)))


async def test_soak_multi_session_churn_drains_cleanly() -> None:
    clock = FakeClock()
    provider = SoakProvider()
    runtime = _make_runtime(clock, provider)
    start_ticking(clock)
    duration = _soak_seconds()

    tasks: list[asyncio.Task] = []
    cancelled_tasks: list[asyncio.Task] = []

    async def emit(session_id: str) -> None:
        for seq in range(CHUNKS_PER_SESSION):
            task = asyncio.create_task(runtime.submit(_chunk(session_id, seq)))
            tasks.append(task)
            if seq % CANCEL_EVERY == 0:
                # Caller disconnects before the dispatcher can see it.
                cancelled_tasks.append(task)
                task.cancel()
            await asyncio.sleep(duration / CHUNKS_PER_SESSION)

    try:
        await asyncio.gather(*(emit(f"sess-{i}") for i in range(SESSION_COUNT)))
        await asyncio.sleep(0.05)  # let the dispatcher drain the tail

        results = await asyncio.gather(*tasks, return_exceptions=True)
        cancelled_ids = {id(t) for t in cancelled_tasks}
        pairs = list(zip(tasks, results))
        cancelled = [r for t, r in pairs if id(t) in cancelled_ids]
        completed = [r for t, r in pairs if id(t) not in cancelled_ids]

        # Zero routing/fairness failures: cancelled requests never dispatched
        # and every dispatched request resolved exactly once to its own id.
        assert all(isinstance(r, asyncio.CancelledError) for r in cancelled)
        assert all(isinstance(r, AudioResult) for r in completed)
        completed_ids = sorted(r.request_id for r in completed)
        dispatched_ids = sorted(rid for batch in provider.dispatched for rid in batch)
        assert completed_ids == dispatched_ids

        # Queue back to baseline: no pending work, no leaked session keys.
        assert runtime.pending_depth() == 0
        assert runtime.active_sessions() == set()
        assert runtime._admission.global_pending == 0
        assert runtime._admission._session_pending == {}

        # The runtime keeps serving a fresh session after the soak (16.2).
        clock.advance(0.1)
        fresh = await runtime.submit(_chunk("sess-after", 0))
        assert fresh.request_id == "sess-after-0"
    finally:
        await runtime.close()
