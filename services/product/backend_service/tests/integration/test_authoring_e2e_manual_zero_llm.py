"""E2E manual zero-LLM authoring against real PG (Change B, B8).

Drives the FULL manual path with ``engine_manager=None`` (zero LLM):
create set -> save_draft -> submit_for_gate (real Full Script Gate) ->
approve_product. Asserts EXACT text binding: the approved version row carries
the exact saved ``spoken_text`` and the approval row binds the exact version id.
"""

from __future__ import annotations

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.models import ScriptState
from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
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
async def test_manual_zero_llm_full_path_pg(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        # engine_manager defaults to None => zero LLM; all steps are manual.
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        created = await service.create_script_set(
            name="Set E2E",
            transition_policy="ORDER_AGNOSTIC",
            product_ids=["P1"],
            brief=None,
        )
        set_id = created["id"]

        DISPLAY = _long_spoken()
        SPOKEN = _long_spoken()
        draft = await service.save_draft(
            set_id=set_id,
            product_id="P1",
            display_text=DISPLAY,
            spoken_text=SPOKEN,
            revision=None,
        )
        assert draft["state"] == "DRAFT"

        submitted = await service.submit_for_gate(set_id=set_id, product_id="P1")
        assert submitted["state"] == "REVIEWABLE"
        assert submitted["gate"]["state"] == "passed"

        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        assert item.state is ScriptState.REVIEWABLE
        version_id = item.current_version_id
        assert version_id is not None

        approved = await service.approve_product(
            set_id=set_id, product_id="P1", version_id=version_id, actor="admin"
        )
        assert approved["state"] == "APPROVED"
        assert approved["approval"]["version_id"] == version_id

        item = await repos.items.get_by_product(set_id, "P1")
        assert item is not None
        assert item.state is ScriptState.APPROVED
        assert item.approved_version_id == version_id

        # EXACT text binding: the approved version row carries the saved text.
        version = await repos.versions.get(item.approved_version_id)
        assert version is not None
        assert version.spoken_text == SPOKEN

        approval = await repos.approvals.get_by_item(item.id)
        assert approval is not None
        assert approval.script_version_id == version_id
    finally:
        await repos.close()
