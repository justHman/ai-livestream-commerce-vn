"""HIGH-1: PostgreSQL fencing leases prevent cross-process recovery races."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import pytest

from backend.application.script_authoring.models import GenerationJobStatus, ScriptState
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig
from integration.authoring_helpers import FakeEngineManager, gate_compliant_text
from integration.test_authoring_restart_recovery_pg import (
    _InterruptibleLlm,
    _connect,
    _count_rows,
    _good_pair_llm,
    _new_set,
    _wait_for_batch,
    _wait_for_job,
    _wait_for_segments,
)


async def _hard_crash(service: ScriptAuthoringServiceImpl, task_name: str) -> None:
    """Stop a worker without draining its lease, then close its pool."""
    task = next(task for task in service._tasks if task.get_name() == task_name)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await service._repos.close()


async def _job_lease(repos, job_id: str) -> dict:
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        row = await conn.fetchrow(
            "SELECT lease_owner, lease_expires_at, lease_epoch "
            "FROM script_generation_jobs WHERE id = $1",
            job_id,
        )
    assert row is not None
    return dict(row)


async def _expire_job_lease(repos, job_id: str) -> None:
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        await conn.execute(
            "UPDATE script_generation_jobs "
            "SET lease_expires_at = NOW() - interval '1 second' WHERE id = $1",
            job_id,
        )


async def _await_task(service, task_name: str, timeout: float = 60.0) -> None:
    """Await an owned background task by name if it is still active."""
    task = next((t for t in list(service._tasks) if t.get_name() == task_name), None)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


async def _batch_lease(repos, batch_id: str) -> dict:
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        row = await conn.fetchrow(
            "SELECT lease_owner, lease_expires_at, lease_epoch "
            "FROM script_generation_batches WHERE id = $1",
            batch_id,
        )
    assert row is not None
    return dict(row)


async def _expire_batch_lease(repos, batch_id: str) -> None:
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        await conn.execute(
            "UPDATE script_generation_batches "
            "SET lease_expires_at = NOW() - interval '1 second' WHERE id = $1",
            batch_id,
        )


async def _batch_cancel_flag(repos, batch_id: str) -> bool:
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        row = await conn.fetchrow(
            "SELECT cancel_requested FROM script_generation_batches WHERE id = $1",
            batch_id,
        )
    assert row is not None
    return bool(row["cancel_requested"])


def _task_names(service) -> list[str]:
    return [t.get_name() for t in list(service._tasks) if not t.done()]


@pytest.mark.asyncio
async def test_two_processes_concurrent_recovery_yields_one_claimant(pg_url: str) -> None:
    """Concurrent startup recovery atomically assigns one job to one process."""
    llm_a = _InterruptibleLlm(segment_by_index={0: _good_pair_llm().segment_by_index[0]}, delay=1.5)
    service_a = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_a),
    )
    set_id = (await _new_set(service_a, ["P1"]))["id"]
    item = await service_a._repos.items.get_by_product(set_id, "P1")
    assert item is not None
    result = await service_a.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="cross-process-concurrent",
    )
    workflow_id = result["workflow_id"]
    plan, segments = await _wait_for_segments(service_a._repos, item.id, 1)
    segment_id = segments[0].id
    await _hard_crash(service_a, f"sa-gen:{workflow_id}")
    # A hard crash leaves the lease valid; simulate the recovery_lease_seconds
    # window elapsing so both replicas can race for the claim.
    control = await _connect(pg_url)
    await _expire_job_lease(control, workflow_id)
    await control.close()

    llm_a2 = _good_pair_llm()
    service_a2 = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_a2),
    )
    llm_b = _good_pair_llm()
    service_b = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_b),
    )
    try:
        await asyncio.gather(service_a2.recover_pending(), service_b.recover_pending())
        lease = await _job_lease(service_a2._repos, workflow_id)
        owner = lease["lease_owner"]
        assert owner in {service_a2._instance_id, service_b._instance_id}
        assert (
            sum(owner == instance for instance in (service_a2._instance_id, service_b._instance_id))
            == 1
        )

        await _wait_for_job(service_a2._repos, workflow_id)
        job = await service_a2._repos.jobs.get(workflow_id)
        assert job is not None and job.status is GenerationJobStatus.COMPLETED
        assert job.id == workflow_id
        item_after = await service_a2._repos.items.get_by_product(set_id, "P1")
        assert item_after is not None and item_after.state is ScriptState.REVIEWABLE
        assert await _count_rows(service_a2._repos, "product_script_plans", item_after.id) == 1
        assert await _count_rows(service_a2._repos, "script_segments", item_after.id) == 2
        segments_after = await service_a2._repos.segments.list_by_plan(plan.id)
        assert segments_after[0].id == segment_id
        # Only the winning claimant regenerates the unfinished segment; the
        # loser must not have produced any LLM segment calls.
        assert llm_a2.segment_calls + llm_b.segment_calls == 1
    finally:
        await service_a2.drain(timeout_s=0.0)
        await service_b.drain(timeout_s=0.0)
        await service_a2._repos.close()
        await service_b._repos.close()


@pytest.mark.asyncio
async def test_rolling_deploy_new_replica_does_not_steal_valid_lease(pg_url: str) -> None:
    """A fresh replica waits for a valid lease, then recovers after release."""
    llm_a = _InterruptibleLlm(
        segment_by_index={
            0: _good_pair_llm().segment_by_index[0],
            1: _good_pair_llm().segment_by_index[1],
        },
        delay=1.5,
    )
    service_a = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_a),
    )
    set_id = (await _new_set(service_a, ["P1"]))["id"]
    item = await service_a._repos.items.get_by_product(set_id, "P1")
    assert item is not None
    result = await service_a.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="rolling-deploy-lease",
    )
    workflow_id = result["workflow_id"]
    lease_a = await _job_lease(service_a._repos, workflow_id)
    assert lease_a["lease_owner"] == service_a._instance_id

    service_b = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(_good_pair_llm()),
    )
    try:
        await service_b.recover_pending()
        lease_b = await _job_lease(service_b._repos, workflow_id)
        assert lease_b["lease_owner"] == service_a._instance_id
        assert not any(
            task.get_name() == f"sa-recover:{workflow_id}" and not task.done()
            for task in service_b._tasks
        )

        await service_a.drain(timeout_s=0.0)
        await service_a._repos.close()
        await service_b.recover_pending()
        await _wait_for_job(service_b._repos, workflow_id)
        job = await service_b._repos.jobs.get(workflow_id)
        assert job is not None and job.status is GenerationJobStatus.COMPLETED
        assert await _count_rows(service_b._repos, "product_script_plans", item.id) == 1
        assert await _count_rows(service_b._repos, "script_segments", item.id) == 2
    finally:
        await service_b.drain(timeout_s=0.0)
        await service_b._repos.close()


@pytest.mark.asyncio
async def test_lease_expiry_allows_recovery_after_crash_without_clean_release(pg_url: str) -> None:
    """An expired lease left by a hard crash can be claimed by a new process."""
    llm_a = _InterruptibleLlm(
        segment_by_index={
            0: _good_pair_llm().segment_by_index[0],
            1: _good_pair_llm().segment_by_index[1],
        },
        delay=1.5,
    )
    service_a = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_a),
    )
    set_id = (await _new_set(service_a, ["P1"]))["id"]
    item = await service_a._repos.items.get_by_product(set_id, "P1")
    assert item is not None
    result = await service_a.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="expired-lease-recovery",
    )
    workflow_id = result["workflow_id"]
    await _wait_for_segments(service_a._repos, item.id, 1)
    await _hard_crash(service_a, f"sa-gen:{workflow_id}")

    control_repos = await _connect(pg_url)
    await _expire_job_lease(control_repos, workflow_id)
    await control_repos.close()

    service_c = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(_good_pair_llm()),
    )
    try:
        await service_c.recover_pending()
        await _wait_for_job(service_c._repos, workflow_id)
        job = await service_c._repos.jobs.get(workflow_id)
        assert job is not None and job.status is GenerationJobStatus.COMPLETED
        assert await _count_rows(service_c._repos, "product_script_plans", item.id) == 1
        assert await _count_rows(service_c._repos, "script_segments", item.id) == 2
    finally:
        await service_c.drain(timeout_s=0.0)
        await service_c._repos.close()


@pytest.mark.asyncio
async def test_healthy_slow_owner_stays_owned_through_heartbeat(pg_url: str) -> None:
    """R8.3 / test 10.2: a HEALTHY slow provider call must not lose its lease.

    A owns a generation job whose segment LLM sleeps 3 s per call while the
    lease window is only 1 s. Without a heartbeat the fence would lapse mid-call
    and a concurrent replica's ``recover_pending`` would falsely take over the
    healthy owner. With the bounded heartbeat the fence is renewed every
    ``lease/3`` so B claims NOTHING, the loop stays responsive, the job
    completes under A, and no heartbeat task is left dangling.
    """
    llm_a = _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
        delay=3.0,
    )
    service_a = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(recovery_lease_seconds=1),
        engine_manager=FakeEngineManager(llm_a),
    )
    set_id = (await _new_set(service_a, ["P1"]))["id"]
    item = await service_a._repos.items.get_by_product(set_id, "P1")
    assert item is not None
    result = await service_a.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="healthy-slow-heartbeat",
    )
    workflow_id = result["workflow_id"]
    plan, segs = await _wait_for_segments(service_a._repos, item.id, 1)
    lease_a = await _job_lease(service_a._repos, workflow_id)
    assert lease_a["lease_owner"] == service_a._instance_id
    epoch_a = lease_a["lease_epoch"]

    # Let A enter the slow segment-1 call and outlive the original 1 s lease
    # window (renewed by the segment-0 drain). From here only the heartbeat
    # keeps the fence alive.
    await asyncio.sleep(1.5)

    # The event loop stays responsive while A is inside the 3 s provider call.
    t0 = time.perf_counter()
    await asyncio.sleep(0.05)
    assert time.perf_counter() - t0 < 0.5, "event loop blocked by slow provider call"

    service_b = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(recovery_lease_seconds=1),
        engine_manager=FakeEngineManager(_good_pair_llm()),
    )
    try:
        # B's concurrent recovery mid-call claims NOTHING.
        await service_b.recover_pending()
        assert not any(
            task.get_name() == f"sa-recover:{workflow_id}" and not task.done()
            for task in service_b._tasks
        )
        lease_mid = await _job_lease(service_a._repos, workflow_id)
        assert lease_mid["lease_owner"] == service_a._instance_id
        assert lease_mid["lease_epoch"] == epoch_a
        now = datetime.now(timezone.utc)
        assert lease_mid["lease_expires_at"] > now, "lease expired while owner was alive"

        # The job completes normally under A.
        await _wait_for_job(service_a._repos, workflow_id)
        job = await service_a._repos.jobs.get(workflow_id)
        assert job is not None and job.status is GenerationJobStatus.COMPLETED
        item_after = await service_a._repos.items.get_by_product(set_id, "P1")
        assert item_after is not None and item_after.state is ScriptState.REVIEWABLE
        assert await _count_rows(service_a._repos, "product_script_plans", item_after.id) == 1
        assert await _count_rows(service_a._repos, "script_segments", item_after.id) == 2
        # B never ran provider work — exactly one owner's segments were generated.
        assert llm_a.segment_calls == 2

        # No orphan heartbeat task remains after the owner completes.
        await _await_task(service_a, f"sa-gen:{workflow_id}")
        pending_heartbeats = [
            t
            for t in asyncio.all_tasks()
            if t.get_name() == f"sa-heartbeat:{workflow_id}" and not t.done()
        ]
        assert pending_heartbeats == []
        assert all(t.done() for t in list(service_a._tasks))
    finally:
        await service_a.drain(timeout_s=0.0)
        await service_b.drain(timeout_s=0.0)
        await service_a._repos.close()
        await service_b._repos.close()


# ── R8.4 / R8.5: durable cross-replica batch cancel ──────────────────────────


@pytest.mark.asyncio
async def test_batch_two_processes_race_yields_one_claimant_and_no_duplicates(
    pg_url: str,
) -> None:
    """10.3: two replicas racing to recover a crashed batch → exactly one owner.

    A owns a 2-product batch, persists plan + segment 0 for P1, then hard-crashes
    (no clean lease release). After the lease expires, A2 and B concurrently call
    ``recover_pending()``: the atomic ``claim_recoverable`` fencing gives exactly
    ONE of them the batch, that winner resumes and drives BOTH products to
    terminal, and no committed immutable artifact is duplicated.
    """
    llm_a = _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
        delay=1.5,
    )
    service_a = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_a),
    )
    set_id = (await _new_set(service_a, ["P1", "P2"]))["id"]
    result = await service_a.start_batch_generation(
        set_id=set_id,
        product_ids=["P1", "P2"],
        target_duration_s=600,
        idempotency_key="batch-race",
    )
    batch_id = result["batch_id"]
    item1 = await service_a._repos.items.get_by_product(set_id, "P1")
    assert item1 is not None
    plan1, segs1 = await _wait_for_segments(service_a._repos, item1.id, 1)
    seg0_before = segs1[0].id
    await _hard_crash(service_a, f"sa-batch:{batch_id}")

    control = await _connect(pg_url)
    await _expire_batch_lease(control, batch_id)
    await control.close()

    llm_a2 = _good_pair_llm()
    service_a2 = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_a2),
    )
    llm_b = _good_pair_llm()
    service_b = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_b),
    )
    try:
        await asyncio.gather(service_a2.recover_pending(), service_b.recover_pending())
        lease = await _batch_lease(service_a2._repos, batch_id)
        owner = lease["lease_owner"]
        assert owner in {service_a2._instance_id, service_b._instance_id}
        assert (
            sum(owner == instance for instance in (service_a2._instance_id, service_b._instance_id))
            == 1
        )

        _batch, state = await _wait_for_batch(
            service_a2._repos, batch_id, ("completed", "partial_completed")
        )
        assert state.status == "completed"
        assert state.completed == 2
        # Durable batch identity unchanged.
        assert _batch.id == batch_id

        for pid in ("P1", "P2"):
            item = await service_a2._repos.items.get_by_product(set_id, pid)
            assert item is not None and item.state is ScriptState.REVIEWABLE
            assert await _count_rows(service_a2._repos, "product_script_plans", item.id) == 1
            assert await _count_rows(service_a2._repos, "script_segments", item.id) == 2
        # The pre-crash segment 0 row survived — it was NOT regenerated.
        segs_after = await service_a2._repos.segments.list_by_plan(plan1.id)
        assert len(segs_after) == 2
        assert segs_after[0].id == seg0_before
    finally:
        await service_a2.drain(timeout_s=0.0)
        await service_b.drain(timeout_s=0.0)
        await service_a2._repos.close()
        await service_b._repos.close()


@pytest.mark.asyncio
async def test_batch_non_owner_cancel_persists_request_without_takeover(pg_url: str) -> None:
    """10.4: a non-owner replica persists only the durable cancel request.

    B receives ``cancel_batch`` for a batch A owns: B does NOT reconstruct a
    runner, does NOT claim the lease, does NOT write artifacts — it only sets
    ``cancel_requested = TRUE`` and returns a ``cancelling`` response. A's owner
    loop then observes the durable request, stops scheduling new segment calls,
    and persists the terminal ``cancelled`` state under A's own fence.
    """
    llm_a = _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
        delay=1.5,
    )
    service_a = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_a),
    )
    set_id = (await _new_set(service_a, ["P1", "P2"]))["id"]
    result = await service_a.start_batch_generation(
        set_id=set_id,
        product_ids=["P1", "P2"],
        target_duration_s=600,
        idempotency_key="batch-non-owner-cancel",
    )
    batch_id = result["batch_id"]
    item1 = await service_a._repos.items.get_by_product(set_id, "P1")
    assert item1 is not None
    await _wait_for_segments(service_a._repos, item1.id, 1)
    lease_before = await _batch_lease(service_a._repos, batch_id)
    assert lease_before["lease_owner"] == service_a._instance_id

    service_b = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(_good_pair_llm()),
    )
    try:
        response = await service_b.cancel_batch(set_id=set_id, batch_id=batch_id)
        assert response == {"batch_id": batch_id, "status": "cancelling"}
        # B did not become a runner and did not claim the lease.
        names = _task_names(service_b)
        assert not any(n in ("sa-batch:" + batch_id, "sa-recover-batch:" + batch_id) for n in names)
        lease_after = await _batch_lease(service_b._repos, batch_id)
        assert lease_after["lease_owner"] == service_a._instance_id
        assert lease_after["lease_epoch"] == lease_before["lease_epoch"]
        # B persisted only the durable request.
        assert await _batch_cancel_flag(service_b._repos, batch_id) is True

        calls_before = llm_a.segment_calls
        _batch, state = await _wait_for_batch(service_b._repos, batch_id, ("cancelled",))
        assert state.status == "cancelled"
        # A stopped scheduling new segment work once the request became visible:
        # at most one in-flight round (<= 2 active products) after the cancel.
        assert llm_a.segment_calls - calls_before <= 2
        # Terminal CANCELLED landed under A's own fence.
        lease_terminal = await _batch_lease(service_b._repos, batch_id)
        assert lease_terminal["lease_owner"] == service_a._instance_id
    finally:
        await service_a.drain(timeout_s=0.0)
        await service_b.drain(timeout_s=0.0)
        await service_a._repos.close()
        await service_b._repos.close()


@pytest.mark.asyncio
async def test_batch_cancel_survives_owner_crash(pg_url: str) -> None:
    """10.5: a durable cancel request outlives the owner and wins at recovery.

    ``cancel_requested = TRUE`` is set while A owns the batch; A crashes before
    it can write the terminal CANCELLED state. A fresh replica recovers: it
    observes the durable request, schedules NO new semantic work (zero segment
    calls), and lands the terminal ``cancelled`` state safely under its claimed
    fence.
    """
    llm_a = _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
        delay=1.5,
    )
    service_a = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_a),
    )
    set_id = (await _new_set(service_a, ["P1", "P2"]))["id"]
    result = await service_a.start_batch_generation(
        set_id=set_id,
        product_ids=["P1", "P2"],
        target_duration_s=600,
        idempotency_key="batch-cancel-crash",
    )
    batch_id = result["batch_id"]
    item1 = await service_a._repos.items.get_by_product(set_id, "P1")
    assert item1 is not None
    await _wait_for_segments(service_a._repos, item1.id, 1)

    # Set the durable request (any replica may), then hard-crash the owner
    # before its loop can observe the request and write the terminal state.
    control = await _connect(pg_url)
    await control.batches.request_cancel(batch_id)
    assert await _batch_cancel_flag(control, batch_id) is True
    await _hard_crash(service_a, f"sa-batch:{batch_id}")
    await _expire_batch_lease(control, batch_id)
    await control.close()

    llm_c = _good_pair_llm()
    service_c = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm_c),
    )
    try:
        await service_c.recover_pending()
        _batch, state = await _wait_for_batch(service_c._repos, batch_id, ("cancelled",))
        assert state.status == "cancelled"
        # Recovery observed the durable request and scheduled NO segment work.
        assert llm_c.segment_calls == 0
        lease = await _batch_lease(service_c._repos, batch_id)
        assert lease["lease_owner"] == service_c._instance_id
    finally:
        await service_c.drain(timeout_s=0.0)
        await service_c._repos.close()
