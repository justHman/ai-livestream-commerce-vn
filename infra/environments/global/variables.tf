variable "project" {
  type    = string
  default = "ai-livestream"
}

variable "aws_region" {
  type    = string
  default = "ap-northeast-2"
}

variable "enable_github_oidc" {
  description = "Create GitHub Actions OIDC provider (once per account)"
  type        = bool
  default     = true
}

variable "github_oidc_thumbprint" {
  description = "GitHub Actions OIDC root CA thumbprint"
  type        = string
  default     = "6938fd4d98bab03faadb97b34396831e3780aea1"
}

variable "github_org_repo" {
  description = "repo filter for deploy role trust, e.g. org/repo"
  type        = string
  default     = ""
}

variable "create_tfstate_bucket" {
  description = "Manage shared tfstate S3 bucket from this stack"
  type        = bool
  default     = false
}

variable "tfstate_bucket_name" {
  type    = string
  default = "ai-livestream-tfstate-191918535424"
}

variable "create_tf_lock_table" {
  description = "Manage DynamoDB lock table from this stack"
  type        = bool
  default     = false
}

variable "tf_lock_table_name" {
  type    = string
  default = "ai-livestream-tf-lock"
}

variable "tags" {
  type    = map(string)
  default = {}
}
