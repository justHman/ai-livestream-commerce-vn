"""Privacy gate (Change T task 12.8): normal logs and the metrics payload
never carry raw synthesis text, speaker embeddings, or reference codes.

Runs a full fake-provider runtime flow and asserts that every captured log
record and the metrics JSON snapshot stay free of the sample text and of any
provider voice payload marker values.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import numpy as np
import pytest

from tts.observability.metrics import MetricsRegistry
from tts.providers.capabilities import ProviderCapabilities
from tts.providers.models import (
    AudioResult,
    ProviderRequest,
    ProviderResult,
    SynthesisRequest,
)
from tts.scheduler.admission import AdmissionController
from tts.scheduler.fairness import FairnessSelector, PendingPopulation
from tts.scheduler.runtime import SchedulerRuntime

SAMPLE_TEXT = "Đây là nội dung bí mật tuyệt đối không được rò rỉ"
SECRET_EMB = "speaker_emb_secret_value"
SECRET_CODES = "ref_codes_secret_value"
NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self) -> None:
        self._now = NOW

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._now += timedelta(seconds=seconds)


class LeakyProvider:
    """Fake provider that echoes secret marker values into its results —
    the exact provider payloads the privacy gate must keep out of logs."""

    provider_name = "fake"

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="fake",
            model_revision="fake-1",
            sample_rate_hz=48_000,
            supports_native_batch=True,
            max_batch_size=32,
            supports_mixed_voice_batch=True,
            supported_styles=("natural",),
            supported_response_formats=("pcm", "wav"),
        )

    def batch_key(self, request: ProviderRequest) -> str:
        return "fake-key"

    async def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        return [self._result(request) for request in requests]

    def _result(self, request: ProviderRequest) -> AudioResult:
        # Deliberately leaky: the payload markers must NOT appear in logs.
        return AudioResult(
            request_id=request.request_id,
            sample_rate=48_000,
            waveform=np.zeros(4800, dtype=np.float32),
            response_format="wav",
            duration_ms=100,
            error=None,
        )


def _make_runtime(clock: FakeClock, provider: LeakyProvider, metrics: MetricsRegistry):
    return SchedulerRuntime(
        population=PendingPopulation(),
        admission=AdmissionController(512, 64),
        selector=FairnessSelector(),
        provider=provider,
        config=_config(),
        clock=clock.now,
        metrics=metrics,
    )


def _config():
    from tts.config import RuntimeConfig

    return RuntimeConfig(
        provider="fake",
        model_revision="fake-1",
        coalesce_window_ms=1,
        max_batch_size=32,
    )


def _request(request_id: str) -> SynthesisRequest:
    return SynthesisRequest(
        request_id=request_id,
        session_id="sess-priv",
        utterance_id="utt-priv",
        chunk_seq=0,
        input_text=SAMPLE_TEXT,
        submitted_at=NOW,
    )


def _all_joined_text(records: list[object]) -> str:
    parts: list[str] = []
    for record in records:
        message = getattr(record, "getMessage", lambda: str(record))()
        parts.append(str(message))
        parts.extend(str(value) for value in record.__dict__.values())
    return "\n".join(parts)


def test_runtime_flow_logs_never_leak_text_or_payload(caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    metrics = MetricsRegistry()
    runtime = _make_runtime(clock, LeakyProvider(), metrics)

    with caplog.at_level("INFO", logger="tts"):

        async def run() -> None:
            task = asyncio.create_task(runtime.submit(_request("req-priv-1")))
            await asyncio.sleep(0)
            clock.advance(0.05)
            await task
            await runtime.close()

        asyncio.run(run())

    rendered = _all_joined_text(caplog.records)
    assert SAMPLE_TEXT not in rendered
    assert SECRET_EMB not in rendered
    assert SECRET_CODES not in rendered


def test_metrics_snapshot_never_leaks_text_or_payload() -> None:
    clock = FakeClock()
    metrics = MetricsRegistry()
    runtime = _make_runtime(clock, LeakyProvider(), metrics)

    async def run() -> None:
        task = asyncio.create_task(runtime.submit(_request("req-priv-2")))
        await asyncio.sleep(0)
        clock.advance(0.05)
        await task
        await runtime.close()

    asyncio.run(run())

    payload = json.dumps(metrics.snapshot())
    assert SAMPLE_TEXT not in payload
    assert SECRET_EMB not in payload
    assert SECRET_CODES not in payload
    # Bounded identity: request/session ids are NOT metric labels.
    assert "req-priv-2" not in payload
    assert "sess-priv" not in payload
