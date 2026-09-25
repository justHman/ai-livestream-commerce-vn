"""Real persisted human approval -> binding -> active speech consumption."""

import asyncio
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.application.director.decision import Decision
from backend.main import create_app

from .test_script_authoring_http_approve_pg import _auth, _config


@pytest.mark.asyncio
async def test_persisted_approval_consumed_by_director_and_direct_say(pg_url):
    claim = "Kem dưỡng da giúp làn da mịn màng mỗi ngày."
    spoken = " ".join([claim] * 250)
    config = replace(_config(pg_url), director_enabled=True)
    app = create_app(config=config)
    with TestClient(app) as client:
        created = client.post(
            "/api/v1/script-sets",
            headers=_auth(),
            json={
                "name": "Approved speech",
                "product_ids": ["P1"],
                "brief": {
                    "title": "Approved speech",
                    "tenant_id": "tenant-1",
                    "business_session_id": "business-1",
                    "product_facts_version": "facts-v1",
                    "fact_source": "merchant",
                    "product_facts": {"P1": {"allowed_claims": [claim]}},
                },
            },
        )
        assert created.status_code == 201, created.text
        set_id = created.json()["id"]
        base = f"/api/v1/script-sets/{set_id}/products/P1"
        draft = client.put(
            base + "/draft",
            headers=_auth(),
            json={"display_text": spoken, "spoken_text": spoken},
        )
        assert draft.status_code == 200, draft.text
        submitted = client.post(base + "/submit", headers=_auth())
        assert submitted.status_code == 200, submitted.text
        read = client.get(f"/api/v1/script-sets/{set_id}", headers=_auth()).json()
        version = read["items"]["P1"]["current_version_id"]
        approved = client.post(base + "/approve", headers=_auth(), json={"version_id": version})
        assert approved.status_code == 200, approved.text
        started = client.post(
            "/api/v1/sessions",
            headers=_auth(),
            json={
                "execution_contract": "p0.execution.v1",
                "tenant_id": "tenant-1",
                "business_session_id": "business-1",
                "generation": "generation-1",
            },
        )
        assert started.status_code == 200, started.text
        sid = started.json()["session_id"]
        bound = client.put(
            f"/api/v1/sessions/{sid}/script-set",
            headers=_auth(),
            json={
                "script_set_id": set_id,
                "tenant_id": "tenant-1",
                "business_session_id": "business-1",
            },
        )
        assert bound.status_code == 200, bound.text
        attached = client.post(
            f"/api/v1/sessions/{sid}/attach", headers=_auth(), json={"products": []}
        )
        assert attached.status_code == 200, attached.text
        d = app.state.container

        async def prepare_locked():
            playback = d.coordinator._playback_tasks[sid]
            playback.cancel()
            await asyncio.gather(playback, return_exceptions=True)
            turn = Decision(
                action="introduce_product",
                product_id="P1",
                prompt="ignored raw prompt",
                revision_token=d.director.current_generation_token(sid),
            )
            d.coordinator._decision_queue[sid].append(turn)
            await d.coordinator._prepare_turn(sid, turn)
            assert turn.prepared_script == spoken
            assert turn.approved_speech.product.approved_version_id == version

        client.portal.call(prepare_locked)
        said = client.post(
            f"/api/v1/sessions/{sid}/say",
            headers=_auth(),
            json={"text": claim, "generate": False},
        )
        assert said.status_code == 200, said.text
        assert said.json()["reply"] == claim
        assert said.json()["validation"]["approved_version_id"] == version
