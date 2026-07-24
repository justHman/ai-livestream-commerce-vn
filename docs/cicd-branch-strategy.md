# CI/CD and branch strategy

> Current workflow contract. It describes tracked GitHub Actions files; no
> workflow execution, image publication, or ECS deployment is assumed verified.

## Branches

| Branch | Purpose | Automation |
|---|---|---|
| `feature/*` | Isolated change | CI only |
| `develop` | Integration branch | CI, then conditional DEV deployment |
| `main` | Release source | CI only until a release tag and manual PROD dispatch |
| `hotfix/*` | Production fix | PR to `main`, then back-merge to `develop` |

## CI

`.github/workflows/ci.yml` runs production-source Ruff, `uv sync --frozen
--extra test`, the offline `core/tests/` suite, Terraform formatting plus
`init -backend=false`/`validate` for global, DEV, and PROD, then backend image
build validation. CI never deploys or publishes an image.

## DEV deployment

`deploy-dev.yml` runs on qualifying pushes to `develop`.

1. Uses GitHub OIDC and checks the state bucket and lock table.
2. Reads deployed DEV Terraform outputs instead of a fixed DNS name.
3. Builds `imjusthman/ai-live-backend:dev-<sha>` for ARM64.
4. Builds LLM/TTS, avatar, LiveKit, and LMCache only when Terraform reports a
   positive effective optional desired count.
5. Registers task revisions, updates active services, waits for ECS and ALB
   health, and rolls back task definitions on failure.

Required secrets: `AWS_ROLE_ARN_DEV`, `DOCKERHUB_USER`, and `DOCKERHUB_TOKEN`.
DEV deployment requires already-applied Terraform state; it cannot bootstrap it.

## PROD deployment

`deploy-prod.yml` separates build from deployment:

- A `vMAJOR.MINOR.PATCH` tag reachable from `main` builds six immutable images.
- Tag push does not deploy.
- Deployment requires `workflow_dispatch` from `main`, `confirm_deploy=true`,
  the pre-existing tag, all image manifests, and the `production` Environment.
- The job reads Terraform outputs, deploys with rollback, and checks backend
  target health.

This is a manual live-operation gate, not active release automation.

## Image contract

```text
imjusthman/ai-live-backend   linux/arm64
imjusthman/ai-live-llm       linux/amd64
imjusthman/ai-live-tts       linux/amd64
imjusthman/ai-live-avatar    linux/amd64
imjusthman/ai-live-livekit   linux/arm64
imjusthman/ai-live-lmcache   linux/arm64
```

LLM and TTS remain two images in one EC2 GPU task. Tier S builds/deploys only
the backend because all optional service desired counts are zero.

## Smoke origin

```powershell
$scheme = terraform -chdir=infra/environments/dev output -raw alb_url_scheme
$host = terraform -chdir=infra/environments/dev output -raw alb_dns_name
$base = "$scheme://$host"
```

A Cloudflare hostname is optional and never replaces Terraform output discovery.