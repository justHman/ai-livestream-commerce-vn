# environments/staging

Root Terraform module for **staging** (ap-northeast-2). Same module graph as dev/prod;
prod-like guards, ephemeral lifecycle:

- storage `force_destroy=false`, versioning on, noncurrent versions expire after 1 day
- RDS backup retention 7 days, final snapshot kept on destroy, deletion protection off
- ALB deletion protection off (staging is torn down regularly)
- Backend runs on-demand Fargate (`spot_capacity_percentage=0`) for stable E2E runs
- No plain-HTTP ALB ingress; HTTPS via `certificate_arn`

## State

Unique state key `staging/terraform.tfstate` in the shared bootstrap bucket.
No Terraform workspaces — environment isolation is backend-config/key per env.

## Run

```bash
cd infra/environments/staging
cp terraform.tfvars.example terraform.tfvars
export TF_VAR_db_password='...'
export TF_VAR_backend_api_token='...'
export TF_VAR_admin_api_token='...'
export TF_VAR_certificate_arn='arn:aws:acm:...'
terraform init
terraform plan -var-file=terraform.tfvars
# apply only after review
```

## Notes

- Set `certificate_arn` for HTTPS:443 (Cloudflare Full strict).
- Prefer Cloudflare IP allowlist on `alb_ingress_cidrs`.
- OIDC provider is account-wide → `environments/global`, not here.
- Secrets are never in tfvars files: pass via `TF_VAR_*` or SSM out-of-band.
