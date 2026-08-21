"""R8.3: bounded lease heartbeat helper unit tests (zero PG).

``ScriptAuthoringServiceImpl._with_lease_heartbeat`` runs a sync provider call
off the loop while renewing the job/batch fence every
``lease_heartbeat_interval()`` seconds (``lease/3``, bounded below at 0.25 s).
The heartbeat matches owner+epoch via ``assert_and_renew_lease``; on a lost
fence it discards the provider result and raises ``LeaseLostError`` so the
caller commits nothing. The heartbeat task is cancelled/awaited before the
helper returns — never left dangling.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.application.script_authoring.repositories import LeaseLostError
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig


class _LeaseRepo:
    """Controllable ``assert_and_renew_lease`` (job or batch)."""

    def __init__(self) -> None:
        self.calls = 0
        self.fail_after: int | None = None  # raise LeaseLostError from the Nth call

    async def assert_and_renew_lease(self, *args, **kwargs) -> None:
        self.calls += 1
        if self.fail_after is not None and self.calls >= self.fail_after:
            raise LeaseLostError("lease lost")


class _FakeRepos:
    def __init__(self) -> None:
        self.jobs = _LeaseRepo()
        self.batches = _LeaseRepo()


def _make_service(lease_s: int = 1) -> ScriptAuthoringServiceImpl:
    # lease=1s -> interval = max(1/3, 0.25) = ~0.33s.
    return ScriptAuthoringServiceImpl(
        _FakeRepos(), config=ScriptAuthoringConfig(recovery_lease_seconds=lease_s)
    )


_JOB = SimpleNamespace(id="job-1", lease_owner="owner-a", lease_epoch=3)
_BATCH_LEASE = ("owner-a", 5)


# ── interval is centralized on the config ────────────────────────────────────


def test_lease_heartbeat_interval_centralized() -> None:
    assert ScriptAuthoringConfig().lease_heartbeat_interval() == 100.0  # 300/3
    assert ScriptAuthoringConfig(
        recovery_lease_seconds=1
    ).lease_heartbeat_interval() == pytest.approx(1 / 3)
    # Lower bound: a tiny lease still leaves a sane heartbeat cadence.
    assert ScriptAuthoringConfig(recovery_lease_seconds=0).lease_heartbeat_interval() == 0.25


# ── job heartbeat ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_renews_job_lease_while_slow_call_runs() -> None:
    service = _make_service()

    def slow() -> str:
        time.sleep(0.8)  # longer than one interval; the heartbeat must fire
        return "provider-result"

    result = await service._with_lease_heartbeat(slow, job=_JOB)
    assert result == "provider-result"
    assert service._repos.jobs.calls >= 1, "heartbeat never renewed the job lease"


@pytest.mark.asyncio
async def test_heartbeat_raises_when_job_lease_lost_mid_call() -> None:
    service = _make_service()
    service._repos.jobs.fail_after = 1

    def slow() -> str:
        time.sleep(0.8)
        return "late-result"  # must be discarded

    with pytest.raises(LeaseLostError):
        await service._with_lease_heartbeat(slow, job=_JOB)
    assert service._repos.jobs.calls >= 1


@pytest.mark.asyncio
async def test_heartbeat_returns_result_and_never_fires_for_fast_call() -> None:
    service = _make_service()

    def fast() -> str:
        return "quick"

    result = await service._with_lease_heartbeat(fast, job=_JOB)
    assert result == "quick"
    assert service._repos.jobs.calls == 0, "fast call completed before the first beat"


@pytest.mark.asyncio
async def test_heartbeat_leaves_no_orphan_task_when_parent_cancelled() -> None:
    service = _make_service()
    entered: list[bool] = []

    def slow() -> str:
        entered.append(True)
        time.sleep(1.0)
        return "late-result"

    task = asyncio.create_task(service._with_lease_heartbeat(slow, job=_JOB))
    for _ in range(200):
        if entered:
            break
        await asyncio.sleep(0.01)
    assert entered, "slow provider call never started"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    pending = [
        t for t in asyncio.all_tasks() if t.get_name() == "sa-heartbeat:job-1" and not t.done()
    ]
    assert pending == [], "heartbeat task leaked after parent cancellation"


# ── batch heartbeat ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_heartbeat_renews_batch_lease_while_slow_call_runs() -> None:
    service = _make_service()

    def slow() -> str:
        time.sleep(0.8)
        return "round-result"

    result = await service._with_lease_heartbeat(slow, batch_id="batch-1", batch_lease=_BATCH_LEASE)
    assert result == "round-result"
    assert service._repos.batches.calls >= 1, "heartbeat never renewed the batch lease"


@pytest.mark.asyncio
async def test_heartbeat_raises_when_batch_lease_lost_mid_call() -> None:
    service = _make_service()
    service._repos.batches.fail_after = 1

    def slow() -> str:
        time.sleep(0.8)
        return "late-result"

    with pytest.raises(LeaseLostError):
        await service._with_lease_heartbeat(slow, batch_id="batch-1", batch_lease=_BATCH_LEASE)
    assert service._repos.batches.calls >= 1
