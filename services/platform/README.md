# Platform runtimes

This directory owns local and sandbox runtime configuration for platform
dependencies. Cloud provisioning remains in `infra/`; product application code
and runtime SQL remain outside these roots.

| Runtime | Canonical path | Local surface |
| --- | --- | --- |
| LiveKit | `services/platform/livekit/` | Pinned upstream Docker wrapper, YAML config, fail-loud entrypoint, validation, smoke |
| LMCache | `services/platform/lmcache/` | Pinned upstream standalone MP Docker wrapper, real health/metrics, smoke |
| Postgres | `services/platform/postgres/` | Official local image notes and smoke command |
| Redis | `services/platform/redis/` | Official local image notes, config, smoke command |

Build commands use the repository root as context:

```bash
docker build -f services/platform/livekit/Dockerfile -t imjusthman/ai-live-livekit:dev .
docker build -f services/platform/lmcache/Dockerfile -t imjusthman/ai-live-lmcache:dev .
```

No `src/`, product API, or vendored upstream source lives under this root.
Upstream ownership is documented per runtime; local runtimes wrap real pinned
upstream images/packages only.

## Offline validation (no Docker required)

```bash
python services/platform/livekit/validate_config.py
```

`core/tests/test_platform_service_roots.py` and the canonical-path tests
assert structural ownership, root-context Docker references, adjacent ignores,
and SQL ownership.

## Managed data

Terraform remains the source of managed RDS and ElastiCache configuration
under `infra/modules/database/`. Managed data resources default **off** in dev
(`create_rds=false` / `create_redis=false`); staging/prod provision
non-public, SG-isolated, authenticated, encrypted RDS/ElastiCache. Backend
SQL lives in `core/sql/runtime_schema.sql` (backend-owned); platform
directories contain no application SQL.