# environments/global

Account-wide bootstrap skeleton:

1. **GitHub Actions OIDC provider** (once per account)
2. Optional **tfstate S3 bucket** + **DynamoDB lock table**
3. `us-east-1` provider alias reserved for billing alarms later

## Why separate from dev/prod

OIDC and remote-state primitives are account-scoped. Creating them inside `dev` would couple env lifecycle to global IAM.

## Run

```bash
cd infra/environments/global
cp terraform.tfvars.example terraform.tfvars
terraform init
terraform plan -var-file=terraform.tfvars
```

## Notes

- First-time state bucket creation uses **local** backend, then migrate.
- Deploy IAM roles for OIDC are not fully defined yet (CI task).
- Do not put env VPC/RDS here.
