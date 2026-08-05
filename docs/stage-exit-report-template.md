# Stage-exit report

> Copy this template into `.runtime/stage-{N}-<timestamp>/SUMMARY.md` for every
> stage attempt (PASS or FAIL). Reports MUST exclude secrets and Authorization
> values. FAIL reports MUST still include teardown verification after destroy.

## Stage {N} — {Stage name}

- **Profile**: `infra/environments/dev/terraform.{stage-tfvars}.example`
- **Status**: {PASS | FAIL}
- **Operator**: {name}
- **Date (local)**: {YYYY-MM-DD HH:MM}
- **Billable window**: {start → end, local}
- **Estimated cost**: {AWS + LiveAvatar credits, residual ALB/RDS/Redis}
- **Prior stage**: {Stage N-1 PASS report path + teardown-verify.md path, or "n/a — Stage 1"}

## Engine matrix

| Surface | Value |
|---|---|
| `RENDER_BACKEND` | {mock | cloud_liveavatar | self_host_avatarforcing_half} |
| `LLM_ENGINE` | {none | vllm} |
| `LLM_BASE_URL` | {empty | service-discovery DNS} |
| `TTS_ENGINE` | {tone | vieneu} |
| `TTS_BASE_URL` | {empty | service-discovery DNS} |
| `SESSION_STORE` | {memory | redis} |
| `APP_ENV` | {dev} |
| `desired_backend` | {1} |
| `desired_llm/desired_tts` | {0|1} |
| `desired_avatar` | {0|1} |
| `desired_livekit` | {0|1} |
| `desired_lmcache` | {0} |
| `create_ec2_capacity` | {false|true} |
| `LIVEKIT_PUBLISH` | {0|1} |
| `LMCACHE_ENABLED` | {false} |
| Image tags | {imjusthman/ai-live-*:dev-<sha>} |
| Instance types | {engine g6.xlarge, avatar g4dn.xlarge, n/a} |

## Money-safe boot

- [ ] Phase 0 applied with all cost-driving desired/env OFF
- [ ] Stack verified healthy at zero compute cost
- [ ] GitHub Actions SHA images pushed (no `:latest`, no hand-push)
- [ ] S3 weights seeded offline (Qwen3.5-4B-AWQ, VieNeu-TTS from local `.git`, neucodec)
- [ ] SSM secrets present (`LIVEAVATAR_API_KEY` for Stage 2; LiveKit keys for Stage 3)
- [ ] Config validated
- [ ] Phase 1 applied with cost-driving desired counts ON

## Smoke / benchmark results

- **Base URL**: {from terraform output, not a remembered hostname}
- **Endpoints checked**: `/health/live`, `/health/ready`, `/engines`, session lifecycle
- **Stage 2**: sandbox avatar `{dd73ea75-...}` first smoke PASS? real-avatar bench? LLM → TTS → LiveAvatar cloud path? `desired_livekit=0` verified? latency sample?
- **Stage 3**: `self_host_avatarforcing_half` start/speak/stop? avatar video published through LiveKit? bench timings? FE localhost WebRTC check (avatar video visible)?
- **Latency / throughput sample**: {p50/p95/p99 ms, rps, bounded sample}
- **Logs captured under**: `.runtime/stage-{N}-<timestamp>/`

## Teardown action

- **Route**: {full destroy | temporary stop}
- **Command**: {terraform destroy ... | aws ecs update-service --desired-count 0}
- **Time**: {YYYY-MM-DD HH:MM}

## Teardown verification

Paste the output counts (or `empty`) for each check from
`runbook-live-smoke-and-teardown.md` → Teardown verification. Any non-empty
billable resource, leftover RDS snapshot, or S3 noncurrent version = teardown
FAIL.

- ECS RUNNING tasks: {count}
- RDS instances: {count}
- RDS snapshots (manual + automated): {count}
- ElastiCache clusters: {count}
- ALB (env-scoped): {count}
- S3 noncurrent versions: {count}
- S3 delete markers: {count}
- EC2 instances: {count}
- NAT gateways: {count}
- **Verdict**: {TEARDOWN_VERIFIED | TEARDOWN_FAIL}

## Notes

- {What was proven, what failed, root cause if FAIL, follow-up offline fix}
