# security

Security groups + optional GitHub OIDC for ai-livestream MVP.

## SG matrix

| SG | Inbound | Outbound |
|----|---------|----------|
| `sg-alb` | 443 from `alb_ingress_cidrs` | 8800→backend, 443→livekit |
| `sg-backend` | 8800 from alb | 5432 rds, 6379 redis, 8001 llm, 8002 tts, 8080 avatar, 80/443 internet |
| `sg-llm` | 8001 from backend | 443 internet, 5555→lmcache |
| `sg-tts` | 8002 from backend | 443 internet |
| `sg-avatar` | 8080 from backend | 443, LiveKit TCP/UDP |
| `sg-rds` | 5432 from backend only | none |
| `sg-redis` | 6379 from backend only | none |
| `sg-lmcache` | 5555 from llm only | none |
| `sg-livekit` | 443 from alb; UDP 50000-60000 from 0.0.0.0/0 | UDP media + 443 |

Iron rules: no port 22; no public on DB/Redis/GPU control ports.

## Inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | required | |
| `vpc_id` | required | from network module |
| `alb_ingress_cidrs` | `["0.0.0.0/0"]` | set Cloudflare IPs in prod |
| `enable_oidc` | `false` | account-wide; usually global env |

## Outputs

Individual `sg_*_id`, `sg_map`, optional `github_oidc_provider_arn`.

## Usage

```hcl
module "security" {
  source             = "../../modules/security"
  env                = var.env
  vpc_id             = module.network.vpc_id
  alb_ingress_cidrs  = var.cloudflare_cidrs
  enable_oidc        = false
}
```
