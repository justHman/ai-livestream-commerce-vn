"""R8.1 + R8.2: fence-and-renew lease primitives and transactionally fenced
artifact writes for Script Authoring (change-b multi-replica review).

- R8.1: ``JobRepository.assert_and_renew_lease`` /
  ``BatchRepository.assert_and_renew_lease`` are single-statement fencing
  primitives that renew the expiry ONLY while the caller still owns the
  ``(owner, epoch)`` fence; a stale epoch/owner raises ``LeaseLostError``.
- R8.2 / test 10.1: a stale owner whose lease was taken over cannot commit
  durable artifacts — the artifact-write transaction begins with the lease
  assertion, so ``LeaseLostError`` rolls back ZERO artifacts.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.application.script_authoring.models import (
    GenerationJobStatus,
    ProductScriptPlan,
    ScriptIntent,
    ScriptSegment,
    ScriptState,
    new_id,
)
from backend.application.script_authoring.repositories import LeaseLostError
from backend.application.script_authoring.service_impl import (
    _SyncPersistBridge,
    ScriptAuthoringServiceImpl,
)
from backend.config import ScriptAuthoringConfig
from integration.authoring_helpers import FakeEngineManager, gate_compliant_text
from integration.test_authoring_restart_recovery_pg import (
    _InterruptibleLlm,
    _connect,
    _count_rows,
    _good_pair_llm,
    _new_set,
    _wait_for_job,
    _wait_for_segments,
)

_STALE_TEXT = gate_compliant_text(560, 280)
_LEASE_S = 300


async def _make_service(pg_url: str, llm) -> ScriptAuthoringServiceImpl:
    return ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm),
    )


async def _job_lease(repos, job_id: str) -> dict:
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        row = await conn.fetchrow(
            "SELECT lease_owner, lease_expires_at, lease_epoch "
            "FROM script_generation_jobs WHERE id = $1",
            job_id,
        )
    assert row is not None
    return dict(row)


async def _batch_lease(repos, batch_id: str) -> dict:
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        row = await conn.fetchrow(
            "SELECT lease_owner, lease_expires_at, lease_epoch "
            "FROM script_generation_batches WHERE id = $1",
            batch_id,
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
    task = next((t for t in list(service._tasks) if t.get_name() == task_name), None)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


def _plan_for(item_id: str, product_id: str) -> ProductScriptPlan:
    return ProductScriptPlan(
        id=new_id("plan"),
        script_item_id=item_id,
        version=1,
        product_id=product_id,
        target_duration_s=600,
        K=1,
        segments=[],
        fingerprint="fence-tx",
    )


def _segment_for(item_id: str, plan_id: str, index: int, text: str) -> ScriptSegment:
    return ScriptSegment(
        id=new_id("segment"),
        script_item_id=item_id,
        plan_id=plan_id,
        segment_index=index,
        title="stale",
        intent="selling",
        target_duration_s=280,
        display_text=text,
        spoken_text=text,
        status=ScriptState.DRAFT,
        version=1,
    )


# ── R8.1: job fence-and-renew ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_assert_and_renew_lease_renews_expiry(pg_url: str) -> None:
    repos = await _connect(pg_url)
    service = await _make_service(pg_url, _good_pair_llm())
    try:
        set_id = (await _new_set(service, ["P1"]))["id"]
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job, epoch = await service._create_job(
            item, set_id, "P1", ScriptIntent.GENERATE_LONG_FORM, 600, "fence-renew"
        )
        before = await _job_lease(repos, job.id)
        await repos.jobs.assert_and_renew_lease(job.id, service._instance_id, epoch, _LEASE_S)
        after = await _job_lease(repos, job.id)
        assert after["lease_owner"] == service._instance_id
        assert after["lease_epoch"] == epoch
        assert after["lease_expires_at"] > before["lease_expires_at"]
        now = datetime.now(timezone.utc)
        assert (after["lease_expires_at"] - now).total_seconds() > _LEASE_S - 10
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


@pytest.mark.asyncio
async def test_job_assert_and_renew_lease_stale_epoch_raises(pg_url: str) -> None:
    repos = await _connect(pg_url)
    service = await _make_service(pg_url, _good_pair_llm())
    try:
        set_id = (await _new_set(service, ["P1"]))["id"]
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job, epoch = await service._create_job(
            item, set_id, "P1", ScriptIntent.GENERATE_LONG_FORM, 600, "fence-stale-epoch"
        )
        with pytest.raises(LeaseLostError):
            await repos.jobs.assert_and_renew_lease(
                job.id, service._instance_id, epoch + 1, _LEASE_S
            )
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


@pytest.mark.asyncio
async def test_job_assert_and_renew_lease_stale_owner_raises(pg_url: str) -> None:
    repos = await _connect(pg_url)
    service = await _make_service(pg_url, _good_pair_llm())
    try:
        set_id = (await _new_set(service, ["P1"]))["id"]
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job, epoch = await service._create_job(
            item, set_id, "P1", ScriptIntent.GENERATE_LONG_FORM, 600, "fence-stale-owner"
        )
        with pytest.raises(LeaseLostError):
            await repos.jobs.assert_and_renew_lease(job.id, "someone-else", epoch, _LEASE_S)
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


@pytest.mark.asyncio
async def test_job_assert_and_renew_lease_inside_transaction_commits_with_writes(
    pg_url: str,
) -> None:
    repos = await _connect(pg_url)
    service = await _make_service(pg_url, _good_pair_llm())
    try:
        set_id = (await _new_set(service, ["P1"]))["id"]
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job, epoch = await service._create_job(
            item, set_id, "P1", ScriptIntent.GENERATE_LONG_FORM, 600, "fence-tx"
        )
        plan = _plan_for(item.id, "P1")
        segment = _segment_for(item.id, plan.id, 0, gate_compliant_text(0, 280))
        async with repos.transaction() as conn:
            await repos.jobs.assert_and_renew_lease(
                job.id, service._instance_id, epoch, _LEASE_S, conn=conn
            )
            await repos.plans.insert(plan, conn=conn)
            await repos.segments.insert(segment, conn=conn)
        # Both the lease renewal and the artifact writes landed together.
        plan_row = await repos.plans.get(plan.id)
        assert plan_row is not None
        seg_row = await repos.segments.get(segment.id)
        assert seg_row is not None
        assert await _count_rows(repos, "product_script_plans", item.id) == 1
        assert await _count_rows(repos, "script_segments", item.id) == 1
        after = await _job_lease(repos, job.id)
        now = datetime.now(timezone.utc)
        assert (after["lease_expires_at"] - now).total_seconds() > _LEASE_S - 10
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


# ── R8.1: batch fence-and-renew ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_assert_and_renew_lease_renews_expiry(pg_url: str) -> None:
    repos = await _connect(pg_url)
    service = await _make_service(pg_url, _good_pair_llm())
    try:
        set_id = (await _new_set(service, ["P1"]))["id"]
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job, _ = await service._create_job(
            item, set_id, "P1", ScriptIntent.GENERATE_LONG_FORM, 600, "batch-renew"
        )
        epoch = await repos.batches.acquire_lease(job.batch_id, "batch-owner", _LEASE_S)
        before = await _batch_lease(repos, job.batch_id)
        await repos.batches.assert_and_renew_lease(job.batch_id, "batch-owner", epoch, _LEASE_S)
        after = await _batch_lease(repos, job.batch_id)
        assert after["lease_owner"] == "batch-owner"
        assert after["lease_epoch"] == epoch
        assert after["lease_expires_at"] > before["lease_expires_at"]
        now = datetime.now(timezone.utc)
        assert (after["lease_expires_at"] - now).total_seconds() > _LEASE_S - 10
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


@pytest.mark.asyncio
async def test_batch_assert_and_renew_lease_stale_epoch_raises(pg_url: str) -> None:
    repos = await _connect(pg_url)
    service = await _make_service(pg_url, _good_pair_llm())
    try:
        set_id = (await _new_set(service, ["P1"]))["id"]
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job, _ = await service._create_job(
            item, set_id, "P1", ScriptIntent.GENERATE_LONG_FORM, 600, "batch-stale-epoch"
        )
        epoch = await repos.batches.acquire_lease(job.batch_id, "batch-owner", _LEASE_S)
        with pytest.raises(LeaseLostError):
            await repos.batches.assert_and_renew_lease(
                job.batch_id, "batch-owner", epoch + 1, _LEASE_S
            )
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


@pytest.mark.asyncio
async def test_batch_assert_and_renew_lease_stale_owner_raises(pg_url: str) -> None:
    repos = await _connect(pg_url)
    service = await _make_service(pg_url, _good_pair_llm())
    try:
        set_id = (await _new_set(service, ["P1"]))["id"]
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job, _ = await service._create_job(
            item, set_id, "P1", ScriptIntent.GENERATE_LONG_FORM, 600, "batch-stale-owner"
        )
        epoch = await repos.batches.acquire_lease(job.batch_id, "batch-owner", _LEASE_S)
        with pytest.raises(LeaseLostError):
            await repos.batches.assert_and_renew_lease(
                job.batch_id, "someone-else", epoch, _LEASE_S
            )
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


@pytest.mark.asyncio
async def test_batch_assert_and_renew_lease_inside_transaction_commits_with_writes(
    pg_url: str,
) -> None:
    repos = await _connect(pg_url)
    service = await _make_service(pg_url, _good_pair_llm())
    try:
        set_id = (await _new_set(service, ["P1"]))["id"]
        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job, _ = await service._create_job(
            item, set_id, "P1", ScriptIntent.GENERATE_LONG_FORM, 600, "batch-tx"
        )
        epoch = await repos.batches.acquire_lease(job.batch_id, "batch-owner", _LEASE_S)
        plan = _plan_for(item.id, "P1")
        async with repos.transaction() as conn:
            await repos.batches.assert_and_renew_lease(
                job.batch_id, "batch-owner", epoch, _LEASE_S, conn=conn
            )
            await repos.plans.insert(plan, conn=conn)
        plan_row = await repos.plans.get(plan.id)
        assert plan_row is not None
        after = await _batch_lease(repos, job.batch_id)
        now = datetime.now(timezone.utc)
        assert (after["lease_expires_at"] - now).total_seconds() > _LEASE_S - 10
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


# ── R8.2 / test 10.1: stale owner cannot commit after takeover ──────────────


@pytest.mark.asyncio
async def test_stale_owner_cannot_commit_artifacts_after_takeover(pg_url: str) -> None:
    """A stale owner's artifact write after a takeover is rejected atomically.

    A drives a generation job to plan + segment 0 (slow segment LLM), then its
    lease is expired mid-flight; B claims a new epoch and recovers to terminal.
    When A's slow segment-1 provider result returns, A's drain attempts to
    persist its stale segment with A's OLD (owner, epoch) fence — the lease
    assertion fails, the whole transaction rolls back, and B's valid committed
    sequence is preserved exactly (plan=1, segments=2, no duplicates).
    """
    llm_a = _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: _STALE_TEXT},
        delay=5.0,
    )
    service_a = await _make_service(pg_url, llm_a)
    set_id = (await _new_set(service_a, ["P1"]))["id"]
    item = await service_a._repos.items.get_by_product(set_id, "P1")
    assert item is not None
    result = await service_a.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="stale-owner-takeover",
    )
    workflow_id = result["workflow_id"]
    plan, segs = await _wait_for_segments(service_a._repos, item.id, 1)
    seg0_id = segs[0].id
    # A's fence BEFORE the takeover (replayed later as stale).
    job_a = await service_a._repos.jobs.get(workflow_id)
    assert job_a is not None and job_a.lease_owner == service_a._instance_id

    # Make A's lease reclaimable; B claims a new epoch and recovers to terminal.
    control = await _connect(pg_url)
    await _expire_job_lease(control, workflow_id)
    await control.close()

    service_b = await _make_service(pg_url, _good_pair_llm())
    try:
        await service_b.recover_pending()
        await _wait_for_job(service_b._repos, workflow_id)
        job_b = await service_b._repos.jobs.get(workflow_id)
        assert job_b is not None and job_b.status is GenerationJobStatus.COMPLETED

        # Deterministic proof: a stale drain call carrying A's old fence raises
        # LeaseLostError and commits NOTHING.
        stale_bridge = _SyncPersistBridge()
        stale_bridge(_segment_for(item.id, plan.id, 1, _STALE_TEXT))
        with pytest.raises(LeaseLostError):
            await service_a._drain_artifacts(
                stale_bridge, {item.id: item.revision}, set(), job=job_a
            )

        # A's real background task wakes from its slow segment call and hits
        # the same fence naturally — it stops without marking the job FAILED.
        await _await_task(service_a, f"sa-gen:{workflow_id}")

        # Exactly one valid committed artifact sequence survives.
        assert await _count_rows(service_a._repos, "product_script_plans", item.id) == 1
        assert await _count_rows(service_a._repos, "script_segments", item.id) == 2
        segs_after = await service_a._repos.segments.list_by_plan(plan.id)
        assert len(segs_after) == 2
        assert segs_after[0].id == seg0_id
        assert all(_STALE_TEXT not in s.spoken_text for s in segs_after)
        assert any(gate_compliant_text(280, 280) in s.spoken_text for s in segs_after)
        # B owns the fence now and the job is not FAILED.
        assert job_b.status is GenerationJobStatus.COMPLETED
        lease_now = await _job_lease(service_a._repos, workflow_id)
        assert lease_now["lease_owner"] == service_b._instance_id
    finally:
        await service_a.drain(timeout_s=0.0)
        await service_a._repos.close()
        await service_b.drain(timeout_s=0.0)
        await service_b._repos.close()
