"""ScriptAuthoringServiceImpl integration tests (Change B, B4) against real PG.

RED before ``application/script_authoring/service_impl.py`` exists: imports
fail. GREEN once the concrete zero-LLM service drives the SQL repositories
end-to-end: create/get/update set, save_draft -> submit -> approve against the
real ``ScriptGate``, preview, and llm_unavailable stubs.
"""

from __future__ import annotations

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig


async def _connect(pg_url: str) -> PostgresAuthoringRepositories:
    store = PostgresRuntimeStore(pg_url)
    await store.connect()
    await store.apply_schema()
    repos = PostgresAuthoringRepositories(pg_url)
    await repos.connect()
    return repos


def _long_spoken() -> str:
    """A script whose estimated spoken duration lands inside [300, 3600]s."""
    sentence = "Kem dưỡng da này giúp làn da mịn màng và tươi sáng mỗi ngày."
    return " ".join([sentence] * 200)


@pytest.mark.asyncio
async def test_create_get_update_script_set_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        created = await service.create_script_set(
            name="Set PG",
            transition_policy="ORDER_AWARE",
            product_ids=["P1", "P2"],
            brief={"title": "T", "shop_name": "Shop PG"},
        )
        assert created["items"] == {"P1": {"state": "EMPTY"}, "P2": {"state": "EMPTY"}}
        fetched = await service.get_script_set(set_id=created["id"])
        assert fetched is not None
        assert fetched["revision"] == 0
        assert fetched["name"] == "Set PG"
        updated = await service.update_script_set(
            set_id=created["id"],
            name="Set PG 2",
            transition_policy="ORDER_AGNOSTIC",
            product_ids=["P1", "P2", "P3"],
            brief=None,
            revision=0,
        )
        assert updated is not None
        assert updated["revision"] == 1
        assert updated["items"]["P3"]["state"] == "EMPTY"
        with pytest.raises(ScriptAuthoringError) as exc:
            await service.update_script_set(
                set_id=created["id"],
                name="x",
                transition_policy=None,
                product_ids=None,
                brief=None,
                revision=0,
            )
        assert exc.value.code == "stale_revision"
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_save_draft_submit_approve_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        created = await service.create_script_set(
            name="Set", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
        )
        draft = await service.save_draft(
            set_id=created["id"],
            product_id="P1",
            display_text="Kem tốt",
            spoken_text=None,
            revision=None,
        )
        assert draft["state"] == "DRAFT"

        # A short draft fails the real Full Script Gate (SPEECH_DURATION_TOTAL).
        submitted = await service.submit_for_gate(set_id=created["id"], product_id="P1")
        assert submitted["state"] == "GATE_FAILED"
        assert submitted["gate"]["state"] == "gate_failed"

        # A long compliant draft passes the real gate -> REVIEWABLE.
        await service.save_draft(
            set_id=created["id"],
            product_id="P1",
            display_text=_long_spoken(),
            spoken_text=_long_spoken(),
            revision=None,
        )
        submitted = await service.submit_for_gate(set_id=created["id"], product_id="P1")
        assert submitted["state"] == "REVIEWABLE"
        assert submitted["gate"]["state"] == "passed"

        item = await repos.items.get_by_product(created["id"], "P1")
        assert item is not None
        version_id = item.current_version_id
        approved = await service.approve_product(
            set_id=created["id"], product_id="P1", version_id=version_id, actor="nam"
        )
        assert approved["state"] == "APPROVED"
        assert approved["approval"]["version_id"] == version_id
        assert approved["approval"]["actor"] == "nam"

        recorded = await repos.approvals.recorded_dependencies(item.id)
        assert recorded["rule_set"]
        assert len(await repos.gate_runs.list_by_item(item.id)) >= 2
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_preview_product_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        created = await service.create_script_set(
            name="Set", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
        )
        preview = await service.preview_product(
            set_id=created["id"], product_id="P1", target_duration_s=600
        )
        assert preview["estimated_semantic_calls"] == 1 + preview["planned_segment_count"]
        with pytest.raises(ScriptAuthoringError) as exc:
            await service.preview_product(
                set_id=created["id"], product_id="P1", target_duration_s=60
            )
        assert exc.value.code == "illegal_transition"
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_ai_stubs_raise_llm_unavailable_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        with pytest.raises(ScriptAuthoringError) as exc:
            await service.start_generation(
                set_id="s",
                product_id="p",
                target_duration_s=600,
                intent="selling",
                idempotency_key="k",
            )
        assert exc.value.code == "llm_unavailable"
    finally:
        await repos.close()
