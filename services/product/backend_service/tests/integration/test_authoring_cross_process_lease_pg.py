"""HIGH-1: PostgreSQL fencing leases prevent cross-process recovery races."""

from __future__ import annotations

import asyncio

import pytest

from backend.application.script_authoring.models import GenerationJobStatus, ScriptState
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig
from integration.authoring_helpers import FakeEngineManager
from integration.test_authoring_restart_recovery_pg import (
    _InterruptibleLlm,
    _connect,
    _count_rows,
    _good_pair_llm,
    _new_set,
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
