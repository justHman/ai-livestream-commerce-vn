"""HIGH-B / R6.3-R6.4: genuine process-restart recovery for Script Authoring.

A durable RUNNING single-product job (or QUEUED/RUNNING batch) must NOT be
merely rediscovered by idempotency after a restart — it must be reconstructed
from persisted finite state and re-spawned so it actually resumes and reaches a
durable terminal status.

These tests simulate a worker dying mid-flight:
  phase 1  real PG + a real service drive a job/batch to a partial durable
           state (plan + at least one segment committed), then the owned
           background task is cancelled and the pools are closed (process exit);
  phase 2  a completely fresh service/repository pool on the SAME database runs
           ``recover_pending()`` (the startup recovery hook), and the durable
           workflow continues from where it stopped.

Invariant asserted everywhere: already-committed immutable artifacts (plan /
segment / version rows) are NOT duplicated — exactly-once external LLM calls
are NOT claimed; committed artifact identity is.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.models import (
    GenerationJobStatus,
    ScriptState,
)
from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import AppConfig, ScriptAuthoringConfig, TTSConfig

from integration.authoring_helpers import FakeEngineManager, FakeLlm, gate_compliant_text


class _InterruptibleLlm(FakeLlm):
    """FakeLlm that delays ONLY segment calls (planning is instant).

    The delay keeps a background job/batch mid-flight long enough for the test
    to cancel it after a deterministic durable milestone is persisted.
    """

    def __init__(self, *, segment_by_index=None, default_segment=None, delay: float = 1.5):
        super().__init__(
            segment_by_index=segment_by_index, default_segment=default_segment, delay=0.0
        )
        self._segment_delay = delay

    def __call__(self, prompt: str) -> str:
        if "PLAN_THE_SCRIPT_SEGMENTS" not in prompt:
            time.sleep(self._segment_delay)
        return super().__call__(prompt)


def _good_pair_llm() -> FakeLlm:
    return FakeLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
    )


def _config(database_url: str) -> AppConfig:
    return AppConfig(
        app_env="dev",
        render_backend="mock",
        database_url=database_url,
        tts=TTSConfig(engine="tone"),
    )


async def _connect(pg_url: str) -> PostgresAuthoringRepositories:
    store = PostgresRuntimeStore(pg_url)
    await store.connect()
    await _apply_schema_once(store)
    repos = PostgresAuthoringRepositories(pg_url)
    await repos.connect()
    return repos


async def _apply_schema_once(store: PostgresRuntimeStore) -> None:
    """Apply the runtime schema only when this database does not have it yet.

    Tests create several repository pools on the same database (a service and
    its recovery replica). Re-running ``apply_schema`` while a background job
    is actively writing artifacts takes DDL locks that can deadlock against
    the artifact transaction (segment INSERT / item UPDATE ordering), which
    surfaces as flaky ``DeadlockDetectedError`` in the cross-process tests.
    The schema is idempotent and each test database is fresh, so applying it
    once per database is sufficient.
    """
    async with store._pool.acquire() as conn:  # noqa: SLF001 - integration harness
        applied = await conn.fetchval("SELECT to_regclass('script_sets') IS NOT NULL")
    if not applied:
        await store.apply_schema()


async def _new_set(service, product_ids, brief=None):
    return await service.create_script_set(
        name="Restart Recovery",
        transition_policy="ORDER_AGNOSTIC",
        product_ids=product_ids,
        brief=brief,
    )


async def _wait_for_job(
    repos,
    workflow_id: str,
    tries: int = 600,
    terminal: tuple[GenerationJobStatus, ...] = (
        GenerationJobStatus.COMPLETED,
        GenerationJobStatus.FAILED,
    ),
) -> None:
    for _ in range(tries):
        job = await repos.jobs.get(workflow_id)
        if job is not None and job.status in terminal:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"background job {workflow_id} did not reach a terminal state")


async def _wait_for_batch(repos, batch_id: str, statuses: tuple[str, ...], tries: int = 600):
    for _ in range(tries):
        result = await repos.batches.get(batch_id)
        if result is not None and result[1].status in statuses:
            return result
        await asyncio.sleep(0.05)
    return await repos.batches.get(batch_id)


async def _wait_for_segments(repos, item_id: str, min_count: int, tries: int = 300):
    """Wait until the item has a persisted plan with >= ``min_count`` segment rows."""
    for _ in range(tries):
        plan = await repos.plans.get_latest(item_id)
        if plan is not None:
            segs = await repos.segments.list_by_plan(plan.id)
            if len(segs) >= min_count:
                return plan, segs
        await asyncio.sleep(0.05)
    raise AssertionError(f"item {item_id} did not persist {min_count} segment(s)")


async def _count_rows(repos, table: str, item_id: str) -> int:
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        return await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE script_item_id = $1", item_id
        )


async def _interrupt_owned(service, name: str) -> None:
    """Cancel an owned background task, then close the pools (process exit)."""
    task = next(t for t in list(service._tasks) if t.get_name() == name)
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass
    await service.drain(timeout_s=0.0)
    await service._repos.close()


# ── single-product recovery ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_single_product_recovery_resumes_to_terminal(pg_url: str) -> None:
    # ── Phase 1: start generation, let plan + segment 0 persist, "die" ──
    llm1 = _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
    )
    service1 = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm1),
    )
    set_id = (await _new_set(service1, ["P1"]))["id"]
    item = await service1._repos.items.get_by_product(set_id, "P1")
    assert item is not None
    result = await service1.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="restart-single",
    )
    workflow_id = result["workflow_id"]
    plan, segs = await _wait_for_segments(service1._repos, item.id, 1)
    seg0_id = segs[0].id
    await _interrupt_owned(service1, f"sa-gen:{workflow_id}")

    # ── Phase 2: fresh repos on the SAME DB; startup recovery resumes ──
    repos2 = await _connect(pg_url)
    try:
        job = await repos2.jobs.get(workflow_id)
        assert job is not None and job.status is GenerationJobStatus.RUNNING
        llm2 = _good_pair_llm()
        service2 = ScriptAuthoringServiceImpl(
            repos2, config=ScriptAuthoringConfig(), engine_manager=FakeEngineManager(llm2)
        )
        await service2.recover_pending()
        await _wait_for_job(repos2, workflow_id)

        job2 = await repos2.jobs.get(workflow_id)
        assert job2 is not None
        assert job2.status is GenerationJobStatus.COMPLETED
        assert job2.id == workflow_id  # workflow identity unchanged

        item2 = await repos2.items.get_by_product(set_id, "P1")
        assert item2 is not None and item2.state is ScriptState.REVIEWABLE
        version = await repos2.versions.get(item2.current_version_id)
        assert version is not None
        assert gate_compliant_text(0, 280) in version.spoken_text
        assert gate_compliant_text(280, 280) in version.spoken_text

        # No duplicate immutable artifacts; the pre-crash segment 0 survived.
        assert await _count_rows(repos2, "product_script_plans", item2.id) == 1
        assert await _count_rows(repos2, "script_segments", item2.id) == 2
        segs_after = await repos2.segments.list_by_plan(plan.id)
        assert len(segs_after) == 2
        assert segs_after[0].id == seg0_id
        # The recovered runner regenerated only the unfinished segment 1.
        assert llm2.segment_calls == 1
    finally:
        await repos2.close()


# ── batch recovery ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_batch_recovery_resumes_partial_batch(pg_url: str) -> None:
    # ── Phase 1: start a 2-product batch, let partial progress persist, die ──
    llm1 = _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
    )
    service1 = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm1),
    )
    set_id = (await _new_set(service1, ["P1", "P2"]))["id"]
    result = await service1.start_batch_generation(
        set_id=set_id,
        product_ids=["P1", "P2"],
        target_duration_s=600,
        idempotency_key="restart-batch",
    )
    batch_id = result["batch_id"]
    item1 = await service1._repos.items.get_by_product(set_id, "P1")
    assert item1 is not None
    plan1, segs1 = await _wait_for_segments(service1._repos, item1.id, 1)
    seg0_before = segs1[0].id
    await _interrupt_owned(service1, f"sa-batch:{batch_id}")

    # ── Phase 2: fresh repos; recovery continues unfinished products ──
    repos2 = await _connect(pg_url)
    try:
        _batch, state = await repos2.batches.get(batch_id)
        assert state is not None and state.status in ("queued", "running")
        service2 = ScriptAuthoringServiceImpl(
            repos2,
            config=ScriptAuthoringConfig(),
            engine_manager=FakeEngineManager(_good_pair_llm()),
        )
        await service2.recover_pending()
        _batch2, state2 = await _wait_for_batch(
            repos2, batch_id, ("completed", "partial_completed")
        )
        assert state2.status == "completed"
        assert state2.completed == 2

        for pid in ("P1", "P2"):
            item = await repos2.items.get_by_product(set_id, pid)
            assert item is not None and item.state is ScriptState.REVIEWABLE
            assert await _count_rows(repos2, "product_script_plans", item.id) == 1
            assert await _count_rows(repos2, "script_segments", item.id) == 2
        # The pre-crash segment 0 row survived — it was NOT regenerated.
        segs_after = await repos2.segments.list_by_plan(plan1.id)
        assert len(segs_after) == 2
        assert segs_after[0].id == seg0_before
    finally:
        await repos2.close()


# ── duplicate-recovery safety ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_duplicate_recovery_produces_one_runner(pg_url: str) -> None:
    # Phase 1: interrupt a single-product job with plan + segment 0 persisted.
    llm1 = _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
    )
    service1 = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=FakeEngineManager(llm1),
    )
    set_id = (await _new_set(service1, ["P1"]))["id"]
    item = await service1._repos.items.get_by_product(set_id, "P1")
    assert item is not None
    result = await service1.start_generation(
        set_id=set_id,
        product_id="P1",
        target_duration_s=600,
        intent="selling",
        idempotency_key="restart-dup",
    )
    workflow_id = result["workflow_id"]
    _plan, _ = await _wait_for_segments(service1._repos, item.id, 1)
    await _interrupt_owned(service1, f"sa-gen:{workflow_id}")

    # Phase 2: a slow LLM keeps the recovered runner alive while we call
    # recovery twice and a client retry — all must observe ONE durable runner.
    repos2 = await _connect(pg_url)
    try:
        llm2 = _InterruptibleLlm(
            segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
            delay=1.5,
        )
        service2 = ScriptAuthoringServiceImpl(
            repos2, config=ScriptAuthoringConfig(), engine_manager=FakeEngineManager(llm2)
        )
        await service2.recover_pending()
        await service2.recover_pending()  # second attempt must be a no-op
        active = [
            t
            for t in service2._tasks
            if t.get_name() == f"sa-recover:{workflow_id}" and not t.done()
        ]
        assert len(active) == 1, "expected exactly one active recovered runner"

        # A client retry with the same idempotency key observes the same
        # workflow and must NOT spawn a second runner.
        again = await service2.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="restart-dup",
        )
        assert again["workflow_id"] == workflow_id
        assert again.get("idempotent") is True
        gen_tasks = [
            t for t in service2._tasks if t.get_name().startswith("sa-gen:") and not t.done()
        ]
        assert len(gen_tasks) == 0, "client retry spawned a second generation runner"

        await _wait_for_job(repos2, workflow_id)
        job2 = await repos2.jobs.get(workflow_id)
        assert job2 is not None and job2.status is GenerationJobStatus.COMPLETED
        item2 = await repos2.items.get_by_product(set_id, "P1")
        assert item2 is not None and item2.state is ScriptState.REVIEWABLE
        assert await _count_rows(repos2, "product_script_plans", item2.id) == 1
        assert await _count_rows(repos2, "script_segments", item2.id) == 2
    finally:
        await repos2.close()


# ── lifespan wiring ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lifespan_startup_recovers_pending_job(pg_url: str) -> None:
    """A brand-new app entering its lifespan runs recover_pending automatically."""
    from backend.main import create_app

    # Phase 1: real app + connected lifespan, interrupt mid-flight.
    app1 = create_app(config=_config(pg_url))
    container1 = app1.state.container
    service1 = container1.script_authoring_service
    assert service1 is not None
    em1 = container1.engine_manager
    em1.llm_cfg["engine"] = "echo"
    em1.get_llm_fn = lambda: _InterruptibleLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
    )
    lifespan1 = app1.router.lifespan_context(app1)
    await lifespan1.__aenter__()
    try:
        set_id = (await _new_set(service1, ["P1"]))["id"]
        item = await service1._repos.items.get_by_product(set_id, "P1")
        assert item is not None
        result = await service1.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="restart-lifespan",
        )
        workflow_id = result["workflow_id"]
        plan, _ = await _wait_for_segments(service1._repos, item.id, 1)
        await _interrupt_owned(service1, f"sa-gen:{workflow_id}")
    finally:
        await lifespan1.__aexit__(None, None, None)

    # Phase 2: a brand-new app on the same DB — its lifespan startup recovery
    # drives the durable RUNNING job to terminal with no explicit call.
    app2 = create_app(config=_config(pg_url))
    container2 = app2.state.container
    service2 = container2.script_authoring_service
    assert service2 is not None
    em2 = container2.engine_manager
    em2.llm_cfg["engine"] = "echo"
    em2.get_llm_fn = lambda: _good_pair_llm()
    lifespan2 = app2.router.lifespan_context(app2)
    await lifespan2.__aenter__()
    try:
        await _wait_for_job(service2._repos, workflow_id)
        job = await service2._repos.jobs.get(workflow_id)
        assert job is not None and job.status is GenerationJobStatus.COMPLETED
        item2 = await service2._repos.items.get_by_product(set_id, "P1")
        assert item2 is not None and item2.state is ScriptState.REVIEWABLE
        assert await _count_rows(service2._repos, "product_script_plans", item2.id) == 1
        assert await _count_rows(service2._repos, "script_segments", item2.id) == 2
    finally:
        await lifespan2.__aexit__(None, None, None)
