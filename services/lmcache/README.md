# lmcache — `justhman/ai-live-lmcache`

Optional LMCache server on **c7g.2xlarge Spot ARM**. Stateful warm KV in RAM — not Fargate.

| Item | Value |
|------|-------|
| Ports | ZMQ `5555`, metrics `8080` |
| Health | `GET :8080/metrics` |
| Arch | `linux/arm64` |
| Weights | none (KV is runtime memory) |
| Toggle | `LMCACHE_ENABLED` → ECS `desired_count` 0/1 |

## Build

```bash
docker build -f services/lmcache/Dockerfile -t justhman/ai-live-lmcache:dev .
```

## Run

```bash
docker run --rm -p 8080:8080 -p 5555:5555 justhman/ai-live-lmcache:dev
```

## Notes

- Skeleton serves Prometheus-ish `/metrics` until real `lmcache-server` binary is installed.
- ASG capacity-rebalance required (cache wipe on Spot reclaim).
