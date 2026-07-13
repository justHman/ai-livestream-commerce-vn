# Release Checklist

## Pre-merge

### Code quality
- [ ] `uv run pytest core/tests/ -v` passes (offline suite)
- [ ] `uv run pytest core/tests/test_engines_endpoint.py` passes (preset registry)
- [ ] `uv run python -m core.tests.v1_smoke_test` passes (sandbox, 0 credits)
- [ ] `uv run python -m providers.liveavatar_cloud.examples.server_ws_smoke_test` passes (legacy, 0 credits)
- [ ] No `liveavatar_api/` path references remain -- all imports use `providers/liveavatar_cloud/`

### Auth check
- [ ] `APP_ENV=prod` + no tokens rejects with 401
- [ ] `CORS_ORIGINS=*` with `APP_ENV=prod` raises RuntimeError at startup
- [ ] WS token validation closes before accept on wrong token
- [ ] `DEBUG_ENABLED=0` -> `/debug/*` returns 404

### Config check
- [ ] `AppConfig.from_env()` reads all expected env vars without KeyError
- [ ] `RENDER_BACKEND=mock` does not require `LIVEAVATAR_API_KEY`
- [ ] `LLM_ENGINE=none TTS_ENGINE=tone` (defaults) boot without GPU

### Docs
- [ ] `docs/architecture.md` reflects current module layout
- [ ] `docs/aws-architecture.md` + pricing match live Seoul stack
- [ ] Active work only in `plans/`; historical drafts only under `archive/docs-historical/`
- [ ] `docs/runbook-colab.md` matches the current bootstrap notebook steps
- [ ] Any new env vars are documented in `core/config.py` docstrings

## Release

- [ ] Branch merged to `main`
- [ ] Changelog entry written
- [ ] Git tag created (`v0.N.0` or similar)
- [ ] Colab bootstrap notebook re-tested from clean runtime
- [ ] ngrok URL verified with `frontend/lite.html`
- [ ] Smoke test passes on sandbox (0 credits used)
