# Platform runtimes

This directory owns local and sandbox runtime configuration for platform dependencies. Cloud provisioning remains in `infra/`; product application code and runtime SQL remain outside these roots.

| Runtime | Canonical path | Local surface |
| --- | --- | --- |
| LiveKit | `services/platform/livekit/` | Docker wrapper, YAML config, entrypoint |
| LMCache | `services/platform/lmcache/` | Docker wrapper, entrypoint, health/metrics surface |
| Postgres | `services/platform/postgres/` | Usage notes and smoke command |
| Redis | `services/platform/redis/` | Local configuration pointer and smoke command |

Build commands use the repository root as context:

```bash
docker build -f services/platform/livekit/Dockerfile -t imjusthman/ai-live-livekit:dev .
docker build -f services/platform/lmcache/Dockerfile -t imjusthman/ai-live-lmcache:dev .
```

Terraform remains the source of managed RDS and ElastiCache configuration under `infra/modules/database/`. Platform directories do not copy backend SQL or business data schemas.
