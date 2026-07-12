variable "env" {
  description = "Environment name"
  type        = string
  default     = "prod"
}

variable "project" {
  description = "Project tag / name prefix"
  type        = string
  default     = "ai-livestream"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-northeast-2"
}

variable "vpc_cidr" {
  description = "VPC CIDR"
  type        = string
  default     = "10.30.0.0/16"
}

variable "public_subnet_cidr" {
  description = "Primary public subnet CIDR (compute pin AZ)"
  type        = string
  default     = "10.30.1.0/24"
}

variable "public_subnet_cidr_b" {
  description = "Second public subnet CIDR (ALB/RDS multi-AZ span; still public, no NAT)"
  type        = string
  default     = "10.30.2.0/24"
}

variable "az" {
  description = "Primary AZ"
  type        = string
  default     = "ap-northeast-2a"
}

variable "az_b" {
  description = "Second AZ for ALB/RDS subnet groups"
  type        = string
  default     = "ap-northeast-2b"
}

variable "extra_public_subnet_ids" {
  description = "Optional additional public subnet IDs beyond network module"
  type        = list(string)
  default     = []
}

variable "alb_ingress_cidrs" {
  description = "CIDRs allowed to ALB:443 — set Cloudflare IP ranges in prod"
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "certificate_arn" {
  description = "ACM cert ARN for ALB HTTPS (required for prod)"
  type        = string
  default     = ""
}

variable "db_password" {
  description = "RDS master password via TF_VAR / tfvars.local — never commit"
  type        = string
  sensitive   = true
  default     = "CHANGE_ME_via_TF_VAR"
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 100
}

variable "redis_node_type" {
  type    = string
  default = "cache.t4g.small"
}

variable "lmcache_enabled" {
  type    = bool
  default = false
}

variable "desired_backend" {
  type    = number
  default = 1
}

variable "desired_llm_tts" {
  type    = number
  default = 1
}

variable "desired_avatar" {
  type    = number
  default = 1
}

variable "desired_livekit" {
  type    = number
  default = 1
}

variable "desired_lmcache" {
  type    = number
  default = 0
}

variable "image_backend" {
  type    = string
  default = "imjusthman/ai-live-backend:latest"
}

variable "image_llm_tts" {
  type    = string
  default = "imjusthman/ai-live-llm-tts:latest"
}

variable "image_avatar" {
  type    = string
  default = "imjusthman/ai-live-avatar:latest"
}

variable "image_lmcache" {
  type    = string
  default = "imjusthman/ai-live-lmcache:latest"
}

variable "alert_email" {
  type    = string
  default = ""
}

variable "enable_billing_alarms" {
  type    = bool
  default = false
}

variable "create_ec2_capacity" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
