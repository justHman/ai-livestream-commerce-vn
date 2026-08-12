"""Admission controller: global/per-session bounds, pre-validation,
duplicate IDs, cancellation, deadline expiration (Change T tasks 8.2-8.7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tts.providers.errors import (
    CapabilityError,
    DeadlineExceededError,
    OverloadError,
)
from tts.providers.models import SynthesisRequest
from tts.scheduler.admission import (
    AdmissionController,
    DispatchMargin,
    check_deadline,
)
from tts.scheduler.models import PendingRequest, PendingState

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


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


def _controller(**kwargs) -> AdmissionController:
    defaults = dict(global_pending_limit=3, per_session_pending_limit=2)
    defaults.update(kwargs)
    return AdmissionController(**defaults)


def _admit_all(
    controller: AdmissionController, requests: list[SynthesisRequest]
) -> list[PendingRequest]:
    return [
        admitted
        for request in requests
        if (admitted := controller.try_admit(request, NOW)) is not None
    ]


# ── global bound (8.4) ───────────────────────────────────────────────────────
async def test_global_limit_reached_raises_overload() -> None:
    controller = _controller(global_pending_limit=2)
    _admit_all(controller, [_request(request_id=f"req-{i}") for i in range(2)])
    with pytest.raises(OverloadError, match="global pending limit"):
        controller.try_admit(_request(request_id="req-3"), NOW)


async def test_release_frees_global_capacity() -> None:
    controller = _controller(global_pending_limit=2)
    admitted = _admit_all(controller, [_request(request_id=f"req-{i}") for i in range(2)])
    controller.release(admitted[0])
    admitted_again = controller.try_admit(_request(request_id="req-3"), NOW)
    assert admitted_again is not None
    assert controller.global_pending == 2


# ── per-session bound (8.5) ──────────────────────────────────────────────────
async def test_session_limit_reached_other_session_still_admits() -> None:
    controller = _controller(per_session_pending_limit=1)
    _admit_all(controller, [_request(request_id="req-1")])
    with pytest.raises(OverloadError, match="pending limit"):
        controller.try_admit(_request(request_id="req-2"), NOW)
    assert controller.try_admit(_request(request_id="req-3", session_id="sess-2"), NOW) is not None


async def test_session_pending_count_tracks_release() -> None:
    controller = _controller(per_session_pending_limit=2)
    first, second = _admit_all(
        controller, [_request(request_id="req-1"), _request(request_id="req-2")]
    )
    assert controller.session_pending("sess-1") == 2
    controller.release(first)
    assert controller.session_pending("sess-1") == 1
    controller.release(second)
    assert controller.session_pending("sess-1") == 0


# ── pre-validation before capacity (8.3) ─────────────────────────────────────
async def test_validation_failure_does_not_consume_capacity() -> None:
    def reject_style(request: SynthesisRequest) -> None:
        if request.style == "whisper":
            raise CapabilityError("unsupported style")

    controller = _controller(validate=reject_style)
    with pytest.raises(CapabilityError):
        controller.try_admit(_request(request_id="req-1", style="whisper"), NOW)
    assert controller.global_pending == 0
    assert controller.session_pending("sess-1") == 0
    assert controller.try_admit(_request(request_id="req-2"), NOW) is not None


async def test_validation_runs_before_overload_check() -> None:
    controller = _controller(
        global_pending_limit=0,
        validate=lambda _: (_ for _ in ()).throw(CapabilityError("unsupported")),
    )
    with pytest.raises(CapabilityError):
        controller.try_admit(_request(request_id="req-1"), NOW)


# ── duplicate request IDs (8.7) ──────────────────────────────────────────────
async def test_duplicate_request_id_rejected() -> None:
    controller = _controller()
    _admit_all(controller, [_request(request_id="req-1")])
    with pytest.raises(OverloadError, match="duplicate request_id"):
        controller.try_admit(_request(request_id="req-1"), NOW)


async def test_released_request_id_can_be_reused() -> None:
    controller = _controller()
    admitted = _admit_all(controller, [_request(request_id="req-1")])[0]
    controller.release(admitted)
    assert controller.try_admit(_request(request_id="req-1"), NOW) is not None


# ── cancellation (8.6) ───────────────────────────────────────────────────────
async def test_cancel_pending_marks_request() -> None:
    controller = _controller()
    pending = _admit_all(controller, [_request(request_id="req-1")])[0]
    controller.cancel(pending)
    assert pending.cancelled is True
    assert pending.state is PendingState.PENDING  # state stays; runtime observes flag


async def test_cancel_in_flight_does_not_touch_siblings() -> None:
    controller = _controller()
    pending = _admit_all(controller, [_request(request_id="req-1")])[0]
    sibling = _admit_all(controller, [_request(request_id="req-2")])[0]
    pending.state = PendingState.IN_FLIGHT
    controller.cancel(pending)
    assert pending.cancelled is True
    assert sibling.cancelled is False
    assert sibling.state is PendingState.PENDING


async def test_cancel_keeps_accounting_until_release() -> None:
    controller = _controller()
    pending = _admit_all(controller, [_request(request_id="req-1")])[0]
    controller.cancel(pending)
    assert controller.global_pending == 1
    controller.release(pending)
    assert controller.global_pending == 0


# ── deadline expiration (8.7) ────────────────────────────────────────────────
async def test_expired_request_raises_deadline_exceeded() -> None:
    controller = _controller()
    pending = _admit_all(controller, [_request(request_id="req-1")])[0]
    with pytest.raises(DeadlineExceededError, match="missed its dispatch deadline"):
        check_deadline(pending, NOW + timedelta(seconds=31))


async def test_fresh_request_passes_deadline_check() -> None:
    controller = _controller()
    pending = _admit_all(controller, [_request(request_id="req-1")])[0]
    check_deadline(pending, NOW + timedelta(seconds=1))


async def test_dispatch_deadline_precedes_request_deadline() -> None:
    controller = _controller()
    pending = _admit_all(controller, [_request(request_id="req-1")])[0]
    margin = timedelta(seconds=DispatchMargin().seconds)
    assert pending.dispatch_deadline == NOW + timedelta(seconds=30) - margin
