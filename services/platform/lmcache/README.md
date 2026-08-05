# lmcache — `imjusthman/ai-live-lmcache`

Pinned upstream **LMCache standalone multiprocess server** (real runtime), not a
skeleton. Default `LMCACHE_ENABLED=false` and desired count zero — no paid
compute by default.

| Item | Value |
|------|-------|
| Upstream | `lmcache==0.5.2` (PyPI 2026-07-30, requires_python `>=3.10,<3.14`) |
| Command | `lmcache server` (ZMQ + FastAPI HTTP frontend) |
| ZMQ | `tcp://:5555` (LLM KV transfer, SG from llm only) |
| HTTP | `:8080` — real `/healthcheck` (`503` until engine init, `200` ready), `/metrics` (Prometheus) |
| Arch | `linux/amd64` (upstream ships cp311 manylinux x86_64 wheels only) |
| Toggle | `LMCACHE_ENABLED` → ECS `desired_count` 0/1; default **0** |

## Health/metrics contract

Real upstream endpoints, no project synthetic `metrics_app.py` or fallback
process. `/healthcheck` returns `503` until the engine is initialized and
`200` once ready; `/metrics` emits upstream Prometheus metrics. The project
`metrics_app.py` that faked `lmcache_up 1` was removed.

## Build

```bash
docker build -f services/platform/lmcache/Dockerfile -t imjusthman/ai-live-lmcache:dev .
```

Missing package/binary or a wheel build failure FAILS the build (no `|| true`,
no best-effort install).

## Run

```bash
docker run --rm -p 8080:8080 -p 5555:5555 imjusthman/ai-live-lmcache:dev
```

## Non-GPU health/metrics smoke (requires running local process)

```bash
curl -fsS http://127.0.0.1:8080/healthcheck   # 200 = ready
curl -fsS http://127.0.0.1:8080/metrics | grep -m1 "^# HELP"   # real Prometheus text
```

`lmcache ping kvcache --url http://localhost:8080` is the upstream CLI to
verify the KV-cache endpoint.

## GPU benchmark (Task 1.38)

The benchmark entrypoint is preserved for the authorized Task 1.38 run; this
task does **not** enable or run LMCache on a GPU.

## Terraform

`lmcache_enabled=false` and `desired_lmcache=0` by default in dev/prod
tfvars; the compute module keeps the service registered at zero capacity.
LMCache has **no** connection to the managed Redis — backend sessions only
(Task 1.40).