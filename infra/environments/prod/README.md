# environments/prod

Root Terraform module for **prod** (ap-northeast-2). Same module graph as dev; safer defaults:

- storage `force_destroy=false`, versioning on
- RDS `deletion_protection=true`, final snapshot kept
- ALB deletion protection on
- GPU desired counts default 1 (still budget-sensitive — review before apply)

## Run

```bash
cd infra/environments/prod
cp terraform.tfvars.example terraform.tfvars
export TF_VAR_db_password='...'
terraform init
terraform plan -var-file=terraform.tfvars
# apply only after review
```

## Notes

- Set `certificate_arn` for HTTPS:443 (Cloudflare Full strict).
- Prefer Cloudflare IP allowlist on `alb_ingress_cidrs`.
- OIDC provider is account-wide → `environments/global`, not here.
