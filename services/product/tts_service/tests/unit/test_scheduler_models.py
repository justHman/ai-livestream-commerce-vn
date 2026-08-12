"""Scheduler state models: PendingRequest lifecycle, InFlightBatch, identity
equality (Change T task 8.1/8.7)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from tts.providers.models import AudioResult, Priority, SynthesisRequest
from tts.scheduler.models import (
    InFlightBatch,
    PendingRequest,
    PendingState,
    same_request,
)

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


class _FakeProvider:
    def batch_key(self, request):  # noqa: ANN001
        return "provider-key"


def _request(**overrides) -> SynthesisRequest:
    base = dict(
        request_id="req-1",
        session_id="sess-1",
        utterance_id="utt-1",
        chunk_seq=0,
        input_text="Xin chào",
        submitted_at=NOW,
    )
    base.update(overrides)
    return SynthesisRequest(**base)


def _pending(**overrides) -> PendingRequest:
    request = _request(**{k: v for k, v in overrides.items() if k != "state"})
    pending = PendingRequest(
        synthesis_request=request,
        admitted_at=NOW,
        dispatch_deadline=NOW + timedelta(seconds=27),
        provider_batch_key="provider-key",
    )
    if "state" in overrides:
        pending.state = overrides["state"]
    return pending


# ── request identity equality (8.1) ─────────────────────────────────────────
async def test_same_request_matches_request_id_only() -> None:
    first = _pending()
    second = _pending(request_id="req-2")
    assert same_request(first, second) is False
    assert same_request(_pending(), _pending()) is True


async def test_same_request_ignores_wrapper_mutation() -> None:
    first = _pending()
    second = _pending()
    second.cancelled = True
    second.state = PendingState.CANCELLED
    assert same_request(first, second) is True


# ── PendingRequest lifecycle (8.1) ───────────────────────────────────────────
async def test_pending_request_exposes_immutable_identity() -> None:
    pending = _pending()
    assert pending.request_id == "req-1"
    assert pending.session_id == "sess-1"
    assert pending.synthesis_request.priority is Priority.NORMAL
    assert pending.state is PendingState.PENDING
    assert pending.cancelled is False


async def test_completion_future_carries_audio_result() -> None:
    pending = _pending()
    result = AudioResult(request_id="req-1", sample_rate=48_000, audio_bytes=b"RIFF")
    pending.completion.set_result(result)
    assert pending.completion.result() is result


async def test_pending_request_is_mutable_wrapper() -> None:
    pending = _pending()
    pending.state = PendingState.IN_FLIGHT
    pending.cancelled = True
    assert pending.state is PendingState.IN_FLIGHT
    assert pending.cancelled is True


async def test_provider_batch_key_stored_at_admission() -> None:
    assert _pending().provider_batch_key == "provider-key"


# ── deadline semantics (8.7) ─────────────────────────────────────────────────
async def test_is_expired_false_before_dispatch_deadline() -> None:
    pending = _pending()
    assert pending.is_expired(NOW + timedelta(seconds=10)) is False


async def test_is_expired_true_at_dispatch_deadline() -> None:
    assert _pending().is_expired(NOW + timedelta(seconds=27)) is True


async def test_is_expired_true_after_dispatch_deadline() -> None:
    assert _pending().is_expired(NOW + timedelta(seconds=28)) is True


async def test_is_expired_falls_back_to_request_deadline() -> None:
    pending = _pending()
    pending.dispatch_deadline = None
    assert pending.is_expired(NOW + timedelta(seconds=29)) is False
    assert pending.is_expired(NOW + timedelta(seconds=31)) is True


# ── InFlightBatch (8.1) ──────────────────────────────────────────────────────
async def test_in_flight_batch_is_immutable_and_groups_members() -> None:
    members = (_pending(), _pending(request_id="req-2"))
    batch = InFlightBatch(
        batch_key="provider-key",
        members=members,
        dispatched_at=NOW,
        provider=_FakeProvider(),
    )
    with pytest.raises(Exception):
        batch.members = members  # type: ignore[misc]
    assert batch.batch_key == "provider-key"
    assert [m.request_id for m in batch.members] == ["req-1", "req-2"]


# ── asyncio future support (8.7 pattern) ─────────────────────────────────────
async def test_completion_future_awaits_across_async_boundary() -> None:
    pending = _pending()
    result = AudioResult(request_id="req-1", sample_rate=48_000, audio_bytes=b"RIFF")

    async def resolve() -> None:
        await asyncio.sleep(0)
        pending.completion.set_result(result)

    task = asyncio.create_task(resolve())
    assert await pending.completion is result
    await task
