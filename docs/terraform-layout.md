# Terraform Layout — Root & Child Modules

> Status: **CONFIRMED** (2026-07-11). Style: Root & Child Modules (Standard Module Structure).  
> Companion: `aws-architecture.md` §7.

## 1. Directory tree

```
infra/
├── modules/                              # CHILD MODULES — logic only, no env config
│   ├── network/                          # VPC, 2 public subnets (2 AZ span for ALB/RDS), IGW, S3 Gateway Endpoint
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── README.md
│   ├── security/                         # SGs, OIDC provider, IAM task roles, deploy roles
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── README.md
│   ├── compute/                          # ECS cluster, capacity providers, ASG GPU, task defs, services
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── README.md
│   ├── database/                         # RDS Postgres single-AZ + ElastiCache Redis
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── README.md
│   ├── loadbalancer/                     # ALB + target groups + listeners (no AWS WAF)
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── README.md
│   ├── storage/                          # S3 buckets (weights, idle-frames, replays, tfstate)
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── README.md
│   ├── secrets/                          # SSM Parameter Store SecureString
│   │   ├── main.tf
│   │   ├── variables.tf
│   │   ├── outputs.tf
│   │   └── README.md
│   └── monitoring/                       # CloudWatch log groups, alarms, SNS
│       ├── main.tf
│       ├── variables.tf
│       ├── outputs.tf
│       └── README.md
│
└── environments/                         # ROOT MODULES — execute terraform here
    ├── dev/
    │   ├── backend.tf                    # S3 + DynamoDB lock for dev state
    │   ├── providers.tf                  # AWS provider ap-northeast-2
    │   ├── main.tf                       # module blocks calling ../../modules/*
    │   ├── variables.tf
    │   ├── outputs.tf
    │   └── terraform.tfvars              # dev-specific values
    ├── prod/
    │   ├── backend.tf
    │   ├── providers.tf
    │   ├── main.tf
    │   ├── variables.tf
    │   ├── outputs.tf
    │   └── terraform.tfvars
    └── global/                           # account-wide: OIDC provider, shared IAM, shared S3 tfstate bucket
        ├── backend.tf
        ├── providers.tf
        ├── main.tf
        └── terraform.tfvars
```

## 2. Living rules (Root vs Child)

### Child modules (`modules/*`)
- **No hardcode** of env-specific values (CIDR, instance type, desired_count) → all in `variables.tf`
- **No `provider "aws"`** block — inherits from root
- **No backend** config — never write state from a child
- **Always `outputs.tf`** for IDs/ARNs consumed by siblings (vpc_id, sg_ids, cluster_name)

### Root modules (`environments/{dev,prod,global}`)
- **Only place** you run `terraform init/plan/apply`
- Declares **provider + backend**
- `main.tf` is short: only `module "x" { source = "../../modules/x" ... }` + wire outputs→inputs
- Env values live in `terraform.tfvars` (prod secrets never committed — use `*.tfvars.local` + SSM)

## 3. Root `main.tf` skeleton (prod example)

```hcl
module "network" {
  source     = "../../modules/network"
  env        = var.env
  cidr_block = var.vpc_cidr          # e.g. 10.20.0.0/16
  az         = "ap-northeast-2a"     # primary pin (workload)
  # az_b / public_subnet_cidr_b → second public AZ for ALB/RDS only
}

module "security" {
  source   = "../../modules/security"
  env      = var.env
  vpc_id   = module.network.vpc_id
  # SG matrix: alb / backend / llm / tts / avatar / rds / redis / lmcache / livekit
}

module "storage" {
  source = "../../modules/storage"
  env    = var.env
}

module "secrets" {
  source = "../../modules/secrets"
  env    = var.env
  # params created empty; values set out-of-band via aws ssm put-parameter
}

module "database" {
  source                 = "../../modules/database"
  env                    = var.env
  subnet_ids             = module.network.public_subnet_ids
  rds_sg_id              = module.security.sg_rds_id
  redis_sg_id            = module.security.sg_redis_id
  publicly_accessible    = false   # IRON: even in public subnet, no public IP
  instance_class         = "db.t4g.medium"
  allocated_storage_gb   = 100
  multi_az               = false
}

module "loadbalancer" {
  source            = "../../modules/loadbalancer"
  env               = var.env
  vpc_id            = module.network.vpc_id
  subnet_ids        = module.network.public_subnet_ids
  sg_alb_id         = module.security.sg_alb_id
  # Cloudflare terminates public DNS; ALB origin cert / ACM cert
}

module "compute" {
  source              = "../../modules/compute"
  env                 = var.env
  subnet_ids          = module.network.public_subnet_ids
  sg_map              = module.security.sg_map
  image_backend       = "imjusthman/ai-live-backend:latest"
  image_llm           = "imjusthman/ai-live-llm:latest"
  image_tts           = "imjusthman/ai-live-tts:latest"
  image_avatar        = "imjusthman/ai-live-avatar:latest"
  image_lmcache       = "imjusthman/ai-live-lmcache:latest"
  lmcache_enabled     = var.lmcache_enabled
  weights_s3_uri      = module.storage.weights_uri
  secrets_arns        = module.secrets.parameter_arns
  assign_public_ip    = true   # public subnet, no NAT
}

module "monitoring" {
  source = "../../modules/monitoring"
  env    = var.env
  # billing alarms $50 / $100, ALB 5xx, GPU cache, RDS connections
}
```

## 4. How to run

```bash
# one-time global (OIDC provider, shared state bucket)
cd infra/environments/global
terraform init && terraform apply

# dev
cd infra/environments/dev
terraform init
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars

# prod (manual, after review)
cd infra/environments/prod
terraform init
terraform plan -var-file=terraform.tfvars
terraform apply -var-file=terraform.tfvars
```

State:
- `s3://ai-livestream-tfstate-<account-id>/dev/terraform.tfstate`
- `s3://ai-livestream-tfstate-<account-id>/prod/terraform.tfstate`
- DynamoDB lock table `ai-livestream-tf-lock`

## 5. Explicit non-goals in modules

- No Route53 module (Cloudflare DNS)
- No WAF module (Cloudflare Free edge)
- No NAT / private subnet (public-subnet design)
- No ECR module (Docker Hub public images)
- No Secrets Manager module (SSM Parameter Store free)
