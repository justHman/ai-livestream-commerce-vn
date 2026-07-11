variable "env" {
  description = "Environment name (dev|prod)"
  type        = string
}

variable "project" {
  description = "Project tag value"
  type        = string
  default     = "ai-livestream"
}

variable "vpc_id" {
  description = "VPC ID for security groups"
  type        = string
}

variable "alb_ingress_cidrs" {
  description = "CIDRs allowed to reach ALB:443 (Cloudflare IPs or 0.0.0.0/0)"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "enable_oidc" {
  description = "Create GitHub Actions OIDC provider (usually only once per account)"
  type        = bool
  default     = false
}

variable "github_oidc_thumbprint" {
  description = "GitHub Actions OIDC root CA thumbprint"
  type        = string
  default     = "6938fd4d98bab03faadb97b34396831e3780aea1"
}

variable "tags" {
  description = "Extra tags merged onto all resources"
  type        = map(string)
  default     = {}
}
