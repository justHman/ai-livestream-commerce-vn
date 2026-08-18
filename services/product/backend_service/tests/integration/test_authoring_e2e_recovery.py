"""Restart recovery for AI generation (Change B, B8).

Simulates a worker restart mid-generation: phase 1 drives the workflow through
planning + segment 0 (persisted to real PG), phase 2 re-hydrates from the
snapshot over NEW repository objects on the SAME database and resumes. No
duplicate semantic calls: segment 0 is NOT regenerated, no re-plan, and the
final version contains BOTH segments.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.models import ScriptIntent, ScriptState
from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
from backend.application.script_authoring.service_impl import (
    ScriptAuthoringServiceImpl,
    _SyncPersistBridge,
)
from backend.config import ScriptAuthoringConfig

from integration.authoring_helpers import FakeEngineManager, FakeLlm, gate_compliant_text


async def _connect(pg_url: str) -> PostgresAuthoringRepositories:
    store = PostgresRuntimeStore(pg_url)
    await store.connect()
    await store.apply_schema()
    repos = PostgresAuthoringRepositories(pg_url)
    await repos.connect()
    return repos


async def _wait_for_state(
    repos: PostgresAuthoringRepositories,
    set_id: str,
    product_id: str,
    state: ScriptState,
    tries: int = 600,
):
    for _ in range(tries):
        item = await repos.items.get_by_product(set_id, product_id)
        if item is not None and item.state is state:
            return item
        await asyncio.sleep(0.05)
    return await repos.items.get_by_product(set_id, product_id)


async def _new_set(service, product_ids):
    return await service.create_script_set(
        name="Set Recovery", transition_policy="ORDER_AGNOSTIC", product_ids=product_ids, brief=None
    )


async def _count_rows(repos, table: str, item_id: str) -> int:
    # Direct pool read to assert persistence invariants (no duplicates).
    async with repos._pool.acquire() as conn:  # noqa: SLF001 - integration assertion
        return await conn.fetchval(
            f"SELECT count(*) FROM {table} WHERE script_item_id = $1", item_id
        )


@pytest.mark.asyncio
async def test_recovery_resumes_without_duplicate_semantic_calls(pg_url: str) -> None:
    repos1 = await _connect(pg_url)
    try:
        llm1 = FakeLlm(
            segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
        )
        service1 = ScriptAuthoringServiceImpl(
            repos1, config=ScriptAuthoringConfig(), engine_manager=FakeEngineManager(llm1)
        )
        set_id = (await _new_set(service1, ["P1"]))["id"]
        item = await repos1.items.get_by_product(set_id, "P1")
        assert item is not None
        script_set = await repos1.script_sets.get(set_id)

        # ── Phase 1: plan + segment 0 persisted, then "crash" ──────────
        bridge1 = _SyncPersistBridge()
        driver1 = service1._build_driver(
            item,
            script_set,
            600,
            llm1,
            bridge1,
            emit=lambda *a, **k: None,
            batch_id="",
            loaders=service1._make_loaders({item.id: item}, {}, {}),
        )
        revisions1 = {item.id: item.revision}
        existing1: set[str] = set()
        # EMPTY -> PLANNING
        assert driver1.step() is True
        await service1._drain_artifacts(bridge1, revisions1, existing1)
        # PLANNING -> GENERATING (plan persisted)
        assert driver1.step() is True
        await service1._drain_artifacts(bridge1, revisions1, existing1)
        # GENERATING -> segment 0 generated + persisted; stop mid-generation.
        assert driver1.step() is True
        await service1._drain_artifacts(bridge1, revisions1, existing1)
        assert item.state is ScriptState.GENERATING
        snap = driver1.snapshot()
        assert snap["plan_id"] is not None
        assert llm1.segment_calls == 1

        # ── Phase 2: "restart" over NEW repos on the SAME database ─────
        repos2 = await _connect(pg_url)
        try:
            llm2 = FakeLlm(
                segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
            )
            service2 = ScriptAuthoringServiceImpl(
                repos2, config=ScriptAuthoringConfig(), engine_manager=FakeEngineManager(llm2)
            )
            item2 = await repos2.items.get_by_product(set_id, "P1")
            script_set2 = await repos2.script_sets.get(set_id)
            assert item2 is not None and item2.state is ScriptState.GENERATING

            plan_id = snap["plan_id"]
            segments_by_id = {s.id: s for s in await repos2.segments.list_by_plan(plan_id)}
            versions_by_id = {v.id: v for v in await repos2.versions.list_by_item(item2.id)}
            bridge2 = _SyncPersistBridge()
            driver2 = service2._build_driver(
                item2,
                script_set2,
                600,
                llm2,
                bridge2,
                emit=lambda *a, **k: None,
                batch_id="",
                loaders=service2._make_loaders({item2.id: item2}, segments_by_id, versions_by_id),
            )
            driver2.restore(snap)

            revisions2 = {item2.id: item2.revision}
            existing2: set[str] = set()
            while driver2.step():
                await service2._drain_artifacts(bridge2, revisions2, existing2)
            await service2._drain_artifacts(bridge2, revisions2, existing2)

            item2 = await repos2.items.get_by_product(set_id, "P1")
            assert item2 is not None and item2.state is ScriptState.REVIEWABLE
            version = await repos2.versions.get(item2.current_version_id)
            assert version is not None
            seg0_text = gate_compliant_text(0, 280)
            seg1_text = gate_compliant_text(280, 280)
            assert seg0_text in version.spoken_text
            assert seg1_text in version.spoken_text

            # No duplicate semantic calls: segment 0 was NOT regenerated and
            # segment 1 was generated exactly once after restart.
            assert llm1.segment_calls == 1
            assert llm2.segment_calls == 1

            # Exactly ONE plan row and 2 distinct segment rows (no duplicates).
            plan = await repos2.plans.get_latest(item2.id)
            assert plan is not None
            assert plan.id == plan_id  # the original plan survived; no re-plan
            assert await _count_rows(repos2, "product_script_plans", item2.id) == 1
            assert await _count_rows(repos2, "script_segments", item2.id) == 2
            segments = await repos2.segments.list_by_plan(plan_id)
            assert len(segments) == 2
            assert len({s.id for s in segments}) == 2
        finally:
            await repos2.close()
    finally:
        await repos1.close()


@pytest.mark.asyncio
async def test_restart_idempotent_start_returns_existing_job(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        llm = FakeLlm(
            segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
        )
        service = ScriptAuthoringServiceImpl(
            repos, config=ScriptAuthoringConfig(), engine_manager=FakeEngineManager(llm)
        )
        set_id = (await _new_set(service, ["P1"]))["id"]
        first = await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="recover-dup",
        )
        # The job row is inserted inside the FIRST call before it returns, so
        # the second call hits find_by_idempotency and returns immediately.
        second = await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="recover-dup",
        )
        assert second["workflow_id"] == first["workflow_id"]
        assert second.get("idempotent") is True

        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        job_row = await repos.jobs.find_by_idempotency(
            item.id, ScriptIntent.GENERATE_LONG_FORM.value, "recover-dup"
        )
        assert job_row is not None
        rows = await repos.jobs.list_by_batch(job_row.batch_id)
        assert len(rows) == 1  # exactly one job row for the idempotency key

        # The first-call background task still drives to REVIEWABLE.
        item = await _wait_for_state(repos, set_id, "P1", ScriptState.REVIEWABLE)
        assert item is not None and item.state is ScriptState.REVIEWABLE
    finally:
        await repos.close()
