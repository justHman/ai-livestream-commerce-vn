# Redis split / Multi-AZ evidence triggers

Applies to the initial managed ElastiCache deployment (back end sessions only).
The initial configuration is **single node, single AZ** (`cache.t4g.small`,
`num_cache_nodes = 1`). This is an explicit **cost trade-off**, not an HA
claim. Multi-AZ or a backend/Redis split is not enabled now and must not be
speculatively provisioned.

## Current ceiling (explicit)

- Single node `cache.t4g.small`, single AZ, no replicas.
- Sole consumer: backend session store (`SESSION_STORE=redis`) — LiveKit
  Cloud owns media persistence; LMCache/LLM/avatar never connect.
- Purpose: cross-task backend session metadata (status, mode, lightweight
  session state) sticky over multiple backend tasks.

## Measurable triggers

Upgrade (Multi-AZ or split) is justified only when **evidence** crosses any
threshold below, with the named decision owner signing off.

| # | Trigger | Measurable evidence | Threshold | Decision owner |
|---|---------|---------------------|-----------|----------------|
| 1 | Availability | Session-store reads/writes return errors or timeouts | Error rate > 0.1% over 1h, or any 5-min availability < 99.5%, twice in 24h | Backend on-call |
| 2 | Paid load | ElastiCache CPUUtilization / redis.used_memory over the node | `CPUUtilization > 80%` sustained 15 min on `cache.t4g.small` | Backend + Infra |
| 3 | Capacity | `redis.used_memory` % of `maxmemory`; evicted_keys rate | Memory > 80% or eviction rate > 0 sustained 30 min | Backend + Infra |
| 4 | Noisy neighbor | A second project/workload lands on the same Redis cluster | Any co-tenant created | Infra owner |
| 5 | Multi-AZ need | Backend requires session reads surviving a single-AZ loss | Documented prod SLO requiring AZ-level Redis HA | Product owner |

## Options after a trigger

1. **Vertical scale** first (`cache.r6g.large`) — cheapest, keeps single node.
2. **Multi-AZ with replicas** — requires switching to
   `aws_elasticache_replication_group` (also unlocks AUTH + transit
   encryption) with `num_cache_clusters = 2` (one replica across AZ).
3. **Split** — separate ElastiCache clusters per consumer only if a new
   non-backend consumer appears. Today the backend is the sole consumer; a
   split is not warranted.

## What stays out of scope

- No Multi-AZ is enabled now (recurring cost).
- No speculative extra nodes until a trigger above is real and measured.
- LiveKit media persistence stays in LiveKit Cloud, never Redis.