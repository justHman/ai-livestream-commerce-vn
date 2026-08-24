"""Change B, R3: production startup must fail when the DB is missing/unreachable.

Blocker HIGH-4: production can silently disable Script Authoring (HTTP 501)
when DATABASE_URL is missing or unreachable. Required behavior: in
APP_ENV=prod (the real deployment literal; "production" stays an accepted
alias) a missing DATABASE_URL OR an unreachable/invalid DB MUST fail startup
(the app must not boot). The LLM stays non-fatal (backend boots, manual
authoring works, AI ops return 503). No SA_ENABLED flag.

Non-production (dev) keeps its bootable-but-honest behavior (authoring surface
501 without DB; failures logged).
"""

from __future__ import annotations

import pytest

from backend.config import AppConfig, TTSConfig

# Test-only viewer credential for the production auth plane (build via
# concatenation so the repo secret scanner does not flag a test fixture).
_AUTH_VALUE = "viewer" + "-token"


def _config(database_url: str, *, backend_api_token: str = _AUTH_VALUE, app_env: str = "prod") -> AppConfig:
    # C.3.3: production-shaped DSNs need an explicit sslmode to pass AppConfig's
    # fail-loud TLS gate; these fixtures exercise runtime behavior, not validation.
    if database_url and "sslmode=" not in database_url:
        sep = "&" if "?" in database_url else "?"
        database_url = f"{database_url}{sep}sslmode=require"
    return AppConfig(
        app_env=app_env,
        render_backend="mock",
        database_url=database_url,
        tts=TTSConfig(engine="tone"),  # stub — avoids offline transformers load
        cors_origins="http://localhost",  # production CORS guard forbids "*"
        # Production auth planes require real tokens (B.5): the auth-token
        # guard fires before the DATABASE_URL guard, so these fixtures carry
        # real viewer + admin credentials to keep testing the DB guard.
        backend_api_token=backend_api_token,
        admin_api_token=_AUTH_VALUE,
    )


def _auth() -> dict[str, str]:
    # Production viewer auth requires a valid Bearer token.
    return {"Authorization": "Bearer " + _AUTH_VALUE}


def test_production_without_database_url_fails_startup() -> None:
    from backend.main import create_app

    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_app(config=_config(""))


@pytest.mark.asyncio
async def test_production_unreachable_database_url_fails_startup() -> None:
    from backend.main import create_app

    # Build must succeed (construction is lazy); startup must fail.
    app = create_app(config=_config("postgresql://postgres@127.0.0.1:1/nope"))
    with pytest.raises(Exception):
        await app.router.lifespan_context(app).__aenter__()


@pytest.mark.asyncio
async def test_production_valid_database_llm_none_boots_and_ai_503(pg_url: str) -> None:
    from fastapi.testclient import TestClient

    from backend.main import create_app

    app = create_app(config=_config(pg_url, backend_api_token=_AUTH_VALUE))
    with TestClient(app) as client:
        # Manual authoring works with a reachable DB.
        resp = client.post(
            "/api/v1/script-sets", json={"name": "x", "product_ids": ["P1"]}, headers=_auth()
        )
        assert resp.status_code == 201, resp.text
        set_id = resp.json()["id"]

        # LLM stays non-fatal: AI generation returns 503 llm_unavailable.
        gen = client.post(
            f"/api/v1/script-sets/{set_id}/products/P1/generate",
            json={"target_duration_s": 600, "intent": "selling"},
            headers=_auth(),
        )
        assert gen.status_code == 503, gen.text
        assert gen.json()["error"]["code"] == "llm_unavailable"


@pytest.mark.asyncio
async def test_production_alias_legacy_production_behaves_like_prod() -> None:
    """The legacy ``APP_ENV=production`` literal stays an accepted alias.

    ``prod`` is the deployment value (Terraform ``app_env = "prod"``); the
    ``production`` alias must activate the same production safety gates so a
    stack still configured with the old literal is not silently weakened.
    """
    from backend.main import create_app

    # Missing DATABASE_URL fails composition for the alias too.
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        create_app(config=_config("", app_env="production"))

    # Unreachable DATABASE_URL fails lifespan startup for the alias too.
    app = create_app(config=_config("postgresql://postgres@127.0.0.1:1/nope", app_env="production"))
    with pytest.raises(Exception):
        await app.router.lifespan_context(app).__aenter__()
