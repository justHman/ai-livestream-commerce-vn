"""ScriptAuthoringServiceImpl AI generation integration tests (Change B, B6).

Real Postgres repositories + the real ``ScriptGate`` + a fake/echo LLM injected
through a duck-typed EngineManager. Covers the one-product long-form path
(start_generation -> REVIEWABLE), idempotency, segment regeneration, and the
AI fix path.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.models import (
    GenerationJobStatus,
    ScriptSource,
    ScriptState,
)
from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig

from integration.authoring_helpers import (
    FakeEngineManager,
    FakeLlm,
    gate_compliant_text,
    short_segment_text,
)


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
    item = await repos.items.get_by_product(set_id, product_id)
    for _ in range(tries):
        item = await repos.items.get_by_product(set_id, product_id)
        if item is not None and item.state is state:
            return item
        await asyncio.sleep(0.05)
    return item


async def _wait_for_job(
    repos: PostgresAuthoringRepositories,
    workflow_id: str,
    *,
    terminal: tuple[GenerationJobStatus, ...] = (
        GenerationJobStatus.COMPLETED,
        GenerationJobStatus.FAILED,
    ),
    tries: int = 600,
) -> None:
    """Wait for the background job to reach a terminal status.

    ``start_generation``/``fix_with_ai``/``regenerate_segment`` return as soon
    as the job row is queued; the item state becomes DRAFT/REVIEWABLE inside
    the background task BEFORE its final ``jobs.update``. Tests must wait on
    the job row (the durable completion signal), not just the item, so
    ``repos.close()`` in teardown never races a still-running task (which
    leaks a pool connection and hangs ``pool.close()``).
    """
    job_id = workflow_id
    for _ in range(tries):
        job = await repos.jobs.get(job_id)
        if job is not None and job.status in terminal:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"background job {job_id} did not reach a terminal state")


async def _new_set(service, product_ids):
    return await service.create_script_set(
        name="Set PG", transition_policy="ORDER_AGNOSTIC", product_ids=product_ids, brief=None
    )


def _bad_pair_llm() -> FakeLlm:
    """seg0 long + seg1 short => segment gates pass, full gate too short."""
    return FakeLlm(
        segment_by_index={
            0: gate_compliant_text(0, 280),
            1: short_segment_text(),
        }
    )


@pytest.mark.asyncio
async def test_start_generation_completes_to_reviewable_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        llm = FakeLlm(
            segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
        )
        service = ScriptAuthoringServiceImpl(
            repos, config=ScriptAuthoringConfig(), engine_manager=FakeEngineManager(llm)
        )
        set_id = (await _new_set(service, ["P1"]))["id"]
        result = await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="gen-1",
        )
        assert result["workflow_id"].startswith("job:")
        assert result["status"] == "queued"

        await _wait_for_job(repos, result["workflow_id"])
        item = await _wait_for_state(repos, set_id, "P1", ScriptState.REVIEWABLE)
        assert item is not None
        assert item.state is ScriptState.REVIEWABLE
        assert item.current_version_id is not None
        version = await repos.versions.get(item.current_version_id)
        assert version is not None
        assert version.source is ScriptSource.AI_GENERATE
        # Plan row + exactly K real segment rows persisted (no placeholder rows).
        plan = await repos.plans.get_latest(item.id)
        assert plan is not None
        assert plan.segment_count == 2
        segments = await repos.segments.list_by_plan(plan.id)
        assert len(segments) == 2
        # The segment rows carry content (not the empty placeholder rows).
        assert all(seg.display_text for seg in segments)
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_start_generation_idempotent_duplicate_returns_same_job_pg(pg_url: str) -> None:
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
            idempotency_key="gen-dup",
        )
        second = await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="gen-dup",
        )
        assert second["workflow_id"] == first["workflow_id"]
        assert second.get("idempotent") is True
        await _wait_for_job(repos, first["workflow_id"])
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_regenerate_segment_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        engine_manager = FakeEngineManager(_bad_pair_llm())
        service = ScriptAuthoringServiceImpl(
            repos, config=ScriptAuthoringConfig(), engine_manager=engine_manager
        )
        set_id = (await _new_set(service, ["P1"]))["id"]
        gen = await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="gen-bad",
        )
        await _wait_for_job(repos, gen["workflow_id"])
        item = await _wait_for_state(repos, set_id, "P1", ScriptState.GATE_FAILED)
        assert item is not None and item.state is ScriptState.GATE_FAILED

        # Regenerate segment 1 with a long gate-compliant text => recompiled
        # full script now exceeds the 300s lower bound => REVIEWABLE.
        engine_manager._llm_fn = FakeLlm(segment_by_index={1: gate_compliant_text(560, 280)})
        regen = await service.regenerate_segment(
            set_id=set_id, product_id="P1", segment_index=1, idempotency_key="reg-1"
        )
        assert regen["segment_index"] == 1
        await _wait_for_job(repos, regen["workflow_id"])
        item = await _wait_for_state(repos, set_id, "P1", ScriptState.REVIEWABLE)
        assert item is not None and item.state is ScriptState.REVIEWABLE
        version = await repos.versions.get(item.current_version_id)
        assert version is not None
        current_segments = await repos.segments.list_selected(version.segment_version_ids)
        assert current_segments
        # Regeneration created a NEW immutable segment version: two distinct
        # segment rows now exist for index 1.
        all_segments = await repos.segments.list_by_plan(current_segments[0].plan_id)
        seg1_rows = [s for s in all_segments if s.segment_index == 1]
        assert len({s.version for s in seg1_rows}) == 2
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_fix_with_ai_produces_draft_ai_fix_version_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        engine_manager = FakeEngineManager(FakeLlm(default_segment=gate_compliant_text(700, 60)))
        service = ScriptAuthoringServiceImpl(
            repos, config=ScriptAuthoringConfig(), engine_manager=engine_manager
        )
        set_id = (await _new_set(service, ["P1"]))["id"]
        # A manual short draft fails the real Full Script Gate and keeps a
        # current version — the AI fix path repairs exactly that version.
        await service.save_draft(
            set_id=set_id,
            product_id="P1",
            display_text="Kem tốt",
            spoken_text="Kem tốt",
            revision=None,
        )
        submitted = await service.submit_for_gate(set_id=set_id, product_id="P1")
        assert submitted["state"] == "GATE_FAILED"

        result = await service.fix_with_ai(set_id=set_id, product_id="P1", idempotency_key="fix-1")
        assert result["workflow_id"].startswith("job:")
        assert result["status"] == "queued"

        await _wait_for_job(repos, result["workflow_id"])
        for _ in range(600):
            item = await repos.items.get_by_product(set_id, "P1")
            if item is not None and item.state is ScriptState.DRAFT and item.current_version_id:
                version = await repos.versions.get(item.current_version_id)
                if version is not None and version.source is ScriptSource.AI_FIX:
                    break
            await asyncio.sleep(0.05)
        assert item is not None and item.state is ScriptState.DRAFT
        version = await repos.versions.get(item.current_version_id)
        assert version.source is ScriptSource.AI_FIX
    finally:
        await repos.close()
