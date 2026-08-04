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

# ---------------------------------------------------------------------------
# GitHub Actions deploy role (develop branch → dev env)
# OIDC trust: repo:justHman/ai-livestream-commerce-vn:ref:refs/heads/develop
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "github_oidc_assume" {
  count = var.enable_github_oidc ? 1 : 0

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github[0].arn]
    }
    # Exact repo only — rejects forks and any other repository. Environment
    # condition matches the protected GitHub Environment (dev/staging/prod).
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:justHman/ai-livestream-commerce-vn:environment:${var.github_environment}"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "github_deploy_dev" {
  count              = var.enable_github_oidc ? 1 : 0
  name               = "${var.project}-github-deploy-dev"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_assume[0].json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "github_deploy_dev" {
  statement {
    effect = "Allow"
    actions = [
      "ecs:UpdateService",
      "ecs:DescribeServices",
      "ecs:DescribeTasks",
      "ecs:ListTasks",
      "ecs:DescribeTaskDefinition",
      "ecs:RegisterTaskDefinition",
      "ecs:RunTask",
      "ecs:StopTask",
    ]
    resources = ["*"]
  }
  # iam:PassRole limited to the exact execution/task roles this env deploys.
  statement {
    effect = "Allow"
    actions = [
      "iam:PassRole",
    ]
    resources = [
      "arn:aws:iam::*:role/${var.project}-${var.github_environment}-*",
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
  statement {
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = ["arn:aws:ssm:*:*:parameter/dev/*"]
  }
  statement {
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups", "logs:DescribeLogStreams"]
    resources = ["*"]
  }
  # Terraform state bucket: preflight verifies it, terraform init/output reads
  # state objects. Scoped to the exact dev state key prefix (native S3
  # lockfiles: .tflock objects live beside the state key).
  statement {
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
    resources = ["arn:aws:s3:::${var.tfstate_bucket_name}"]
  }
  statement {
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:HeadObject",
    ]
    resources = [
      "arn:aws:s3:::${var.tfstate_bucket_name}/${var.github_environment}/*",
    ]
  }
  # DEV assets bucket: seed-weights workflow uploads model weights to
  # s3://ai-livestream-dev-assets-<acct>/weights/. Scoped to the dev env bucket.
  statement {
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      "arn:aws:s3:::ai-livestream-dev-assets-*",
      "arn:aws:s3:::ai-livestream-dev-assets-*/*",
    ]
  }
}

resource "aws_iam_role_policy" "github_deploy_dev" {
  count  = var.enable_github_oidc ? 1 : 0
  name   = "${var.project}-github-deploy-dev"
  role   = aws_iam_role.github_deploy_dev[0].id
  policy = data.aws_iam_policy_document.github_deploy_dev.json
}

# Plan-only role: read/plan access to the environment state; never mutates.
resource "aws_iam_role" "github_plan_dev" {
  count              = var.enable_github_oidc ? 1 : 0
  name               = "${var.project}-github-plan-${var.github_environment}"
  assume_role_policy = data.aws_iam_policy_document.github_oidc_assume[0].json
  tags               = local.common_tags
}

data "aws_iam_policy_document" "github_plan_dev" {
  statement {
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetObject",
      "s3:HeadBucket",
      "s3:HeadObject",
    ]
    resources = [
      "arn:aws:s3:::${var.tfstate_bucket_name}",
      "arn:aws:s3:::${var.tfstate_bucket_name}/${var.github_environment}/*",
    ]
  }
  statement {
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:ListTasks",
    ]
    resources = ["*"]
  }
  statement {
    effect = "Allow"
    actions = [
      "ssm:GetParameter",
      "ssm:GetParameters",
    ]
    resources = ["arn:aws:ssm:*:*:parameter/${var.github_environment}/*"]
  }
}

resource "aws_iam_role_policy" "github_plan_dev" {
  count  = var.enable_github_oidc ? 1 : 0
  name   = "${var.project}-github-plan-${var.github_environment}"
  role   = aws_iam_role.github_plan_dev[0].id
  policy = data.aws_iam_policy_document.github_plan_dev.json
}

output "github_deploy_dev_role_arn" {
  value = var.enable_github_oidc ? aws_iam_role.github_deploy_dev[0].arn : ""
}

output "github_plan_dev_role_arn" {
  value = var.enable_github_oidc ? aws_iam_role.github_plan_dev[0].arn : ""
}
