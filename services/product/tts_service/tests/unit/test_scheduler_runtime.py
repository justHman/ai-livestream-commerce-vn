"""SchedulerRuntime: dynamic micro-batch dispatch rules (Change T tasks
10.1-10.11).

Deterministic fake clock + fake provider (records batch calls, returns a
waveform whose sample count encodes the text length) so every dispatch rule,
deadline edge, backlog transition, cancellation, and result mapping is
asserted without real time or the VieNeu SDK.

Test convention: every test that needs a dispatch runs on a short coalescing
window (1 ms) and advances the fake clock past it; only the window-expiry
tests use a long window to prove the request STAYS pending until expiry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Hashable, Optional

import numpy as np
import pytest

from tts.config import RuntimeConfig
from tts.providers.capabilities import ProviderCapabilities
from tts.providers.errors import (
    DeadlineExceededError,
    OverloadError,
    ProviderInferenceError,
)
from tts.providers.models import (
    AudioResult,
    GenerationConfig,
    Priority,
    ProviderRequest,
    ProviderResult,
    SynthesisRequest,
)
from tts.scheduler.admission import AdmissionController
from tts.scheduler.fairness import FairnessSelector, PendingPopulation
from tts.scheduler.runtime import SchedulerRuntime

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)
TEXT_DURATION_MS = 240  # fake provider: len(text) * 240 ms of 48 kHz audio


class FakeClock:
    """Deterministic clock the runtime and tests share."""

    def __init__(self, start: datetime = NOW) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class FakeProvider:
    """Deterministic provider: waveform length encodes the request text.

    ``batch_calls`` records (key, request ids) per batch dispatch so tests
    assert grouping/order; ``fail_next_batches`` makes the next N batch calls
    raise ``ProviderInferenceError``.
    """

    def __init__(
        self,
        *,
        supports_native_batch: bool = True,
        max_batch_size: int = 32,
        max_dispatch_delay: float = 0.0,
    ) -> None:
        self.supports_native_batch = supports_native_batch
        self.max_batch_size = max_batch_size
        self.max_dispatch_delay = max_dispatch_delay
        self.batch_calls: list[tuple[Hashable, tuple[str, ...]]] = []
        self.fail_next_batches = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="fake",
            model_revision="fake-1",
            sample_rate_hz=48_000,
            supports_native_batch=self.supports_native_batch,
            max_batch_size=self.max_batch_size,
            supports_mixed_voice_batch=self.supports_native_batch,
            supported_styles=("natural",),
            supported_response_formats=("pcm", "wav"),
        )

    def batch_key(self, request: ProviderRequest) -> Hashable:
        return ("fake", request.generation_config.temperature)

    async def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        if self.fail_next_batches > 0:
            self.fail_next_batches -= 1
            self.batch_calls.append(
                (self.batch_key(requests[0]), tuple(r.request_id for r in requests))
            )
            raise ProviderInferenceError("fake batch failed")
        self.batch_calls.append(
            (self.batch_key(requests[0]), tuple(r.request_id for r in requests))
        )
        if self.max_dispatch_delay:
            await asyncio.sleep(self.max_dispatch_delay)
        return [self._result(request) for request in requests]

    async def synthesize(self, request: ProviderRequest) -> AudioResult:
        return self._result(request)

    def _result(self, request: ProviderRequest) -> AudioResult:
        n = max(1, len(request.input_text)) * TEXT_DURATION_MS * 48
        return AudioResult(
            request_id=request.request_id,
            sample_rate=48_000,
            waveform=np.zeros(n, dtype=np.float32),
            response_format="wav",
            duration_ms=TEXT_DURATION_MS,
        )


def _config(
    *,
    global_pending_limit: int = 512,
    per_session_pending_limit: int = 64,
    request_deadline_ms: int = 30_000,
    max_batch_size: int = 32,
    coalesce_window_ms: int = 1,
    aging_threshold_ms: int = 5_000,
) -> RuntimeConfig:
    return RuntimeConfig(
        provider="fake",
        model_revision="fake-1",
        global_pending_limit=global_pending_limit,
        per_session_pending_limit=per_session_pending_limit,
        request_deadline_ms=request_deadline_ms,
        max_batch_size=max_batch_size,
        coalesce_window_ms=coalesce_window_ms,
        aging_threshold_ms=aging_threshold_ms,
    )


def _request(
    request_id: str,
    session_id: str = "sess-1",
    *,
    chunk_seq: int = 0,
    priority: Priority = Priority.NORMAL,
    deadline_at: Optional[datetime] = None,
    **overrides,
) -> SynthesisRequest:
    base = dict(
        request_id=request_id,
        session_id=session_id,
        utterance_id="utt-0",
        chunk_seq=chunk_seq,
        input_text="xin chào",
        priority=priority,
        submitted_at=NOW,
    )
    base.update(overrides)
    if deadline_at is not None:
        base["deadline_at"] = deadline_at
    return SynthesisRequest(**base)


def _make_runtime(
    clock: FakeClock,
    provider: FakeProvider,
    *,
    max_batch_size: int = 32,
    coalesce_window_ms: int = 1,
    global_pending_limit: int = 512,
    per_session_pending_limit: int = 64,
) -> SchedulerRuntime:
    return SchedulerRuntime(
        population=PendingPopulation(),
        admission=AdmissionController(
            global_pending_limit, per_session_pending_limit
        ),
        selector=FairnessSelector(),
        provider=provider,
        config=_config(
            max_batch_size=max_batch_size, coalesce_window_ms=coalesce_window_ms
        ),
        clock=clock.now,
    )


async def _submit(runtime: SchedulerRuntime, *requests: SynthesisRequest) -> list[asyncio.Task]:
    return [asyncio.create_task(runtime.submit(request)) for request in requests]


async def _settle(clock: FakeClock, seconds: float = 0.05) -> None:
    """Yield so submit tasks run (stamping the window), advance the fake
    clock, then yield so the dispatcher drains the population."""
    await asyncio.sleep(0)
    clock.advance(seconds)
    await asyncio.sleep(0.02)


# ── 10.2/10.5: batch bound + immediate dispatch on fill ───────────────────────
async def test_batch_fills_before_window_dispatch_immediately() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=2)
    runtime = _make_runtime(clock, provider, max_batch_size=2, coalesce_window_ms=10_000)
    tasks = await _submit(runtime, _request("r1"), _request("r2"))
    await _settle(clock)  # both submitted: batch fills -> dispatch immediately
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert provider.batch_calls == [(("fake", 0.8), ("r1", "r2"))]


async def test_effective_batch_size_is_min_of_config_and_provider() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=2)
    runtime = _make_runtime(clock, provider, max_batch_size=32, coalesce_window_ms=1)
    tasks = await _submit(runtime, _request("r1"), _request("r2"), _request("r3"))
    await _settle(clock)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    # The provider bound caps the batch at 2: r1+r2 fill it immediately, and
    # r3 rides the backlog transition straight into the next batch (10.6).
    assert [call[1] for call in provider.batch_calls] == [("r1", "r2"), ("r3",)]


# ── 10.3/10.4: idle/empty coalescing window expiry ────────────────────────────
async def test_first_request_opens_window_and_dispatches_on_expiry() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4, coalesce_window_ms=10)
    task = await _submit(runtime, _request("r1"))
    await asyncio.sleep(0.01)  # dispatcher sees r1, window still open
    assert provider.batch_calls == []
    clock.advance(0.5)
    result = await asyncio.wait_for(task[0], timeout=5)
    assert result.request_id == "r1"
    assert provider.batch_calls == [(("fake", 0.8), ("r1",))]


async def test_window_dispatch_waits_until_expiry() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4, coalesce_window_ms=10_000)
    task = await _submit(runtime, _request("r1"))
    await asyncio.sleep(0.01)  # window (10 s) still open
    assert provider.batch_calls == []
    clock.advance(10.5)
    result = await asyncio.wait_for(task[0], timeout=5)
    assert result.request_id == "r1"
    assert len(provider.batch_calls) == 1


# ── 10.6: backlog dispatches immediately after completion (no new window) ─────
async def test_backlog_dispatches_immediately_after_completion() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=1)
    runtime = _make_runtime(clock, provider, max_batch_size=1, coalesce_window_ms=10_000)
    tasks = await _submit(runtime, _request("r1"), _request("r2"))
    await _settle(clock)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert [call[1] for call in provider.batch_calls] == [("r1",), ("r2",)]


# ── 10.7: requests during inference stay pending for the next batch ───────────
async def test_requests_during_in_flight_batch_wait_for_next_batch() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=1, max_dispatch_delay=0.1)
    runtime = _make_runtime(clock, provider, max_batch_size=1, coalesce_window_ms=1)
    first = await _submit(runtime, _request("r1"))
    clock.advance(0.05)
    await asyncio.sleep(0.01)  # r1 in flight
    second = await _submit(runtime, _request("r2"))
    results = await asyncio.wait_for(asyncio.gather(*(first + second)), timeout=5)
    assert [r.request_id for r in results] == ["r1", "r2"]
    assert [call[1] for call in provider.batch_calls] == [("r1",), ("r2",)]


# ── 10.8: deadline-near dispatch early ────────────────────────────────────────
async def test_deadline_near_request_dispatches_early() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=2)
    runtime = _make_runtime(clock, provider, max_batch_size=2, coalesce_window_ms=10_000)
    # dispatch_deadline = deadline - 3s margin = +1s; past that, urgent.
    urgent = _request("r1", deadline_at=NOW + timedelta(seconds=4))
    task = await _submit(runtime, urgent)
    await asyncio.sleep(0.01)
    clock.advance(1.1)
    result = await asyncio.wait_for(task[0], timeout=5)
    assert result.request_id == "r1"
    assert len(provider.batch_calls) == 1


# ── 10.9: CPU/non-native-batch provider: batch size 1, no coalesce ────────────
async def test_non_native_batch_provider_uses_single_no_coalesce() -> None:
    clock = FakeClock()
    provider = FakeProvider(supports_native_batch=False, max_batch_size=1)
    runtime = _make_runtime(clock, provider, max_batch_size=32, coalesce_window_ms=10_000)
    tasks = await _submit(runtime, _request("r1"), _request("r2"))
    await _settle(clock)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert [call[1] for call in provider.batch_calls] == [("r1",), ("r2",)]


# ── cancellation: pending skipped, in-flight discarded, siblings ok ───────────
async def test_cancelled_pending_request_is_skipped_by_selection() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4)
    runtime._admission.try_admit(_request("r1"), clock.now())
    runtime.cancel("r1")
    await _submit(runtime, _request("r2"))
    await _settle(clock)
    assert provider.batch_calls == [(("fake", 0.8), ("r2",))]


async def test_cancelled_in_flight_result_discarded_sibling_resolves() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=2, max_dispatch_delay=0.05)
    runtime = _make_runtime(clock, provider, max_batch_size=2, coalesce_window_ms=1)
    tasks = await _submit(runtime, _request("r1"), _request("r2"))
    clock.advance(0.05)
    await asyncio.sleep(0.02)  # both dispatched together, batch in flight
    tasks[0].cancel()  # caller disconnect
    await asyncio.sleep(0.01)
    results = await asyncio.wait_for(
        asyncio.gather(*tasks, return_exceptions=True), timeout=5
    )
    assert isinstance(results[0], asyncio.CancelledError)
    assert results[1].request_id == "r2"
    assert [call[1] for call in provider.batch_calls] == [("r1", "r2")]


# ── result mapping (10.10) ────────────────────────────────────────────────────
async def test_result_mapping_matches_request_ids_in_batch_order() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4)
    tasks = await _submit(runtime, _request("a", "sess-a"), _request("b", "sess-b"))
    await _settle(clock)
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert [r.request_id for r in results] == ["a", "b"]
    assert provider.batch_calls[0][1] == ("a", "b")


async def test_provider_failure_fails_all_members_and_runtime_continues() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4)
    provider.fail_next_batches = 1
    first = await _submit(runtime, _request("r1"), _request("r2"))
    await _settle(clock)
    with pytest.raises(ProviderInferenceError):
        await asyncio.wait_for(asyncio.gather(*first), timeout=5)
    provider.fail_next_batches = 0
    later = await _submit(runtime, _request("r3"))
    await _settle(clock)
    result = await asyncio.wait_for(later[0], timeout=5)
    assert result.request_id == "r3"
    assert len(provider.batch_calls) == 2


# ── deadline sweep (10.10) ────────────────────────────────────────────────────
async def test_expired_pending_request_raises_deadline_exceeded() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4, coalesce_window_ms=10_000)
    task = await _submit(runtime, _request("r1", deadline_at=NOW + timedelta(seconds=10)))
    await asyncio.sleep(0.01)
    clock.advance(11)
    with pytest.raises(DeadlineExceededError):
        await asyncio.wait_for(task[0], timeout=5)


# ── stats + admission wiring (10.1) ───────────────────────────────────────────
async def test_submit_overload_raises_before_entering_queue() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4, global_pending_limit=1)
    # Admit r1 deterministically (no dispatcher race), then r2 must 429.
    runtime._admission.try_admit(_request("r1"), clock.now())
    with pytest.raises(OverloadError):
        await runtime.submit(_request("r2"))


async def test_pending_depth_and_active_sessions() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4)
    tasks = await _submit(runtime, _request("r1", "sess-a"), _request("r2", "sess-b"))
    await _settle(clock)
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)
    assert runtime.pending_depth() == 0
    assert runtime.active_sessions() == set()


async def test_submit_returns_result_for_correct_request_id() -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=4)
    runtime = _make_runtime(clock, provider, max_batch_size=4)
    task = await _submit(runtime, _request("r1"))
    await _settle(clock)
    result = await asyncio.wait_for(task[0], timeout=5)
    assert result.request_id == "r1"
    assert result.sample_rate == 48_000
    assert result.waveform is not None
