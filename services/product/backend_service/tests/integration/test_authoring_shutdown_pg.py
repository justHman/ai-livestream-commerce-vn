"""R1 (HIGH-1): production shutdown must own background Script Authoring jobs.

Reproduces the pool-close race that hung the CI Coverage gate at the real
production boundary: a background generation/regenerate/fix/batch task is
still running when the FastAPI lifespan shutdown closes the authoring
repository pool.

``ScriptAuthoringServiceImpl`` must own every task it spawns (``_spawn``),
stop admitting new work, and drain owned tasks (bounded graceful completion,
then cancel + await) BEFORE ``PostgresAuthoringRepositories.close()`` — no
leaked task, no "not connected" error, bounded shutdown, and unfinished
durable jobs left recoverable across a restart.
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
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import AppConfig, ScriptAuthoringConfig, TTSConfig

from integration.authoring_helpers import (
    FakeEngineManager,
    FakeLlm,
    gate_compliant_text,
    short_segment_text,
)

# Seconds per fake LLM call. Enough that the job is still active when the test
# triggers shutdown, small enough that the drain completes it gracefully.
_GRACE_DELAY = 0.5


def _config(
    database_url: str, *, script_authoring: ScriptAuthoringConfig | None = None
) -> AppConfig:
    return AppConfig(
        app_env="dev",
        render_backend="mock",
        database_url=database_url,
        tts=TTSConfig(engine="tone"),
        script_authoring=script_authoring or ScriptAuthoringConfig(),
    )


def _production_app(pg_url: str, llm, *, script_authoring: ScriptAuthoringConfig | None = None):
    """Build the real production app and wire a controllable slow LLM in place.

    ``create_app`` runs the true composition (real container, real service over
    real Postgres, real lifespan). The engine manager is patched so
    ``_require_llm()`` resolves the injected fake — the same seam a real LLM
    engine occupies in production.
    """
    from backend.main import create_app

    app = create_app(config=_config(pg_url, script_authoring=script_authoring))
    container = app.state.container
    service = container.script_authoring_service
    assert service is not None
    em = container.engine_manager
    em.llm_cfg["engine"] = "echo"  # _require_llm treats a non-"none" engine as available
    em.get_llm_fn = lambda: llm
    return app, service


async def _enter_lifespan(app):
    lifespan = app.router.lifespan_context(app)
    await lifespan.__aenter__()
    return lifespan


async def _exit_lifespan(lifespan) -> float:
    started = time.perf_counter()
    await lifespan.__aexit__(None, None, None)
    return time.perf_counter() - started


async def _spawn_job(service, spawn):
    """Capture the single service-owned background task created by the call.

    ``start_batch_generation`` also spawns a best-effort idempotency ``_flush``
    task (not service-owned, self-swallowing); the owned jobs are the ones
    named ``sa-*`` by ``ScriptAuthoringServiceImpl._spawn``.
    """
    before = set(asyncio.all_tasks())
    result = await spawn(service)
    new_tasks = [t for t in asyncio.all_tasks() if t not in before]
    owned = [t for t in new_tasks if t.get_name().startswith("sa-")]
    assert len(owned) == 1, f"expected 1 owned background task, got {len(owned)}"
    return result, owned[0]


async def _connect(pg_url: str) -> PostgresAuthoringRepositories:
    store = PostgresRuntimeStore(pg_url)
    await store.connect()
    await store.apply_schema()
    repos = PostgresAuthoringRepositories(pg_url)
    await repos.connect()
    return repos


async def _wait_for_job(repos, workflow_id, *, tries: int = 600) -> None:
    for _ in range(tries):
        job = await repos.jobs.get(workflow_id)
        if job is not None and job.status in (
            GenerationJobStatus.COMPLETED,
            GenerationJobStatus.FAILED,
        ):
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"background job {workflow_id} did not reach a terminal state")


async def _assert_owned_and_closed(service, task, elapsed) -> None:
    """Shutdown contract: bounded, no leaked task, pool closed."""
    assert elapsed < 9.0, f"shutdown took {elapsed:.1f}s; background job was not drained"
    assert task.done(), "shutdown returned with a still-running background job"
    assert service._tasks == set(), f"leaked owned tasks: {service._tasks}"
    assert service._repos._pool is None, "authoring pool was not closed before shutdown returned"


async def _new_set(service, name, product_ids):
    return await service.create_script_set(
        name=name, transition_policy="ORDER_AGNOSTIC", product_ids=product_ids, brief=None
    )


@pytest.mark.asyncio
async def test_shutdown_drains_running_generation_job_before_close(pg_url: str) -> None:
    llm = FakeLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
        delay=_GRACE_DELAY,
    )
    app, service = _production_app(pg_url, llm)
    lifespan = await _enter_lifespan(app)
    entered = True
    try:
        set_id = (await _new_set(service, "Shutdown Gen", ["P1"]))["id"]
        result, task = await _spawn_job(
            service,
            lambda svc: svc.start_generation(
                set_id=set_id,
                product_id="P1",
                target_duration_s=600,
                intent="selling",
                idempotency_key="shutdown-gen",
            ),
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "background generation must still be running before shutdown"
        elapsed = await _exit_lifespan(lifespan)
        entered = False
    finally:
        if entered:
            try:
                await lifespan.__aexit__(None, None, None)
            except Exception:
                pass

    await _assert_owned_and_closed(service, task, elapsed)
    # The job ran to completion inside the drain window and is durably terminal.
    read = await _connect(pg_url)
    try:
        job = await read.jobs.get(result["workflow_id"])
        assert job is not None and job.status is GenerationJobStatus.COMPLETED
    finally:
        await read.close()


@pytest.mark.asyncio
async def test_shutdown_drains_running_regenerate_job_before_close(pg_url: str) -> None:
    # Drive the item to GATE_FAILED first (short second segment), then regen.
    llm = FakeLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: short_segment_text()},
        delay=_GRACE_DELAY,
    )
    app, service = _production_app(pg_url, llm)
    lifespan = await _enter_lifespan(app)
    entered = True
    task = None
    try:
        set_id = (await _new_set(service, "Shutdown Regen", ["P1"]))["id"]
        first, _ = await _spawn_job(
            service,
            lambda svc: svc.start_generation(
                set_id=set_id,
                product_id="P1",
                target_duration_s=600,
                intent="selling",
                idempotency_key="shutdown-regen-0",
            ),
        )
        await _wait_for_job(service._repos, first["workflow_id"])
        item = await service._repos.items.get_by_product(set_id, "P1")
        assert item is not None and item.state is ScriptState.GATE_FAILED

        service._engine_manager.get_llm_fn = lambda: FakeLlm(
            segment_by_index={1: gate_compliant_text(560, 280)}, delay=_GRACE_DELAY
        )
        result, task = await _spawn_job(
            service,
            lambda svc: svc.regenerate_segment(
                set_id=set_id, product_id="P1", segment_index=1, idempotency_key="shutdown-regen-1"
            ),
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "background regenerate must still be running before shutdown"
        elapsed = await _exit_lifespan(lifespan)
        entered = False
    finally:
        if entered:
            try:
                await lifespan.__aexit__(None, None, None)
            except Exception:
                pass

    await _assert_owned_and_closed(service, task, elapsed)
    read = await _connect(pg_url)
    try:
        job = await read.jobs.get(result["workflow_id"])
        assert job is not None and job.status is GenerationJobStatus.COMPLETED
    finally:
        await read.close()


@pytest.mark.asyncio
async def test_shutdown_drains_running_fix_job_before_close(pg_url: str) -> None:
    llm = FakeLlm(default_segment=gate_compliant_text(700, 60), delay=_GRACE_DELAY)
    app, service = _production_app(pg_url, llm)
    lifespan = await _enter_lifespan(app)
    entered = True
    task = None
    try:
        set_id = (await _new_set(service, "Shutdown Fix", ["P1"]))["id"]
        await service.save_draft(
            set_id=set_id,
            product_id="P1",
            display_text="Kem tốt",
            spoken_text="Kem tốt",
            revision=None,
        )
        submitted = await service.submit_for_gate(set_id=set_id, product_id="P1")
        assert submitted["state"] == "GATE_FAILED"
        result, task = await _spawn_job(
            service,
            lambda svc: svc.fix_with_ai(
                set_id=set_id, product_id="P1", idempotency_key="shutdown-fix"
            ),
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "background fix must still be running before shutdown"
        elapsed = await _exit_lifespan(lifespan)
        entered = False
    finally:
        if entered:
            try:
                await lifespan.__aexit__(None, None, None)
            except Exception:
                pass

    await _assert_owned_and_closed(service, task, elapsed)
    read = await _connect(pg_url)
    try:
        job = await read.jobs.get(result["workflow_id"])
        assert job is not None and job.status is GenerationJobStatus.COMPLETED
    finally:
        await read.close()


@pytest.mark.asyncio
async def test_shutdown_drains_running_batch_job_before_close(pg_url: str) -> None:
    llm = FakeLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
        delay=_GRACE_DELAY,
    )
    app, service = _production_app(pg_url, llm)
    lifespan = await _enter_lifespan(app)
    entered = True
    task = None
    try:
        set_id = (await _new_set(service, "Shutdown Batch", ["P1", "P2"]))["id"]
        result, task = await _spawn_job(
            service,
            lambda svc: svc.start_batch_generation(
                set_id=set_id,
                product_ids=["P1", "P2"],
                target_duration_s=600,
                idempotency_key="shutdown-batch",
            ),
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "background batch must still be running before shutdown"
        elapsed = await _exit_lifespan(lifespan)
        entered = False
    finally:
        if entered:
            try:
                await lifespan.__aexit__(None, None, None)
            except Exception:
                pass

    await _assert_owned_and_closed(service, task, elapsed)
    read = await _connect(pg_url)
    try:
        result_pair = await read.batches.get(result["batch_id"])
        assert result_pair is not None
        _batch, state = result_pair
        assert state.status in ("completed", "partial_completed")
    finally:
        await read.close()


@pytest.mark.asyncio
async def test_cancelled_job_is_recoverable_after_restart(pg_url: str) -> None:
    # Short drain grace: the job is guaranteed still running when shutdown
    # begins, so the drain cancels it and the durable row must stay recoverable.
    app, service = _production_app(
        pg_url,
        FakeLlm(
            segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)},
            delay=1.0,
        ),
        script_authoring=ScriptAuthoringConfig(drain_timeout_s=0.2),
    )
    lifespan = await _enter_lifespan(app)
    entered = True
    task = None
    try:
        set_id = (await _new_set(service, "Shutdown Recover", ["P1"]))["id"]
        result, task = await _spawn_job(
            service,
            lambda svc: svc.start_generation(
                set_id=set_id,
                product_id="P1",
                target_duration_s=600,
                intent="selling",
                idempotency_key="shutdown-recover",
            ),
        )
        await asyncio.sleep(0.05)
        assert not task.done(), "background job must still be running before shutdown"
        elapsed = await _exit_lifespan(lifespan)
        entered = False
    finally:
        if entered:
            try:
                await lifespan.__aexit__(None, None, None)
            except Exception:
                pass

    await _assert_owned_and_closed(service, task, elapsed)
    # New work is refused while the service is shut down (admission stopped).
    with pytest.raises(ScriptAuthoringError) as exc_info:
        await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="shutdown-recover-new",
        )
    assert exc_info.value.code == "service_unavailable"

    # The unfinished durable job survives the restart and is recognized by a
    # fresh service over fresh repositories on the SAME database.
    read = await _connect(pg_url)
    try:
        job = await read.jobs.get(result["workflow_id"])
        assert job is not None
        assert job.status is GenerationJobStatus.RUNNING  # cancelled mid-flight, not lost
        service2 = ScriptAuthoringServiceImpl(
            read,
            config=ScriptAuthoringConfig(),
            engine_manager=FakeEngineManager(
                FakeLlm(
                    segment_by_index={
                        0: gate_compliant_text(0, 280),
                        1: gate_compliant_text(280, 280),
                    }
                )
            ),
        )
        again = await service2.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=600,
            intent="selling",
            idempotency_key="shutdown-recover",
        )
        assert again["workflow_id"] == result["workflow_id"]
        assert again.get("idempotent") is True
    finally:
        await read.close()
