# secrets

SSM Parameter Store SecureString placeholders (free Standard tier).

## Resources

Creates SecureString parameters under `/{env}/…` by default:

| Path | Purpose |
|------|---------|
| `/{env}/db/password` | RDS master password |
| `/{env}/redis/auth` | ElastiCache AUTH token |
| `/{env}/livekit/api_key` | LiveKit API key |
| `/{env}/livekit/api_secret` | LiveKit API secret |
| `/{env}/jwt/secret` | Backend JWT signing secret |

- `overwrite = false` on create
- `lifecycle.ignore_changes = [value]` so out-of-band `put-parameter` is not reverted

## Set real values (out-of-band)

```bash
aws ssm put-parameter \
  --name /prod/db/password \
  --type SecureString \
  --value '…' \
  --overwrite \
  --region ap-northeast-2
```

## Explicit non-goals

- No Secrets Manager
- No automatic rotation

## Inputs

| Name | Default |
|------|---------|
| `env` | required |
| `parameters` | map of 5 CHANGE_ME placeholders |
| `ignore_value_changes` | `true` (lifecycle always ignores value) |

## Outputs

`parameter_names`, `parameter_arns`, `parameter_name_map`, `prefix`

## Usage

```hcl
module "secrets" {
  source = "../../modules/secrets"
  env    = var.env
}
```
