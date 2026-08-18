"""ScriptAuthoringServiceImpl multi-product batch integration tests (B6).

Real Postgres repositories + real ``ScriptGate`` + a fake/echo LLM through a
duck-typed EngineManager. Covers start_batch_generation wire + persisted
state, cancel, snapshot, and the SSE event stream.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.models import ScriptState
from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig

from integration.authoring_helpers import FakeEngineManager, FakeLlm, gate_compliant_text


async def _connect(pg_url: str) -> PostgresAuthoringRepositories:
    store = PostgresRuntimeStore(pg_url)
    await store.connect()
    await store.apply_schema()
    repos = PostgresAuthoringRepositories(pg_url)
    await repos.connect()
    return repos


def _good_pair_llm() -> FakeLlm:
    return FakeLlm(
        segment_by_index={0: gate_compliant_text(0, 280), 1: gate_compliant_text(280, 280)}
    )


async def _new_set(service, product_ids):
    return await service.create_script_set(
        name="Set PG", transition_policy="ORDER_AGNOSTIC", product_ids=product_ids, brief=None
    )


async def _wait_for_batch(
    repos: PostgresAuthoringRepositories, batch_id: str, statuses: tuple[str, ...], tries: int = 600
):
    result = await repos.batches.get(batch_id)
    for _ in range(tries):
        result = await repos.batches.get(batch_id)
        if result is not None and result[1].status in statuses:
            return result
        await asyncio.sleep(0.05)
    return result


@pytest.mark.asyncio
async def test_start_batch_generation_wire_and_completion_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(
            repos,
            config=ScriptAuthoringConfig(),
            engine_manager=FakeEngineManager(_good_pair_llm()),
        )
        set_id = (await _new_set(service, ["P1", "P2"]))["id"]
        result = await service.start_batch_generation(
            set_id=set_id, product_ids=["P1", "P2"], target_duration_s=600, idempotency_key="b-1"
        )
        assert result["batch_id"].startswith("batch:")
        assert result["status"] == "queued"
        summary = result["workflow_summary"]
        assert len(summary["products"]) == 2
        assert summary["estimated_semantic_calls_total"] == 4  # 1 planning + K segments per product

        batch, state = await _wait_for_batch(
            repos, result["batch_id"], ("completed", "partial_completed")
        )
        assert state.status == "completed"
        assert state.completed == 2
        item1 = await repos.items.get_by_product(set_id, "P1")
        item2 = await repos.items.get_by_product(set_id, "P2")
        assert item1 is not None and item1.state is ScriptState.REVIEWABLE
        assert item2 is not None and item2.state is ScriptState.REVIEWABLE
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_start_batch_idempotent_duplicate_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(
            repos,
            config=ScriptAuthoringConfig(),
            engine_manager=FakeEngineManager(_good_pair_llm()),
        )
        set_id = (await _new_set(service, ["P1"]))["id"]
        first = await service.start_batch_generation(
            set_id=set_id, product_ids=["P1"], target_duration_s=600, idempotency_key="b-dup"
        )
        second = await service.start_batch_generation(
            set_id=set_id, product_ids=["P1"], target_duration_s=600, idempotency_key="b-dup"
        )
        assert second["batch_id"] == first["batch_id"]
        assert second.get("idempotent") is True
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_cancel_batch_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        # Slow segment LLM keeps the batch running long enough to cancel.
        service = ScriptAuthoringServiceImpl(
            repos,
            config=ScriptAuthoringConfig(),
            engine_manager=FakeEngineManager(
                FakeLlm(
                    segment_by_index={
                        0: gate_compliant_text(0, 280),
                        1: gate_compliant_text(280, 280),
                    },
                    delay=0.2,
                )
            ),
        )
        set_id = (await _new_set(service, ["P1", "P2"]))["id"]
        result = await service.start_batch_generation(
            set_id=set_id,
            product_ids=["P1", "P2"],
            target_duration_s=600,
            idempotency_key="b-cancel",
        )
        batch_id = result["batch_id"]
        cancelled = await service.cancel_batch(set_id=set_id, batch_id=batch_id)
        assert cancelled["status"] == "cancelling"
        batch, state = await _wait_for_batch(repos, batch_id, ("cancelled",))
        assert state.status == "cancelled"
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_batch_snapshot_and_sse_events_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(
            repos,
            config=ScriptAuthoringConfig(),
            engine_manager=FakeEngineManager(_good_pair_llm()),
        )
        set_id = (await _new_set(service, ["P1"]))["id"]
        result = await service.start_batch_generation(
            set_id=set_id, product_ids=["P1"], target_duration_s=600, idempotency_key="b-sse"
        )
        batch_id = result["batch_id"]

        snapshot = await service.get_batch_events_snapshot(set_id=set_id, batch_id=batch_id)
        assert snapshot is not None
        data = json.loads(snapshot)
        assert data["batch_id"] == batch_id
        assert data["set_id"] == set_id
        assert data["revision"] >= 1

        events: list[dict[str, str]] = []
        async for event in service.stream_batch_events(set_id=set_id, batch_id=batch_id):
            events.append(event)
            if event["event"] in ("batch.completed", "batch.cancelled", "batch.error"):
                break
        names = [event["event"] for event in events]
        assert "batch.completed" in names or "batch.cancelled" in names or "batch.error" in names
        # No script text leaks into event payloads (Decision 21).
        text = " ".join(event.get("data", "") for event in events)
        assert "rao bán" not in text
    finally:
        await repos.close()
