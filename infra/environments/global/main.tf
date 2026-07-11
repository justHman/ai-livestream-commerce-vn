# Account-wide bootstrap skeleton.
# - GitHub Actions OIDC provider (once)
# - Optional tfstate bucket + DynamoDB lock table
# Deploy roles / trust policies can be added when CI pipeline lands.

locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = "global"
      Module  = "global"
    },
    var.tags,
  )
}

# ---------------------------------------------------------------------------
# GitHub Actions OIDC (account-wide; enable once)
# ---------------------------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  count = var.enable_github_oidc ? 1 : 0

  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [var.github_oidc_thumbprint]

  tags = merge(local.common_tags, {
    Name = "${var.project}-github-oidc"
  })
}

# ---------------------------------------------------------------------------
# Optional shared remote-state primitives
# ---------------------------------------------------------------------------

resource "aws_s3_bucket" "tfstate" {
  count = var.create_tfstate_bucket ? 1 : 0

  bucket = var.tfstate_bucket_name

  tags = merge(local.common_tags, {
    Name = var.tfstate_bucket_name
  })
}

resource "aws_s3_bucket_versioning" "tfstate" {
  count  = var.create_tfstate_bucket ? 1 : 0
  bucket = aws_s3_bucket.tfstate[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  count  = var.create_tfstate_bucket ? 1 : 0
  bucket = aws_s3_bucket.tfstate[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  count  = var.create_tfstate_bucket ? 1 : 0
  bucket = aws_s3_bucket.tfstate[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tf_lock" {
  count = var.create_tf_lock_table ? 1 : 0

  name         = var.tf_lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = merge(local.common_tags, {
    Name = var.tf_lock_table_name
  })
}
