"""HIGH-2 integration: production read model for human review/approval (real PG).

The reviewer flagged that ``GET /script-sets/{id}`` stripped every field a
human reviewer needs (version id / version content / gate state), so an
external production client could not perform the approve flow. These tests
drive the REAL service against a REAL Postgres database (no mocks):

  1. a DRAFT with a version -> read wire exposes ``current_version_id`` +
     ``current_version`` content (exact spoken/display text, source);
  2. an item with NO version yet -> null-safe (no crash);
  3. production E2E: read the exact ``current_version.spoken_text`` via the
     read API, then approve that exact version id and confirm the binding.

The happy path proves "the exact spoken_text the client read is the exact text
that gets approved" by comparing the wire to the stored ``ScriptVersion`` row.
"""

from __future__ import annotations

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
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
async def test_get_script_set_exposes_current_version_id_and_content(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        created = await service.create_script_set(
            name="Read Model", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
        )
        set_id = created["id"]
        DISPLAY = _long_spoken()
        SPOKEN = _long_spoken()
        await service.save_draft(
            set_id=set_id,
            product_id="P1",
            display_text=DISPLAY,
            spoken_text=SPOKEN,
            revision=None,
        )

        wire = await service.get_script_set(set_id=set_id)
        assert wire is not None
        item = wire["items"]["P1"]

        # The read wire must expose the exact version the approve path requires.
        assert item["current_version_id"] is not None
        assert item["current_version"] is not None
        cv = item["current_version"]
        assert cv["id"] == item["current_version_id"]
        assert cv["spoken_text"] == SPOKEN
        assert cv["display_text"] == DISPLAY
        assert cv["source"] == "manual"
        # The stored row carries the same exact text.
        stored = await repos.versions.get(item["current_version_id"])
        assert stored is not None
        assert stored.spoken_text == SPOKEN
        assert stored.display_text == DISPLAY
        # No approval yet.
        assert item["approved_version_id"] is None
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_get_script_set_no_current_version_returns_null(pg_url: str) -> None:
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        created = await service.create_script_set(
            name="Empty Read", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
        )
        wire = await service.get_script_set(set_id=created["id"])
        assert wire is not None
        item = wire["items"]["P1"]
        # DRAFT-stage item with no version yet -> all null, no crash.
        assert item["state"] == "EMPTY"
        assert item["current_version_id"] is None
        assert item["current_version"] is None
        assert item["approved_version_id"] is None
        assert item["gate"] is None
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_production_approve_e2e_via_read_api(pg_url: str) -> None:
    """Production E2E: read current_version via GET, approve the exact id."""
    repos = await _connect(pg_url)
    try:
        service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
        created = await service.create_script_set(
            name="Approve E2E", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
        )
        set_id = created["id"]
        SPOKEN = _long_spoken()
        await service.save_draft(
            set_id=set_id, product_id="P1", display_text=SPOKEN, spoken_text=SPOKEN, revision=None
        )
        submitted = await service.submit_for_gate(set_id=set_id, product_id="P1")
        assert submitted["state"] == "REVIEWABLE"

        # Production read API -> extract current_version_id + spoken_text.
        wire = await service.get_script_set(set_id=set_id)
        assert wire is not None
        item = wire["items"]["P1"]
        cv = item["current_version"]
        assert cv is not None
        assert cv["spoken_text"] == SPOKEN
        assert item["gate"] is not None
        assert item["gate"]["state"] == "passed"

        version_id = item["current_version_id"]
        approved = await service.approve_product(
            set_id=set_id, product_id="P1", version_id=version_id, actor="reviewer"
        )
        assert approved["state"] == "APPROVED"
        assert approved["approval"]["version_id"] == version_id

        item_after = await repos.items.get_by_product(set_id, "P1")
        assert item_after is not None
        assert item_after.approved_version_id == version_id

        # The read wire now reflects the approved binding and the exact text.
        wire2 = await service.get_script_set(set_id=set_id)
        item2 = wire2["items"]["P1"]
        assert item2["approved_version_id"] == version_id
        assert item2["current_version"]["spoken_text"] == SPOKEN
    finally:
        await repos.close()
