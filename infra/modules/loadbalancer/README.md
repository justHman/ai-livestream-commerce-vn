# loadbalancer

Internet-facing ALB + backend target group for ai-livestream MVP.

## Resources

- ALB (internet-facing, public subnets)
- Target group `backend` port 8800, `target_type=ip`, sticky cookie
- Listener: empty `certificate_arn` → HTTP:80; set → HTTPS:443 (+ optional HTTP redirect)
- Optional path-pattern listener rules (stubs)

## Explicit non-goals

- No AWS WAFv2
- No Route53 records (Cloudflare DNS)
- No API Gateway

## Inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | required | |
| `vpc_id` | required | |
| `subnet_ids` | required | ALB needs >=2 AZs |
| `sg_alb_id` | required | security module |
| `certificate_arn` | `""` | empty = HTTP:80 (dev) |
| `backend_port` | `8800` | |
| `health_check_path` | `/health` | |
| `path_rules` | `[]` | optional stubs |

## Outputs

`alb_arn`, `alb_dns_name`, `backend_target_group_arn`, `listener_arn`, `https_enabled`

## Usage

```hcl
module "loadbalancer" {
  source          = "../../modules/loadbalancer"
  env             = var.env
  vpc_id          = module.network.vpc_id
  subnet_ids      = module.network.public_subnet_ids
  sg_alb_id       = module.security.sg_alb_id
  certificate_arn = var.certificate_arn
}
```
