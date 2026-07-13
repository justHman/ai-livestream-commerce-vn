# environments/dev

Root Terraform module for the **dev** stack (ap-northeast-2).

## Layout

| File | Role |
|------|------|
| `backend.tf` | S3+DDB remote state (commented until bootstrap) |
| `providers.tf` | AWS provider `ap-northeast-2` |
| `main.tf` | module wiring |
| `variables.tf` / `outputs.tf` | root I/O |
| `terraform.tfvars.example` | non-secret sample values |

## Module order

`network` → `security` → `storage` → `secrets` → `database` → `loadbalancer` → `compute` → `monitoring`

## Bootstrap remote state

```bash
# once per account (or via environments/global)
aws s3api create-bucket --bucket ai-livestream-tfstate --region ap-northeast-2 \
  --create-bucket-configuration LocationConstraint=ap-northeast-2
aws s3api put-bucket-versioning --bucket ai-livestream-tfstate \
  --versioning-configuration Status=Enabled
aws dynamodb create-table --table-name ai-livestream-tf-lock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region ap-northeast-2
# then uncomment backend "s3" in backend.tf
```

## Run

```bash
cd infra/environments/dev
cp terraform.tfvars.example terraform.tfvars
export TF_VAR_db_password='your-strong-password'
terraform init
terraform plan -var-file=terraform.tfvars
# terraform apply -var-file=terraform.tfvars
```

## Apply notes

1. Network module creates **2 public subnets** (AZ a+b) for ALB/RDS. Workload still pins primary AZ.
2. **Billing alarms** disabled by default (metric only in us-east-1).
3. API-only smoke: `create_ec2_capacity=false`, GPU/livekit/lmcache desired **0**.

## Explicit non-goals

No NAT, private subnets, ECR, WAF, Route53, Secrets Manager.
