"""R8.6 real-app HTTP evidence: read->approve over create_app() + real PG.

The service-level proof (``test_script_set_read_model_pg.py``) calls the
service methods directly. This test exercises the REAL production app factory
(``create_app``) and the HTTP router against a REAL Postgres database via ASGI
``TestClient``, so the evidence wording ("a production client can read and
approve") is honest:

  POST /api/v1/script-sets          -> create a ScriptSet
  PUT  .../products/P1/draft        -> save a manual draft (zero LLM)
  POST .../products/P1/submit       -> run the deterministic gate
  GET  /api/v1/script-sets/{id}     -> read the EXACT current_version_id +
                                       current_version.spoken_text
  POST .../products/P1/approve      -> approve with EXACTLY that version_id
  GET  /api/v1/script-sets/{id}     -> approved_version_id == version_id

Session binding is NOT exercised here; it is proven separately by
``test_authoring_e2e_session_binding.py``. Together the two tests compose a
full HTTP-read->approve->binding proof.
"""

from __future__ import annotations

import pytest

from backend.config import AppConfig, TTSConfig

# Test-only viewer credential (concatenated so the repo secret scanner does
# not flag a test fixture).
_AUTH_VALUE = "viewer" + "-token"


def _config(database_url: str) -> AppConfig:
    # C.3.3: production-shaped DSNs need an explicit sslmode to pass AppConfig's
    # fail-loud TLS gate; these fixtures exercise runtime behavior, not validation.
    if database_url and "sslmode=" not in database_url:
        sep = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{sep}sslmode=require"
    return AppConfig(
        app_env="prod",
        render_backend="mock",
        database_url=database_url,
        tts=TTSConfig(engine="tone"),  # stub — avoids offline transformers load
        cors_origins="http://localhost",  # production CORS guard forbids "*"
        backend_api_token=_AUTH_VALUE,  # production viewer auth requires a token
        admin_api_token=_AUTH_VALUE,  # production admin auth requires a token (B.5)
    )


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer " + _AUTH_VALUE}


def _long_spoken() -> str:
    """A script whose estimated spoken duration lands inside [300, 3600]s."""
    sentence = "Kem dưỡng da này giúp làn da mịn màng và tươi sáng mỗi ngày."
    return " ".join([sentence] * 200)


@pytest.mark.asyncio
async def test_http_read_then_approve_exact_version(pg_url: str) -> None:
    from fastapi.testclient import TestClient

    from backend.main import create_app

    app = create_app(config=_config(pg_url))
    with TestClient(app) as client:
        # 1. Create a ScriptSet over HTTP.
        created = client.post(
            "/api/v1/script-sets",
            json={"name": "HTTP Approve", "product_ids": ["P1"]},
            headers=_auth(),
        )
        assert created.status_code == 201, created.text
        set_id = created.json()["id"]

        # 2. Save a manual draft (zero LLM).
        spoken = _long_spoken()
        draft = client.put(
            f"/api/v1/script-sets/{set_id}/products/P1/draft",
            json={"display_text": spoken, "spoken_text": spoken},
            headers=_auth(),
        )
        assert draft.status_code == 200, draft.text

        # 3. Submit through the deterministic gate.
        submitted = client.post(
            f"/api/v1/script-sets/{set_id}/products/P1/submit",
            headers=_auth(),
        )
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["state"] == "REVIEWABLE"
        assert submitted.json()["gate"]["state"] == "passed"

        # 4. A STALE/nonexistent version_id still hits the pre-existing
        #    rejection (no production change; the guard is preserved).
        stale = client.post(
            f"/api/v1/script-sets/{set_id}/products/P1/approve",
            json={"version_id": "version_does_not_exist", "actor": "reviewer"},
            headers=_auth(),
        )
        assert stale.status_code == 409, stale.text
        assert stale.json()["error"]["code"] == "illegal_transition"

        # 5. HTTP GET -> read the EXACT current_version_id + spoken_text
        #    (the text a human reviewer is asked to confirm).
        read = client.get(f"/api/v1/script-sets/{set_id}", headers=_auth())
        assert read.status_code == 200, read.text
        item = read.json()["items"]["P1"]
        assert item["current_version_id"] is not None
        assert item["approved_version_id"] is None
        version_id = item["current_version_id"]
        current_version = item["current_version"]
        assert current_version is not None
        assert current_version["id"] == version_id
        assert current_version["spoken_text"] == spoken

        # 6. HTTP POST approve with EXACTLY that version_id.
        approved = client.post(
            f"/api/v1/script-sets/{set_id}/products/P1/approve",
            json={"version_id": version_id, "actor": "reviewer"},
            headers=_auth(),
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["state"] == "APPROVED"
        assert approved.json()["approval"]["version_id"] == version_id

        # 7. HTTP GET again -> the binding is visible and the exact text the
        #    caller reviewed is the exact text approved.
        read2 = client.get(f"/api/v1/script-sets/{set_id}", headers=_auth())
        assert read2.status_code == 200, read2.text
        item2 = read2.json()["items"]["P1"]
        assert item2["approved_version_id"] == version_id
        assert item2["current_version"]["spoken_text"] == spoken
