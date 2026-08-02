# compute

ECS cluster, capacity providers, task definitions, and services for ai-livestream MVP.

## Resources

- ECS cluster + `FARGATE` / `FARGATE_SPOT` capacity
- EC2 Spot ASG + capacity providers (optional skeleton):
  - `g6.xlarge` — LLM+TTS
  - `g4dn.xlarge` — Avatar
  - `c7g.2xlarge` — LMCache (ARM)
- Task definitions: `backend` (Fargate ARM), `llm_tts` (EC2 GPU, 2 containers), `avatar` (EC2 GPU), `lmcache` (EC2 ARM), `livekit` (Fargate ARM)
- Services with `desired_*` vars; `lmcache` default 0 / gated by `lmcache_enabled`
- Minimal IAM execution + task + instance roles (security module owns SGs only)

## GPU note

Only the **llm** container declares `resourceRequirements GPU=1`. TTS shares the same host GPU process-level (no separate GPU claim).

## Explicit non-goals

- No ECR (Docker Hub public images)
- No NAT / private subnets (`assign_public_ip=true`)
- No EKS

## Inputs (key)

| Name | Default | Notes |
|------|---------|-------|
| `subnet_ids` | required | public subnets |
| `sg_map` | required | from security module |
| `image_backend` | `imjusthman/ai-live-backend:latest` | `services/product/backend_service/` image |
| `image_llm` | `imjusthman/ai-live-llm:latest` | `services/product/llm_service/` image |
| `image_tts` | `imjusthman/ai-live-tts:latest` | `services/product/tts_service/` image |
| `image_avatar` | `imjusthman/ai-live-avatar:latest` | `services/product/avatar_service/` image |
| `image_lmcache` | `imjusthman/ai-live-lmcache:latest` | `services/platform/lmcache/` image |
| `image_livekit` | `imjusthman/ai-live-livekit:latest` | `services/platform/livekit/` image |
| `desired_lmcache` | `0` | off by default |
| `lmcache_enabled` | `false` | forces desired 0 when false |
| `backend_target_group_arn` | `""` | wire from loadbalancer |
| `create_ec2_capacity` | `true` | ASG/LT skeleton |

## Outputs

`cluster_name`, `cluster_arn`, `task_definition_arns`, `service_names`, `execution_role_arn`, `task_role_arn`, `capacity_provider_names`

## Usage

```hcl
module "compute" {
  source                   = "../../modules/compute"
  env                      = var.env
  subnet_ids               = module.network.public_subnet_ids
  sg_map                   = module.security.sg_map
  image_backend            = "imjusthman/ai-live-backend:latest"
  image_llm                = "imjusthman/ai-live-llm:latest"
  image_tts                = "imjusthman/ai-live-tts:latest"
  image_avatar             = "imjusthman/ai-live-avatar:latest"
  image_lmcache            = "imjusthman/ai-live-lmcache:latest"
  image_livekit            = "imjusthman/ai-live-livekit:latest"
  lmcache_enabled          = var.lmcache_enabled
  weights_s3_uri           = module.storage.weights_uri
  secrets_arns             = module.secrets.parameter_arns
  backend_target_group_arn = module.loadbalancer.backend_target_group_arn
  assign_public_ip         = true
  log_group_prefix         = "/ecs/${var.project}-${var.env}"
}
```
