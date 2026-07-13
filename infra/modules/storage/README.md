# storage

Single S3 assets bucket with prefixes for weights / idle-frames / replays.

## Resources

- S3 bucket (default name: `{project}-{env}-assets-{account_id}`)
- Public access block (all four blocks ON)
- BucketOwnerEnforced ownership (no ACLs)
- SSE-S3 AES256
- Optional versioning + noncurrent expiration
- Prefix marker objects: `weights/`, `idle-frames/`, `replays/`

## Explicit non-goals

- No tfstate bucket here (lives in `environments/global`)
- No CloudFront
- No public website hosting

## Inputs

| Name | Default | Notes |
|------|---------|-------|
| `env` | required | |
| `bucket_name` | `""` | auto `{project}-{env}-assets-{account}` |
| `enable_versioning` | `false` | optional |
| `force_destroy` | `false` | dev only |

## Outputs

`bucket_id`, `bucket_arn`, `weights_uri`, `idle_frames_uri`, `replays_uri`

## Usage

```hcl
module "storage" {
  source = "../../modules/storage"
  env    = var.env
}
```
