# Release Checklist

## Pre-merge

### Code quality
- [ ] `uv lock --check` passes.
- [ ] Production-source Ruff gate passes.
- [ ] `uv run pytest core/tests/ -q` passes (offline suite, including LiveKit registry lifecycle).
- [ ] Provider import checks pass without network access.
- [ ] Terraform format and global/DEV/PROD `init -backend=false`/`validate` pass.
- [ ] Provider imports resolve through `backend.application.clients.avatar.liveavatar_sdk`.

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
- [ ] `docs/architecture.md` reflects current module layout and cleanup order.
- [ ] `docs/aws-architecture.md` describes an unverified AWS state until a live smoke exists.
- [ ] Active plans state implemented primitives and remaining external gates.
- [ ] `docs/runbook-colab.md` matches `notebooks/colab_demo.ipynb`.
- [ ] Tier S runbook uses Terraform ALB outputs, not a remembered hostname.
- [ ] New environment variables are documented at their active configuration boundary.

## External release gate

- [ ] Separate explicit approval covers AWS/LiveKit/DNS/release operations.
- [ ] Tier S smoke logs and teardown record exist before GPU/media escalation.
- [ ] Real LiveKit publisher, SFU, and browser subscriber have been observed before media readiness is claimed.
- [ ] Branch merged to `main` and release tag created only after the required gate.
