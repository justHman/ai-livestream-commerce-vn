# lmcache — `imjusthman/ai-live-lmcache`

Optional LMCache server on **c7g.2xlarge Spot ARM**. Stateful warm KV in RAM — not Fargate.

| Item | Value |
|------|-------|
| Ports | ZMQ `5555`, metrics `8080` |
| Health | `GET :8080/metrics` |
| Arch | `linux/arm64` |
| Weights | none (KV is runtime memory) |
| Toggle | `LMCACHE_ENABLED` → ECS `desired_count` 0/1 |

## `LMCACHE_ENABLED` → `desired_count` wiring

Terraform (`infra/modules/compute`):

- Variable `lmcache_enabled` (bool) drives local `lmcache_desired`:
  - `true` → `desired_lmcache` (default capacity, usually 1)
  - `false` → **0** (service stays registered, no Spot capacity)
- Backend task env also gets `LMCACHE_ENABLED=<bool>` so app config
  (`AppConfig.lmcache_enabled` / env `LMCACHE_ENABLED`) matches infra.
- LLM task should only set vLLM `--kv-transfer-config` / `LMCACHE_CONFIG_FILE`
  when the flag is true; otherwise leave KV local.

Ops:

1. Keep `LMCACHE_ENABLED=false` for first DEV deploy (saves Spot + cold start risk).
2. Flip flag in env tfvars / SSM → apply compute → ASG scales lmcache capacity provider.
3. Spot reclaim wipes warm KV → capacity-rebalance + accept cold miss (documented).

## Build

```bash
docker build -f services/lmcache/Dockerfile -t imjusthman/ai-live-lmcache:dev .
```

## Run

```bash
docker run --rm -p 8080:8080 -p 5555:5555 imjusthman/ai-live-lmcache:dev
```

## Notes

- Skeleton serves Prometheus-ish `/metrics` until real `lmcache-server` binary is installed.
- ASG capacity-rebalance required (cache wipe on Spot reclaim).
