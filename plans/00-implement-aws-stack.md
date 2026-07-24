# Plan 00 — AWS stack: offline contracts complete, external operations pending

> Scope: Terraform, Docker, CI/CD, and Tier S preparation on
> `feature/m4-stabilize-deploy`. No external deployment is approved or assumed.

## Delivered offline

| Work package | State |
|---|---|
| Terraform modules | Network, security, compute, database, load balancer, storage, secrets, monitoring exist |
| Environment roots | Global, DEV, and PROD have distinct S3 state keys and validate with backend disabled |
| Tier S | DEV tfvars example keeps backend=1 and all GPU/media optional services at zero |
| Docker | Six service Dockerfile contracts exist; CI builds backend |
| CI | Production Ruff, offline tests, Terraform format/validation, backend build |
| DEV workflow | OIDC/state-output-driven build, deploy, health gate, rollback logic |
| PROD workflow | Tag builds immutable six-image release; manual main-only confirmation deploys |

## External work not verified

- Global remote-state bootstrap/migration and any Terraform plan/apply.
- Docker Hub push, OIDC role assumption, DEV workflow and ECS rollback against
  real services.
- ALB, RDS, Redis, GPU, LiveKit, DNS, and public endpoint smoke.

## Tier S acceptance

An approved Tier S execution must prove:

1. No EC2 capacity and no positive optional desired count.
2. Raw Terraform ALB output returns liveness and readiness.
3. Authenticated session create, attach, plan, chat, and stop complete.
4. Backend logs are captured without secrets.
5. The agreed teardown or scale-down runs within the window.

Use `docs/runbook-live-smoke-and-teardown.md`; do not use legacy images or DNS.

## MVP exclusions

No NAT, private subnet, ECR, AWS WAF, Route53, Secrets Manager, API Gateway,
or weights in Docker layers. Tier S does not validate GPU/media services.
