# AWS deployment architecture

> Design contract for `ap-northeast-2` (Seoul). Terraform, Dockerfiles, and
> workflows validate offline; no AWS environment, DNS name, or public endpoint
> is assumed to exist.

## Chosen MVP topology

| Role | Deployment target | Tier S state |
|---|---|---|
| Backend API | Fargate Spot ARM64 | one task |
| LLM + TTS | one EC2 Spot g6 GPU task, two containers | disabled |
| Avatar | EC2 Spot g4dn GPU task | disabled |
| LiveKit SFU | Fargate Spot ARM64 | disabled |
| Postgres runtime | RDS `db.t4g.medium`, single AZ, gp3 | provisioned, DSN opt-in |
| Session/cache | ElastiCache `cache.t4g.small` | provisioned, unused by memory profile |
| LMCache | EC2 Spot c7g ASG | disabled |

Tier S uses mock render, no LLM, tone TTS, memory sessions, no EC2 capacity,
and zero LLM/TTS/avatar/LiveKit/LMCache desired counts. It establishes the API
and infrastructure path without a GPU bill.

## Fixed decisions

- Two public subnets across AZs, IGW, and S3 Gateway Endpoint; no NAT/private
  subnets.
- Initial smoke uses Terraform ALB output, not a remembered DNS name. Cloudflare
  is optional after ALB health is known.
- Docker Hub public `imjusthman/ai-live-*` images. Weights are fetched from S3
  at start, never baked into layers.
- SSM SecureString for runtime secrets. No Secrets Manager, ECR, Route53, WAF,
  API Gateway, or Multi-AZ RDS in MVP.
- RDS is `publicly_accessible=false`; Redis/GPU ports allow SG traffic only;
  no port 22.

## Control and media boundary

```text
browser -- HTTPS/WS --> ALB --> backend
backend -- control HTTP --> renderer / optional engine services
browser -- WebRTC media --> LiveKit (when enabled)
backend -- PCM audio --> LiveKit (only LIVEKIT_PUBLISH=1 with valid credentials)
avatar -- video track --> LiveKit (not implemented or verified yet)
```

The API backend has a per-session LiveKit audio publisher registry. It connects
once, forwards PCM windows, and cleans up on session stop and app shutdown.
That is not a real SFU/browser media result. Avatar video and idle frames are a
separate milestone.

## Deployment status

| Surface | State |
|---|---|
| Terraform roots/modules | implemented and offline-validated |
| Docker images/workflows | implemented; external build/push/deploy unverified |
| DEV OIDC deploy | workflow present; needs state bootstrap, secrets, and a real run |
| PROD deploy | manual confirmation workflow; not active release automation |
| Tier S apply | not run |
| LiveKit audio/video E2E | not run |

Tier S avoids GPUs but still creates billable ALB, RDS, Redis, and supporting
resources. Consult `aws-pricing-seoul.csv`, set a bounded smoke window, capture
logs, then destroy or scale down according to the Tier S runbook.
