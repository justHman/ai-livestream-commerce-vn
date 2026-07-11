data "aws_caller_identity" "current" {}

locals {
  common_tags = merge(
    {
      Project = var.project
      Env     = var.env
      Module  = "storage"
    },
    var.tags,
  )

  bucket_name = var.bucket_name != "" ? var.bucket_name : "${var.project}-${var.env}-assets-${data.aws_caller_identity.current.account_id}"

  # Logical prefixes used by compute entrypoints (one bucket + prefixes).
  prefixes = [
    "weights/",
    "idle-frames/",
    "replays/",
  ]
}

resource "aws_s3_bucket" "assets" {
  bucket        = local.bucket_name
  force_destroy = var.force_destroy

  tags = merge(local.common_tags, {
    Name = local.bucket_name
  })
}

resource "aws_s3_bucket_public_access_block" "assets" {
  bucket = aws_s3_bucket.assets.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "assets" {
  bucket = aws_s3_bucket.assets.id

  versioning_configuration {
    status = var.enable_versioning ? "Enabled" : "Suspended"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "assets" {
  bucket = aws_s3_bucket.assets.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "assets" {
  count  = var.enable_versioning && var.lifecycle_noncurrent_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.assets.id

  rule {
    id     = "expire-noncurrent"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.lifecycle_noncurrent_days
    }
  }
}

# Marker objects so prefixes exist after apply (optional convenience).
resource "aws_s3_object" "prefix_markers" {
  for_each = toset(local.prefixes)

  bucket       = aws_s3_bucket.assets.id
  key          = each.value
  content      = ""
  content_type = "application/x-directory"

  tags = local.common_tags
}
