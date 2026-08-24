# Terraform layout and state bootstrap

> `infra/modules/*` and `infra/environments/{global,dev,staging,prod}` validate
> offline. No environment is assumed applied.

## Layout

```text
infra/
├── modules/
│   ├── network/        # VPC, two public subnets, IGW, S3 gateway endpoint
│   ├── security/       # security groups, IAM and OIDC support
│   ├── compute/        # ECS task definitions, Fargate and optional EC2 capacity
│   ├── database/       # RDS PostgreSQL and ElastiCache Redis
│   ├── loadbalancer/   # ALB and backend target group
│   ├── storage/        # weights, idle frames, replays
│   ├── secrets/        # SSM SecureString parameter placeholders
│   └── monitoring/     # CloudWatch, SNS, billing alarms
└── environments/
    ├── global/         # OIDC and remote-state primitives
    ├── dev/            # DEV root and Tier S example
    ├── staging/        # STAGING root
    └── prod/           # PROD root
```

Only `infra/environments/*` runs Terraform. Modules contain no backend
configuration or environment credentials.

## Remote state

After bootstrap, roots use:

```text
s3://ai-livestream-tfstate-<account-id>/global/terraform.tfstate
s3://ai-livestream-tfstate-<account-id>/dev/terraform.tfstate
s3://ai-livestream-tfstate-<account-id>/prod/terraform.tfstate
DynamoDB table: ai-livestream-tf-lock
```

The global backend cannot exist before bootstrap. Use local state once, then
migrate:

```powershell
Copy-Item infra/environments/global/terraform.tfvars.example infra/environments/global/terraform.tfvars
# In ignored copy: create_tfstate_bucket=true, create_tf_lock_table=true.
terraform -chdir=infra/environments/global init -backend=false
terraform -chdir=infra/environments/global plan -var-file=terraform.tfvars
# Apply only after explicit live-operation confirmation.
# terraform -chdir=infra/environments/global init -migrate-state -force-copy
```

`terraform.tfvars`, local tfvars, state, and plans are ignored. Only example
files are committed.

## Tier S profile

`infra/environments/dev/terraform.tier-s.tfvars.example` keeps the media and
GPU surface inactive:

```text
backend=1; llm=0; tts=0; avatar=0; livekit=0; lmcache=0
create_ec2_capacity=false
RENDER_BACKEND=mock; LLM_ENGINE=none; TTS_ENGINE=tone; SESSION_STORE=memory
```

RDS and Redis remain infrastructure resources. `DATABASE_URL` is not injected
unless an existing SSM parameter ARN is configured, so default Tier S readiness
does not claim runtime persistence.

## Offline validation

```powershell
terraform fmt -check -recursive infra
terraform -chdir=infra/environments/global init -backend=false
terraform -chdir=infra/environments/global validate
terraform -chdir=infra/environments/dev init -backend=false
terraform -chdir=infra/environments/dev validate
terraform -chdir=infra/environments/staging init -backend=false
terraform -chdir=infra/environments/staging validate
terraform -chdir=infra/environments/prod init -backend=false
terraform -chdir=infra/environments/prod validate
```

Offline validation uses `-backend=false`: Terraform parses the configuration,
builds a dependency graph, and validates types/HCL without reading or writing
any remote state, so local checks never require the paid S3 backend to exist.
Remote state (`infra/environments/*/backend.tf`) remains an explicit deployment
concern wired only when live deployment resumes; nothing in this repository's
automation deletes the tfstate bucket.

## MVP exclusions

No NAT, private subnet, Route53, AWS WAF, ECR, or Secrets Manager. Public
subnets do not expose RDS or Redis: SG restrictions and
`publicly_accessible=false` remain required.