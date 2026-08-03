# Redis platform ownership

Redis is an optional session-store backend selected with `SESSION_STORE=redis`
and configured through `REDIS_URL`. Managed ElastiCache provisioning remains in
`infra/modules/database/`; local development uses the official Redis image.

## Official local image (dev profile)

```bash
docker run --rm --name ailive-redis -p 6379:6379 \
  redis:7.2.4-alpine@sha256:c8bb255c3559b3e458766db810aa7b3c7af1235b204cfdb304e79ff388fe1a5a
```

The default local URL is `redis://localhost:6379/0`. Managed ElastiCache URL is
derived from the database module output for `SESSION_STORE=redis`.

## Local smoke

```bash
# Requires a running local Redis (above). ASCII PING round-trip.
python - <<'PY'
import socket, sys
s = socket.create_connection(("127.0.0.1", 6379), timeout=3)
s.sendall(b"PING\r\n")
data = s.recv(64)
s.close()
if b"+PONG" not in data:
    sys.exit(f"redis smoke FAIL: {data!r}")
print("redis smoke OK")
PY
```

## Backend sessions only (Task 1.40)

The initial managed ElastiCache exists for **backend session storage only**
(`SESSION_STORE=redis`). No LiveKit, LMCache, model, or media-plane consumer
uses Redis, and no LiveKit/LMCache credentials are stored there. LiveKit Cloud
owns media-plane persistence. Local Redis remains the dev profile.

### Single-node/single-AZ ceiling (cost trade-off, not HA)

Initial staging/prod is single-node `cache.t4g.small`, single AZ. This is an
explicit cost ceiling — it is not an HA claim. Refer to
`docs/redis-split-and-ha-triggers.md` for the evidence triggers that would
justify Multi-AZ or a split.

## Managed ElastiCache (staging/prod) and dev defaults

- Dev: `create_redis=false` by default; memory sessions default; opt-in test
  paths explicit.
- Staging/prod: managed ElastiCache isolated in a dedicated SG with ingress
  from the backend SG only (no public ingress), AUTH/transit-encryption
  settings per MVP design, and the Redis URL is exposed to backend tasks only.
- LMCache never reads/writes ElastiCache — KV lives in its own process memory.
- Managed ElastiCache AUTH/transit encryption require
  `aws_elasticache_replication_group` (see design); MVP uses SG isolation plus
  documented AUTH reservation.

## No product code

No product code or application data schema belongs here.